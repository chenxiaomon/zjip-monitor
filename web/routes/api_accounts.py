"""The mini program's company actions: 立即巡检, 去平台办理, 新增企业.

Each is the console's own implementation (web/routes/accounts.py: start_scan,
reveal_account, add_account_core, add_form_options) with JSON around it —
the same permission checks, cooldowns and operation-log lines. Only a
colleague's own sign-in may call these: the API_KEY lane is the .env admin,
and old mini program builds shipped that key. A company outside the caller's
groups answers exactly as one that does not exist, and the platform login
leaves the server only in a reveal.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from src.config_loader import Account
from web.auth import current_user, use_query_scope, visible_accounts
from web.deps import _fmt_time, detail_url, group_label, owner_label, scan_kind_label
from web.routes import accounts as accounts_routes
from web.routes.api import _can, _personal_only
from web.routes.dashboard import scan_health

router = APIRouter(prefix="/api", dependencies=[Depends(use_query_scope)])

_NOT_FOUND = {"error": "企业不存在或无权查看"}


def _visible_by_company(request: Request, company) -> Account | None:
    return next((a for a in visible_accounts(request) if a.company == str(company or "")), None)


async def _json_body(request: Request) -> dict | None:
    """A JSON object, as /api/login takes. A form or text/plain body — what
    another site can make a browser send without asking first — is refused."""
    if not request.headers.get("content-type", "").startswith("application/json"):
        return None
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _bad_body() -> JSONResponse:
    return JSONResponse({"error": "请求格式错误"}, status_code=400)


def _scrub(text: str, acc: Account) -> str:
    """A platform error message may quote the login; only a reveal sends it."""
    return text.replace(acc.username, "（平台账号）") if acc.username else text


@router.get("/company")
def api_company(request: Request, company: str | None = Query(None)) -> JSONResponse:
    """One company's login and scan state — what the console's 账号 row shows,
    picked field by field from its _row(): the row itself carries the Account,
    password and all."""
    if (denied := _personal_only(request)):
        return denied
    acc = _visible_by_company(request, company)
    if acc is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    row = accounts_routes._row(acc)
    task = row["task"] or {}
    ss = row["scan_status"] or {}
    health = scan_health(ss)
    me = (current_user(request) or {}).get("username")
    return JSONResponse({
        "company": acc.company,
        "group": group_label(acc.group),
        "owner": owner_label(acc.owner, acc.group),
        "mine": bool(me and acc.owner == me),
        "record_count": row["record_count"],
        "last_check": _fmt_time(row["last_check"]),
        "login": {
            # pending | ok | error after a login started here or in the console; "" otherwise
            "state": task.get("status") or "",
            "error": _scrub(task.get("msg") or "", acc)[:300] if task.get("status") == "error" else "",
            "label": row["token_status"]["label"],
            "paused": health["login_paused"],
        },
        "scan": {
            "state": row["scan_task"],  # idle | running | done | error
            "failed": health["scan_failed"],
            "partial": health["scan_partial"],
            "label": scan_kind_label(ss.get("status")) if health["scan_failed"] or health["scan_partial"] else "",
            "error": _scrub(ss.get("error") or "", acc)[:160] if health["scan_failed"] else "",
        },
        "scan_wait": accounts_routes.scan_wait(request, acc.username),
        "can": _can(request),
    })


@router.get("/accounts/new-form")
def api_new_account_form(request: Request) -> JSONResponse:
    """The 新增企业 form's choices, as the console's dialog offers them."""
    if (denied := _personal_only(request)):
        return denied
    if not _can(request)["add"]:
        return JSONResponse({"error": "只读账号不能新增企业"}, status_code=403)
    options = accounts_routes.add_form_options(request)
    return JSONResponse({
        "groups": [{"key": k, "label": group_label(k) if k else "未分组（仅管理员可见）"}
                   for k in options["groups"]],
        "owners": {k: [{"username": u, "label": label} for u, label in choices]
                   for k, choices in options["owners"].items()},
        "default_group": options["default_group"],
        "default_owner": options["default_owner"],
    })


@router.post("/accounts")
async def api_add_account(request: Request) -> JSONResponse:
    """{"company", "username", "password", "contact"?, "group", "owner"?} → the
    console's 新增: same checks and messages, then the first login starts
    (follow it with GET /api/company)."""
    if (denied := _personal_only(request)):
        return denied
    body = await _json_body(request)
    if body is None:
        return _bad_body()
    result = await accounts_routes.add_account_core(
        request, **{k: str(body.get(k) or "") for k in ("company", "username", "password",
                                                         "contact", "group", "owner")},
    )
    if not result.ok:
        return JSONResponse({"error": result.error}, status_code=result.status)
    return JSONResponse({"ok": True, "company": result.acc.company})


@router.post("/accounts/scan")
async def api_scan(request: Request) -> JSONResponse:
    """{"company"} → 立即巡检 (follow it with GET /api/company). A 经办人 gets
    one per company per 10 minutes: 429 with retry_after seconds."""
    if (denied := _personal_only(request)):
        return denied
    body = await _json_body(request)
    if body is None:
        return _bad_body()
    acc = _visible_by_company(request, body.get("company"))
    result = await accounts_routes.start_scan(request, acc.username if acc else "")
    if result.status == 404:
        return JSONResponse(_NOT_FOUND, status_code=404)
    if result.status == 429:
        return JSONResponse({"error": result.error, "retry_after": result.retry_after}, status_code=429)
    if not result.ok:
        return JSONResponse({"error": result.error}, status_code=result.status)
    return JSONResponse({"ok": True, "already_running": result.already_running})


@router.post("/accounts/platform-access")
async def api_platform_access(request: Request) -> JSONResponse:
    """{"company", "reg_no"?} → 去平台办理: the platform login and password,
    logged every time. POST so no URL carries it; no-store so nothing keeps it."""
    if (denied := _personal_only(request)):
        return denied
    body = await _json_body(request)
    if body is None:
        return _bad_body()
    acc = _visible_by_company(request, body.get("company"))
    result = accounts_routes.reveal_account(request, acc.username if acc else "", str(body.get("reg_no") or ""))
    if result.status == 404:
        return JSONResponse(_NOT_FOUND, status_code=404)
    if not result.ok:
        return JSONResponse({"error": result.error}, status_code=result.status)
    return JSONResponse({
        "company": result.acc.company,
        "username": result.acc.username,
        "password": result.acc.password,
        "login_url": accounts_routes.platform_login_url(),
        "detail_url": detail_url(result.reg_no) if result.reg_no else "",
    }, headers={"Cache-Control": "no-store"})
