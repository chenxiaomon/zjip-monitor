"""Accounts management: token status, re-login, and run-once scan."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fastapi import Form
from loguru import logger

from src import control
from src.config_loader import Account, ConfigError, load_accounts, save_accounts
from src.login import get_token
from src.main import run_all, run_single
from web.auth import require_admin, visible_accounts
from web.deps import executor as _executor, get_groups, spawn, templates
from web.services.session import _SESSIONS_DIR, _safe_name, get_session, token_status
from web.services.snapshot import get_latest_snapshot
from web.sse import broadcast

router = APIRouter()

# Per-account relogin task state: username → {"status": "pending|ok|error", "msg": ""}
_tasks: dict[str, dict[str, str]] = {}

# Per-account single scan state: username → "idle|running|done|error"
_scan_tasks: dict[str, str] = {}


def run_state_ctx() -> dict:
    """Template context for the global run status, read from data/control/state.json.

    Cross-process: also reflects (and can stop) runs started by the daemon or CLI.
    """
    state = control.read_state()
    if not state:
        return {"status": "idle", "msg": "", "progress": {}, "trigger": None}
    return {
        "status": state.get("status", "idle"),
        "msg": state.get("error") or "",
        "progress": state.get("progress") or {},
        "trigger": state.get("trigger"),
        "pid": state.get("pid"),
        "holder_alive": bool(state.get("holder_alive")),
    }


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _build_rows(accounts_list) -> list[dict]:
    rows = []
    for acc in accounts_list:
        session = get_session(acc.username)
        snap = get_latest_snapshot(acc.company)
        rows.append({
            "account": acc,
            "token_status": token_status(session),
            "record_count": snap.get("record_count", 0) if snap else 0,
            "last_check": snap.get("snapshot_time") if snap else None,
            "task": _tasks.get(acc.username, {}),
            "scan_task": _scan_tasks.get(acc.username, "idle"),
        })
    return rows


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

def _relogin_locked(username: str, password: str) -> None:
    """Fresh browser login under the global run lock (avoids clashing with a scan)."""
    ctx = control.try_acquire(kind="relogin", trigger="web")
    if ctx is None:
        raise RuntimeError("巡检进行中，请稍后再试")
    try:
        get_token(username, password, force_refresh=True)
    except Exception as exc:
        ctx.finish("error", error=str(exc)[:200])
        raise
    ctx.finish("done")


async def _relogin_async(username: str, password: str) -> None:
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            _executor,
            lambda: _relogin_locked(username, password),
        )
        _tasks[username] = {"status": "ok", "msg": ""}
    except Exception as exc:
        _tasks[username] = {"status": "error", "msg": str(exc)[:80]}


async def _scan_cycle_async() -> None:
    # Run state lives in data/control/state.json (written by run_all itself);
    # duplicate triggers are rejected by the global run lock inside run_all.
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_executor, lambda: run_all(trigger="web"))
        await broadcast(json.dumps({"type": "update"}))
    except Exception as exc:
        logger.error(f"run_all failed: {exc!r}")


async def _scan_one_async(username: str, account: Account) -> None:
    _scan_tasks[username] = "running"
    loop = asyncio.get_event_loop()
    status = "error"
    try:
        result = await loop.run_in_executor(
            _executor, lambda: run_single(account, trigger="web")
        )
        if result.get("skipped"):
            status = "idle"  # global lock busy — button reappears, user can retry
        elif result.get("error"):
            status = "error"
        else:
            status = "done"
        await broadcast(json.dumps({"type": "update"}))
    except Exception as exc:
        logger.error(f"[{account.company}] Single scan failed: {exc!r}")
    finally:
        _scan_tasks[username] = status


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request) -> HTMLResponse:
    rows = _build_rows(visible_accounts(request))
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {"rows": rows, "run_once": run_state_ctx()},
    )


@router.post("/accounts/{username}/relogin", response_class=HTMLResponse)
async def relogin(request: Request, username: str) -> HTMLResponse:
    accounts_list = visible_accounts(request)
    acc = next((a for a in accounts_list if a.username == username), None)
    if acc is None:
        return HTMLResponse(f'<tr id="row-{username}"><td colspan="7" class="px-4 py-2 text-red-500 text-sm">账号不存在</td></tr>')

    _tasks[username] = {"status": "pending", "msg": ""}
    spawn(_relogin_async(username, acc.password))

    snap = get_latest_snapshot(acc.company)
    row = {
        "account": acc,
        "token_status": token_status(get_session(username)),
        "record_count": snap.get("record_count", 0) if snap else 0,
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
    accounts_list = visible_accounts(request)
    acc = next((a for a in accounts_list if a.username == username), None)
    if acc is None:
        return HTMLResponse(f'<tr id="row-{username}"></tr>')

    snap = get_latest_snapshot(acc.company)
    row = {
        "account": acc,
        "token_status": token_status(get_session(username)),
        "record_count": snap.get("record_count", 0) if snap else 0,
        "last_check": snap.get("snapshot_time") if snap else None,
        "task": _tasks.get(username, {}),
    }
    return templates.TemplateResponse(
        request,
        "_partials/account_row.html",
        {"row": row},
    )


@router.post("/accounts/{username}/scan", response_class=HTMLResponse)
async def scan_account(request: Request, username: str) -> HTMLResponse:
    accounts_list = visible_accounts(request)
    acc = next((a for a in accounts_list if a.username == username), None)
    if acc is None:
        return HTMLResponse(f'<tr id="row-{username}"><td colspan="7" class="px-4 py-2 text-red-500 text-sm">账号不存在</td></tr>')

    if _scan_tasks.get(username) != "running":
        _scan_tasks[username] = "running"
        spawn(_scan_one_async(username, acc))

    snap = get_latest_snapshot(acc.company)
    row = {
        "account": acc,
        "token_status": token_status(get_session(username)),
        "record_count": snap.get("record_count", 0) if snap else 0,
        "last_check": snap.get("snapshot_time") if snap else None,
        "task": _tasks.get(username, {}),
        "scan_task": "running",
    }
    return templates.TemplateResponse(request, "_partials/account_row.html", {"row": row})


@router.get("/accounts/{username}/scan-status", response_class=HTMLResponse)
def scan_status_poll(request: Request, username: str) -> HTMLResponse:
    accounts_list = visible_accounts(request)
    acc = next((a for a in accounts_list if a.username == username), None)
    if acc is None:
        return HTMLResponse(f'<tr id="row-{username}"></tr>')

    snap = get_latest_snapshot(acc.company)
    row = {
        "account": acc,
        "token_status": token_status(get_session(username)),
        "record_count": snap.get("record_count", 0) if snap else 0,
        "last_check": snap.get("snapshot_time") if snap else None,
        "task": _tasks.get(username, {}),
        "scan_task": _scan_tasks.get(username, "idle"),
    }
    return templates.TemplateResponse(request, "_partials/account_row.html", {"row": row})


@router.post("/run-once", response_class=HTMLResponse)
async def run_once_trigger(request: Request) -> HTMLResponse:
    # Always spawn; run_all's global file lock rejects duplicates race-free.
    spawn(_scan_cycle_async())
    return templates.TemplateResponse(
        request,
        "_partials/run_once_status.html",
        {"run_once": {"status": "running", "msg": "", "progress": {}, "trigger": "web"}},
    )


@router.post("/run-once/stop", response_class=HTMLResponse)
async def run_once_stop(request: Request) -> HTMLResponse:
    control.request_stop()
    ctx = run_state_ctx()
    if ctx["status"] == "running":
        ctx["status"] = "stopping"
    return templates.TemplateResponse(
        request,
        "_partials/run_once_status.html",
        {"run_once": ctx},
    )


@router.get("/run-once/status", response_class=HTMLResponse)
def run_once_status(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_partials/run_once_status.html",
        {"run_once": run_state_ctx()},
    )


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------

@router.get("/accounts/new-form", response_class=HTMLResponse)
def new_account_form(request: Request) -> HTMLResponse:
    require_admin(request)
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
    group: str = Form(""),
) -> HTMLResponse:
    require_admin(request)
    company = company.strip()
    username = username.strip()
    password = password.strip()
    contact = contact.strip()
    group = group.strip()

    error = None
    accounts_list: list[Account] = []
    if not company:
        error = "公司名称不能为空"
    elif not username:
        error = "账号不能为空"
    elif not password:
        error = "密码不能为空"
    elif group and group not in get_groups():
        error = f"分组 {group} 不存在"
    else:
        try:
            accounts_list = load_accounts()
        except ConfigError as exc:
            # Never fall back to an empty list: save_accounts() overwrites the
            # whole file, which would wipe every existing account.
            error = f"账号库读取失败，拒绝写入：{exc}"
        else:
            if any(a.username == username for a in accounts_list):
                error = f"账号 {username} 已存在"

    if error:
        return templates.TemplateResponse(
            request,
            "_partials/account_form.html",
            {"error": error, "form": {"company": company, "username": username, "contact": contact}},
            headers={"HX-Retarget": "#modal-container", "HX-Reswap": "innerHTML"},
        )

    accounts_list.append(Account(company=company, username=username, password=password, contact=contact, group=group))
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
    spawn(_relogin_async(username, password))

    # Success: clear modal and trigger tbody refresh
    response = HTMLResponse("")
    response.headers["HX-Trigger"] = "accountSaved"
    return response


@router.get("/accounts/rows", response_class=HTMLResponse)
def accounts_rows(request: Request) -> HTMLResponse:
    rows = _build_rows(visible_accounts(request))
    return templates.TemplateResponse(
        request, "_partials/accounts_rows.html", {"rows": rows}
    )


@router.post("/accounts/{username}/group", response_class=HTMLResponse)
def set_account_group(request: Request, username: str, group: str = Form("")) -> HTMLResponse:
    require_admin(request)
    group = group.strip()
    if group and group not in get_groups():
        return HTMLResponse("分组不存在", status_code=400)
    try:
        accounts_list = load_accounts()
    except ConfigError as exc:
        return HTMLResponse(f"账号库读取失败，拒绝写入：{exc}", status_code=500)
    acc = next((a for a in accounts_list if a.username == username), None)
    if acc is None:
        return HTMLResponse(f'<tr id="row-{username}"><td colspan="7" class="px-4 py-2 text-red-500 text-sm">账号不存在</td></tr>')
    acc.group = group
    try:
        save_accounts(accounts_list)
    except ConfigError as exc:
        return HTMLResponse(f"保存失败：{exc}", status_code=500)

    snap = get_latest_snapshot(acc.company)
    row = {
        "account": acc,
        "token_status": token_status(get_session(username)),
        "record_count": snap.get("record_count", 0) if snap else 0,
        "last_check": snap.get("snapshot_time") if snap else None,
        "task": _tasks.get(username, {}),
        "scan_task": _scan_tasks.get(username, "idle"),
    }
    return templates.TemplateResponse(request, "_partials/account_row.html", {"row": row})


@router.post("/accounts/{username}/delete", response_class=HTMLResponse)
def delete_account(request: Request, username: str) -> HTMLResponse:
    require_admin(request)
    try:
        accounts_list = load_accounts()
    except ConfigError as exc:
        return HTMLResponse(f"账号库读取失败，拒绝写入：{exc}", status_code=500)
    new_list = [a for a in accounts_list if a.username != username]
    if len(new_list) == len(accounts_list):
        return HTMLResponse(status_code=404)
    try:
        save_accounts(new_list)
    except ConfigError as exc:
        return HTMLResponse(f"保存失败：{exc}", status_code=500)
    _tasks.pop(username, None)
    # Remove cached JWT token so it cannot be reused
    session_file = _SESSIONS_DIR / f"{_safe_name(username)}.json"
    if session_file.exists():
        session_file.unlink()
    return HTMLResponse("")
