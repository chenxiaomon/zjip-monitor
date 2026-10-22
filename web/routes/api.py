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
from web.deps import _fmt_time, account_dot, detail_url, in_year, int_or_none
from web.services import audit_log
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


def _visible_snapshots(request: Request) -> list[dict]:
    """Latest snapshot per account the caller is allowed to see.

    This used to read every snapshot on disk regardless of the caller, so the
    console's group isolation stopped at the API boundary. The key carries
    admin scope today, so the set is the same minus orphan snapshots of
    accounts that no longer exist — but binding keys to groups is now a change
    to the middleware, not to every handler.
    """
    snaps = []
    for acc in visible_accounts(request):
        snap = get_latest_snapshot(acc.company)
        if snap is not None:
            snaps.append(snap)
    return snaps


@router.get("/status")
def api_status(request: Request, year: int | None = Query(None)) -> JSONResponse:
    """All accounts' latest status (optionally limited to one application year)."""
    accounts = []
    total_records = 0
    success_count = 0
    pending_correction = 0

    for snap in _visible_snapshots(request):
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

    for snap in _visible_snapshots(request):
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
