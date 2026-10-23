"""JSON API for the WeChat Mini Program.

Colleagues sign in with their console account (POST /api/login) and send
the token they get back as `Authorization: Bearer …`; they then see exactly
the companies the console shows them. The X-API-Key header and Basic Auth
still work for scripts and carry the .env admin's scope.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from loguru import logger

from src.scraper import AUDIT_STATUS, decode_status
from web import auth, login_guard
from web.auth import current_user, visible_accounts
from web.deps import _fmt_time, account_dot, detail_url, in_year, int_or_none, owner_label
from web.services import audit_log
from web.services.events import read_change_rows
from web.services.snapshot import get_latest_snapshot

router = APIRouter(prefix="/api")

# Same damping as the console login form (web/routes/login.py).
FAILED_LOGIN_DELAY_SECONDS = 0.5


def _public_user(user: dict) -> dict:
    return {k: user.get(k) for k in ("username", "display_name", "role", "groups")}


@router.post("/login")
async def api_login(request: Request) -> JSONResponse:
    """{"username", "password"} → {"token", "expires_at", "user"}.

    The same login guard as the console form, keyed the same way, so the
    mini program is not a second door for guessing a colleague's password.
    """
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    ip = login_guard.client_ip(request)

    wait = login_guard.seconds_locked(ip, username)
    if wait:
        return JSONResponse({"error": f"失败次数过多，请 {wait // 60 + 1} 分钟后再试"},
                            status_code=429, headers={"Retry-After": str(wait)})

    matched, reason = await asyncio.to_thread(auth.authenticate, username, password)
    who = username if auth.is_known_user(username) else login_guard.mask(username)
    if reason == "disabled":
        audit_log.record_request(request, "auth.login_failed", actor=who, ok=False,
                                 detail={"via": "api", "reason": "已停用"})
        return JSONResponse({"error": "账号已停用，请联系管理员"}, status_code=403)
    if matched is None:
        just_locked = login_guard.record_failure(ip, username)
        await asyncio.sleep(FAILED_LOGIN_DELAY_SECONDS)
        logger.warning(f"小程序登录失败 ip={ip} user={login_guard.mask(username)}")
        audit_log.record_request(request, "auth.login_failed", actor=who, ok=False,
                                 detail={"via": "api"})
        if just_locked:
            audit_log.record_request(request, "auth.lockout", actor=who, ok=False,
                                     detail={"via": "api", "minutes": login_guard.LOCK_SECONDS // 60})
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)

    login_guard.record_success(ip, username)
    user = auth.lookup(matched, via="api")
    if user.get("must_change_password"):
        # The console only reminds, because it can take the new password right
        # there. The mini program has nowhere to change it, so a 30-day token
        # would let an initial password set by the administrator (or imported
        # from WEB_USERS) live on unchanged.
        audit_log.record_request(request, "auth.login_failed", actor=who, ok=False,
                                 detail={"via": "api", "reason": "需先修改初始口令"})
        return JSONResponse({"error": "请先登录控制台修改初始口令，再用小程序登录"},
                            status_code=403)
    audit_log.record_request(request, "auth.login", as_user=user)
    token, expiry = auth.make_api_token(matched)
    return JSONResponse({
        "token": token,
        "expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
        "user": _public_user(user),
    })


@router.post("/logout")
def api_logout(request: Request) -> JSONResponse:
    """The phone forgets its token; this only records that it did. The token
    itself stays valid until it expires — 强制下线 on the 用户 page ends it."""
    audit_log.record_request(request, "auth.logout")
    return JSONResponse({"ok": True})


@router.get("/me")
def api_me(request: Request) -> JSONResponse:
    return JSONResponse({"user": _public_user(current_user(request) or {})})

_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _ms_to_date(ms: int | None) -> str:
    """Same defensive shape as the console's copy: an out-of-range timestamp
    from the platform must not 500 the whole endpoint."""
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=_SHANGHAI_TZ).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError, TypeError):
        logger.warning(f"Unparsable dataRegApplyTime: {ms!r}")
        return ""


def _visible_pairs(request: Request) -> list[tuple]:
    """(account, latest snapshot) for every account the caller may see — the
    caller's own companies (负责人) first.

    This used to read every snapshot on disk regardless of the caller, so the
    console's group isolation stopped at the API boundary; going through
    visible_accounts() also drops orphan snapshots of deleted accounts.
    """
    me = (current_user(request) or {}).get("username")
    pairs = []
    for acc in visible_accounts(request):
        snap = get_latest_snapshot(acc.company)
        if snap is not None:
            pairs.append((acc, snap))
    pairs.sort(key=lambda p: not (me and p[0].owner == me))  # stable: mine first
    return pairs


@router.get("/status")
def api_status(request: Request, year: int | None = Query(None)) -> JSONResponse:
    """All accounts' latest status (optionally limited to one application year)."""
    accounts = []
    total_records = 0
    success_count = 0
    pending_correction = 0

    me = (current_user(request) or {}).get("username")
    for acc, snap in _visible_pairs(request):
        status_counts: dict[int, int] = {}
        for r in snap.get("records", []):
            if not in_year(r.get("dataRegApplyTime"), year):
                continue
            code = int_or_none(r.get("dataRegAuditStatus"))
            if code is not None:
                status_counts[code] = status_counts.get(code, 0) + 1

        total = sum(status_counts.values())
        accounts.append({
            "company": snap.get("company", "未知"),
            "dot": account_dot(status_counts),
            "total": total,
            "status_counts": {str(k): v for k, v in status_counts.items()},
            "last_check": _fmt_time(snap.get("snapshot_time")),
            "owner": owner_label(acc.owner, acc.group),
            "mine": bool(me and acc.owner == me),
        })
        total_records += total
        success_count += status_counts.get(7, 0)
        pending_correction += status_counts.get(2, 0)

    return JSONResponse({
        "total_records": total_records,
        "success_count": success_count,
        "pending_correction": pending_correction,
        "accounts": accounts,
        "status_labels": {str(k): v for k, v in AUDIT_STATUS.items()},
    })


@router.get("/records")
def api_records(
    request: Request,
    company: str | None = Query(None),
    status: int | None = Query(None),
    year: int | None = Query(None),
) -> JSONResponse:
    """Registration records, filtered by company name, status and/or application year."""
    rows: list[dict] = []

    for _acc, snap in _visible_pairs(request):
        snap_company = snap.get("company", "")
        if company and snap_company != company:
            continue
        for r in snap.get("records", []):
            if not in_year(r.get("dataRegApplyTime"), year):
                continue
            code = int_or_none(r.get("dataRegAuditStatus"))
            if status is not None and code != status:
                continue
            reg_no = r.get("dataRegNo", "")
            rows.append({
                "company": snap_company,
                "reg_no": reg_no,
                "name": r.get("dataRegName", ""),
                "apply_date": _ms_to_date(r.get("dataRegApplyTime")),
                "status_code": code,
                "status_label": decode_status(code),
                "detail_url": detail_url(reg_no) if reg_no else "",
            })

    rows.sort(key=lambda r: r["apply_date"], reverse=True)
    return JSONResponse({"records": rows, "total": len(rows)})


# The mini program's 最近变化 list and its 「有新变化」 pop-up.
CHANGES_DAYS = 30  # the console's 变更 page shows the same window
CHANGES_LIMIT = 200
# An event is stamped with its scan's fetch time but written only after the
# snapshot is saved (and, for a single-company scan, after the push went
# out), while scans run in parallel — so an event can reach the log stamped
# earlier than one already there. A bookmark taken from the clock would step
# over it; the phone asks again from an hour back and drops by `key` the
# events it has already shown.
CHANGES_OVERLAP = timedelta(hours=1)


@router.get("/changes")
def api_changes(request: Request, since: str | None = Query(None)) -> JSONResponse:
    """Change rows, newest first: the last 30 days, or those stamped at or
    after `since` — the previous answer's `next_since`."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=CHANGES_DAYS)
    if since:
        try:
            asked = datetime.fromisoformat(since)
        except ValueError:
            return JSONResponse({"error": "since 格式错误"}, status_code=400)
        if asked.tzinfo is None:
            asked = asked.replace(tzinfo=timezone.utc)
        start = max(start, asked)

    # visible_accounts(), as /api/status: changes of a deleted company go too.
    visible = {a.username: a for a in visible_accounts(request)}
    me = (current_user(request) or {}).get("username")
    rows = read_change_rows(accounts=set(visible), since=start)
    out = []
    for r in rows[:CHANGES_LIMIT]:
        acc = visible.get(r["account"])
        reg_no = r["reg_no"]
        out.append({
            # One scan of one company; the platform login stays off the wire.
            "key": f"{r['company']}|{r['timestamp']}",
            "company": r["company"],
            "time": _fmt_time(r["timestamp"]),
            "type": r["type"],
            "reg_no": reg_no,
            "name": r["name"],
            "old_status": r["old_status"],
            "new_status": r["new_status"],
            "old_status_code": int_or_none(r["old_status_code"]),
            "new_status_code": int_or_none(r["new_status_code"]),
            "mine": bool(me and acc is not None and acc.owner == me),
            "detail_url": detail_url(reg_no) if reg_no else "",
        })
    return JSONResponse({
        "rows": out,
        "total": len(rows),
        "next_since": (now - CHANGES_OVERLAP).isoformat(),
    })
