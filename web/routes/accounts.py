"""Accounts management: token status, re-login, and run-once scan."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fastapi import Form

from src.config_loader import Account, ConfigError, load_accounts, save_accounts
from src.login import get_token
from src.main import run_all
from web.deps import templates
from web.services.session import _SESSIONS_DIR, _safe_name, get_session, token_status
from web.services.snapshot import get_latest_snapshot
from web.sse import broadcast

router = APIRouter()

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="accounts")

# Per-account relogin task state: username → {"status": "pending|ok|error", "msg": ""}
_tasks: dict[str, dict[str, str]] = {}

# Global run-once state
_run_once: dict[str, str] = {"status": "idle", "msg": ""}


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _build_rows(accounts_list=None) -> list[dict]:
    if accounts_list is None:
        try:
            accounts_list = load_accounts()
        except ConfigError:
            accounts_list = []
    rows = []
    for acc in accounts_list:
        session = get_session(acc.username)
        snap = get_latest_snapshot(acc.company)
        rows.append({
            "account": acc,
            "token_status": token_status(session),
            "record_count": snap["record_count"] if snap else 0,
            "last_check": snap.get("snapshot_time") if snap else None,
            "task": _tasks.get(acc.username, {}),
        })
    return rows


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _do_relogin(username: str, password: str) -> None:
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            _executor,
            lambda: get_token(username, password, force_refresh=True),
        )
        _tasks[username] = {"status": "ok", "msg": ""}
    except Exception as exc:
        _tasks[username] = {"status": "error", "msg": str(exc)[:80]}


async def _do_run_once() -> None:
    loop = asyncio.get_event_loop()
    _run_once["status"] = "running"
    _run_once["msg"] = ""
    try:
        await loop.run_in_executor(_executor, run_all)
        _run_once["status"] = "done"
        await broadcast(json.dumps({"type": "update"}))
    except Exception as exc:
        _run_once["status"] = "error"
        _run_once["msg"] = str(exc)[:80]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request) -> HTMLResponse:
    rows = _build_rows()
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {"rows": rows, "run_once": _run_once},
    )


@router.post("/accounts/{username}/relogin", response_class=HTMLResponse)
async def relogin(request: Request, username: str) -> HTMLResponse:
    try:
        accounts_list = load_accounts()
    except ConfigError:
        accounts_list = []
    acc = next((a for a in accounts_list if a.username == username), None)
    if acc is None:
        return HTMLResponse(f'<tr id="row-{username}"><td colspan="7" class="px-4 py-2 text-red-500 text-sm">账号不存在</td></tr>')

    _tasks[username] = {"status": "pending", "msg": ""}
    asyncio.create_task(_do_relogin(username, acc.password))

    snap = get_latest_snapshot(acc.company)
    row = {
        "account": acc,
        "token_status": token_status(get_session(username)),
        "record_count": snap["record_count"] if snap else 0,
        "last_check": snap.get("snapshot_time") if snap else None,
        "task": {"status": "pending", "msg": ""},
    }
    return templates.TemplateResponse(
        request,
        "_partials/account_row.html",
        {"row": row},
    )


@router.get("/accounts/{username}/token-status", response_class=HTMLResponse)
def token_status_poll(request: Request, username: str) -> HTMLResponse:
    try:
        accounts_list = load_accounts()
    except ConfigError:
        accounts_list = []
    acc = next((a for a in accounts_list if a.username == username), None)
    if acc is None:
        return HTMLResponse(f'<tr id="row-{username}"></tr>')

    snap = get_latest_snapshot(acc.company)
    row = {
        "account": acc,
        "token_status": token_status(get_session(username)),
        "record_count": snap["record_count"] if snap else 0,
        "last_check": snap.get("snapshot_time") if snap else None,
        "task": _tasks.get(username, {}),
    }
    return templates.TemplateResponse(
        request,
        "_partials/account_row.html",
        {"row": row},
    )


@router.post("/run-once", response_class=HTMLResponse)
async def run_once_trigger(request: Request) -> HTMLResponse:
    if _run_once.get("status") != "running":
        asyncio.create_task(_do_run_once())
    return templates.TemplateResponse(
        request,
        "_partials/run_once_status.html",
        {"run_once": {"status": "running", "msg": ""}},
    )


@router.get("/run-once/status", response_class=HTMLResponse)
def run_once_status(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_partials/run_once_status.html",
        {"run_once": _run_once},
    )


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------

@router.get("/accounts/new-form", response_class=HTMLResponse)
def new_account_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_partials/account_form.html", {"error": None}
    )


@router.post("/accounts", response_class=HTMLResponse)
async def add_account(
    request: Request,
    company: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    contact: str = Form(""),
) -> HTMLResponse:
    company = company.strip()
    username = username.strip()
    password = password.strip()
    contact = contact.strip()

    error = None
    if not company:
        error = "公司名称不能为空"
    elif not username:
        error = "账号不能为空"
    elif not password:
        error = "密码不能为空"
    else:
        try:
            existing = load_accounts()
        except ConfigError:
            existing = []
        if any(a.username == username for a in existing):
            error = f"账号 {username} 已存在"

    if error:
        return templates.TemplateResponse(
            request,
            "_partials/account_form.html",
            {"error": error, "form": {"company": company, "username": username, "contact": contact}},
            headers={"HX-Retarget": "#modal-container", "HX-Reswap": "innerHTML"},
        )

    try:
        accounts_list = load_accounts()
    except ConfigError:
        accounts_list = []
    accounts_list.append(Account(company=company, username=username, password=password, contact=contact))
    try:
        save_accounts(accounts_list)
    except ConfigError as exc:
        return templates.TemplateResponse(
            request,
            "_partials/account_form.html",
            {"error": f"保存失败：{exc}", "form": {"company": company, "username": username, "contact": contact}},
            headers={"HX-Retarget": "#modal-container", "HX-Reswap": "innerHTML"},
        )

    # Auto-trigger first login for the new account
    _tasks[username] = {"status": "pending", "msg": ""}
    asyncio.create_task(_do_relogin(username, password))

    # Success: clear modal and trigger tbody refresh
    response = HTMLResponse("")
    response.headers["HX-Trigger"] = "accountSaved"
    return response


@router.get("/accounts/rows", response_class=HTMLResponse)
def accounts_rows(request: Request) -> HTMLResponse:
    rows = _build_rows()
    return templates.TemplateResponse(
        request, "_partials/accounts_rows.html", {"rows": rows}
    )


@router.post("/accounts/{username}/delete", response_class=HTMLResponse)
def delete_account(request: Request, username: str) -> HTMLResponse:
    try:
        accounts_list = load_accounts()
    except ConfigError:
        accounts_list = []
    new_list = [a for a in accounts_list if a.username != username]
    if len(new_list) == len(accounts_list):
        return HTMLResponse(status_code=404)
    try:
        save_accounts(new_list)
        _tasks.pop(username, None)
        # Remove cached JWT token so it cannot be reused
        session_file = _SESSIONS_DIR / f"{_safe_name(username)}.json"
        if session_file.exists():
            session_file.unlink()
    except ConfigError:
        pass
    return HTMLResponse("")
