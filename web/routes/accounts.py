"""Accounts management: token status, re-login/verify, edit, and run-once scan."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fastapi import Form
from loguru import logger

import html as _html

from src import control, status_store
from src.config_loader import Account, ConfigError, load_accounts, save_accounts
from src.exceptions import CredentialsRejectedError
from src.login import _save_token, verify_credentials
from src.main import run_all, run_single
from web.auth import is_admin, require_admin, visible_accounts
from web.deps import executor as _executor, get_groups, spawn, templates
from web.services.run_state import run_state_ctx
from web.services.session import _SESSIONS_DIR, _safe_name, get_session, token_status
from web.services.snapshot import get_latest_snapshot
from web.sse import broadcast

router = APIRouter()

# Per-account relogin task state: username → {"status": "pending|ok|error", "msg": ""}
# Transient UI state only (spinner + freshest error text); durable outcomes
# live in src/status_store (data/control/accounts_status.json).
_tasks: dict[str, dict[str, str]] = {}

# Per-account single scan state: username → "idle|running|done|error"
_scan_tasks: dict[str, str] = {}

# Login verifies must not queue behind a scan/report occupying the shared
# 2-worker pool in web/deps — fixing a password needs its own lane.
_verify_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="verify")

# accounts.enc writers (add/edit/group/delete) do load→mutate→save; without a
# lock two concurrent admin saves would silently drop one of the two changes.
_ACCOUNTS_WRITE_LOCK = threading.Lock()

# Per-account verify generation: each relogin/add/edit bumps it, and an
# in-flight verify whose generation is stale discards its result — a slow
# old-password verify must not cache its token or touch the streak after the
# password was edited.
_verify_seq: dict[str, int] = {}


# A group user re-triggering a login costs a real platform round-trip and
# feeds that account's lockout streak, so it is rate limited per account.
# Admins are not: they are the ones fixing a password and need the retry.
_RELOGIN_COOLDOWN_SECONDS = 600
_last_relogin: dict[str, float] = {}


def _relogin_cooldown_left(username: str) -> int:
    last = _last_relogin.get(username)
    if last is None:
        return 0
    remaining = _RELOGIN_COOLDOWN_SECONDS - (time.monotonic() - last)
    return max(0, int(remaining))


def _bump_verify_seq(username: str) -> int:
    _verify_seq[username] = _verify_seq.get(username, 0) + 1
    return _verify_seq[username]


def _company_key(company: str) -> str:
    """Same sanitizer the snapshot/attachment filenames use — duplicates must
    be detected on the SANITIZED name, since that's what collides on disk."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in company)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _row(acc: Account, *, task: dict | None = None, scan_task: str | None = None) -> dict:
    """Single source for the account-row template context — every route that
    re-renders a row goes through here so the durable scan_status badge never
    disappears after a partial swap."""
    snap = get_latest_snapshot(acc.company)
    return {
        "account": acc,
        "token_status": token_status(get_session(acc.username)),
        "record_count": snap.get("record_count", 0) if snap else 0,
        "last_check": snap.get("snapshot_time") if snap else None,
        "task": task if task is not None else _tasks.get(acc.username, {}),
        "scan_task": scan_task if scan_task is not None else _scan_tasks.get(acc.username, "idle"),
        "scan_status": status_store.get_status(acc.username),
        "snap_fetch": snap.get("fetch") if snap else None,
    }


def _row_error(username: str, msg: str = "账号不存在") -> HTMLResponse:
    """A table row saying why there is nothing to show.

    The username comes straight from the URL path, so it MUST be escaped: the
    two GET pollers below answer 200 text/html for any unknown username, which
    made `/accounts/<payload>/token-status` a reflected-XSS sink that a logged
    -in admin could be walked into by a single link (the session cookie is
    SameSite=Lax, so a top-level navigation carries it).
    """
    u = _html.escape(username, quote=True)
    cell = f'<td colspan="7" class="px-4 py-2 text-red-500 text-sm">{_html.escape(msg)}</td>'
    return HTMLResponse(f'<tr id="row-{u}">{cell}</tr>')


def _row_empty(username: str) -> HTMLResponse:
    """Empty row for the polling endpoints: swapping it in stops the poll."""
    return HTMLResponse(f'<tr id="row-{_html.escape(username, quote=True)}"></tr>')


def _public_error(exc: Exception, context: str, *, username: str = "") -> str:
    """Log the real cause, return something safe and actionable for the page.

    ConfigError text names config paths and the encryption key setup; a save
    failure can carry a filesystem path. None of it helps whoever is looking
    at the console, and it travels over plain HTTP.
    """
    who = f"[{username}] " if username else ""
    logger.error(f"{who}{context}: {exc!r}")
    if isinstance(exc, ConfigError):
        return "账号库读取失败，请查看服务器日志"
    return "操作失败，请查看服务器日志"


def _build_rows(accounts_list) -> list[dict]:
    return [_row(acc) for acc in accounts_list]


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _verify_async(username: str, password: str, company: str = "", seq: int | None = None) -> None:
    """Fresh login with the PERSISTED password (relogin / add / password edit).

    No global run lock: the login session's egress guard limits it to exactly
    one POST /v1/user/login, so it cannot clash with a scan's data — worst
    case it refreshes the token under a running scan, which main.py already
    self-heals via its TokenExpiredError re-login. The token is cached and the
    lockout streak updated only if this verify is still the CURRENT generation
    for the account (seq check runs on the event-loop thread, race-free) —
    otherwise a slow verify of an already-replaced password is discarded."""
    loop = asyncio.get_event_loop()

    def _stale() -> bool:
        return seq is not None and seq != _verify_seq.get(username)

    try:
        token = await loop.run_in_executor(
            _verify_executor,
            lambda: verify_credentials(username, password),
        )
    except Exception as exc:
        if _stale():
            logger.info(f"[{username}] verify result outdated (password changed meanwhile) — discarded")
            return
        if isinstance(exc, CredentialsRejectedError):
            status_store.record_login(username, company, ok=False, error=str(exc))
        _tasks[username] = {"status": "error", "msg": str(exc)[:500]}
        return
    if _stale():
        logger.info(f"[{username}] verify result outdated (password changed meanwhile) — discarded")
        return
    _save_token(username, token)  # password is persisted → caching is correct
    status_store.record_login(username, company, ok=True)
    _tasks[username] = {"status": "ok", "msg": ""}


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
        return _row_error(username)

    if not is_admin(request):
        left = _relogin_cooldown_left(username)
        if left:
            return templates.TemplateResponse(
                request,
                "_partials/account_row.html",
                {"row": _row(acc, task={
                    "status": "error",
                    "msg": f"操作过于频繁，请 {left // 60 + 1} 分钟后再试",
                })},
            )

    _last_relogin[username] = time.monotonic()
    _tasks[username] = {"status": "pending", "msg": ""}
    spawn(_verify_async(username, acc.password, acc.company, seq=_bump_verify_seq(username)))

    return templates.TemplateResponse(
        request,
        "_partials/account_row.html",
        {"row": _row(acc, task={"status": "pending", "msg": ""})},
    )


@router.get("/accounts/{username}/token-status", response_class=HTMLResponse)
def token_status_poll(request: Request, username: str) -> HTMLResponse:
    accounts_list = visible_accounts(request)
    acc = next((a for a in accounts_list if a.username == username), None)
    if acc is None:
        return _row_empty(username)

    return templates.TemplateResponse(
        request,
        "_partials/account_row.html",
        {"row": _row(acc)},
    )


@router.post("/accounts/{username}/scan", response_class=HTMLResponse)
async def scan_account(request: Request, username: str) -> HTMLResponse:
    # A single-account scan takes the GLOBAL run lock and one of the two shared
    # workers: repeated clicks by a group user would keep both the daemon's
    # round and the admin's 立即巡检 answering "already running".
    require_admin(request)
    accounts_list = visible_accounts(request)
    acc = next((a for a in accounts_list if a.username == username), None)
    if acc is None:
        return _row_error(username)

    if _scan_tasks.get(username) != "running":
        _scan_tasks[username] = "running"
        spawn(_scan_one_async(username, acc))

    return templates.TemplateResponse(
        request, "_partials/account_row.html", {"row": _row(acc, scan_task="running")}
    )


@router.get("/accounts/{username}/scan-status", response_class=HTMLResponse)
def scan_status_poll(request: Request, username: str) -> HTMLResponse:
    accounts_list = visible_accounts(request)
    acc = next((a for a in accounts_list if a.username == username), None)
    if acc is None:
        return _row_empty(username)

    return templates.TemplateResponse(
        request, "_partials/account_row.html", {"row": _row(acc)}
    )


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
        request, "_partials/account_form.html", {"error": None, "mode": "add"}
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

    def _err(error: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_partials/account_form.html",
            {"error": error, "mode": "add",
             "form": {"company": company, "username": username, "contact": contact}},
            headers={"HX-Retarget": "#modal-container", "HX-Reswap": "innerHTML"},
        )

    if not company:
        return _err("公司名称不能为空")
    if not username:
        return _err("账号不能为空")
    if not password:
        return _err("密码不能为空")
    if group and group not in get_groups():
        return _err(f"分组 {group} 不存在")

    with _ACCOUNTS_WRITE_LOCK:
        try:
            accounts_list = load_accounts()
        except ConfigError as exc:
            # Never fall back to an empty list: save_accounts() overwrites the
            # whole file, which would wipe every existing account.
            return _err(_public_error(exc, "load_accounts failed, refusing to write", username=username))
        if any(a.username == username for a in accounts_list):
            return _err(f"账号 {username} 已存在")
        if any(_company_key(a.company) == _company_key(company) for a in accounts_list):
            # Snapshots/attachments are keyed by the sanitized company name —
            # a duplicate (even one that only collides after sanitization)
            # would overwrite the other account's history.
            return _err(f"公司名称 {company} 与现有账号的公司重名（或脱敏后同名），历史数据按公司名称存储，请区分开")
        accounts_list.append(Account(
            company=company, username=username, password=password, contact=contact, group=group
        ))
        try:
            save_accounts(accounts_list)
        except ConfigError as exc:
            return _err(_public_error(exc, "save_accounts failed", username=username))

    # Auto-trigger first login for the new account
    _tasks[username] = {"status": "pending", "msg": ""}
    spawn(_verify_async(username, password, company, seq=_bump_verify_seq(username)))

    # Success: clear modal and trigger tbody refresh
    response = HTMLResponse("")
    response.headers["HX-Trigger"] = "accountSaved"
    return response


@router.post("/accounts/verify", response_class=HTMLResponse)
async def verify_account(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
) -> HTMLResponse:
    """测试登录 without saving anything: zero token-cache interaction, no
    failure-streak bookkeeping, no global run lock (the login session's egress
    guard limits it to the single login POST). Empty password = test the
    password currently stored for that account."""
    require_admin(request)
    username = username.strip()
    password = password.strip()

    def _result(ok: bool, msg: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_partials/verify_result.html", {"ok": ok, "msg": msg}
        )

    if not username:
        return _result(False, "请先填写账号")
    if not password:
        try:
            accounts_list = load_accounts()
        except ConfigError as exc:
            return _result(False, _public_error(exc, "verify: load_accounts failed", username=username))
        acc = next((a for a in accounts_list if a.username == username), None)
        if acc is None:
            return _result(False, f"账号 {username} 尚未保存 — 密码留空时测试的是已保存密码，请先输入密码")
        password = acc.password

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            _verify_executor, lambda: verify_credentials(username, password)
        )
    except Exception as exc:
        return _result(False, str(exc)[:500])
    return _result(True, "登录成功，密码有效")


@router.get("/accounts/{username}/edit-form", response_class=HTMLResponse)
def edit_account_form(request: Request, username: str) -> HTMLResponse:
    require_admin(request)
    try:
        accounts_list = load_accounts()
    except ConfigError as exc:
        return HTMLResponse(_public_error(exc, "edit-form: load_accounts failed", username=username), status_code=500)
    acc = next((a for a in accounts_list if a.username == username), None)
    if acc is None:
        return HTMLResponse("账号不存在", status_code=404)
    return templates.TemplateResponse(
        request, "_partials/account_form.html",
        {"error": None, "mode": "edit", "account": acc},
    )


@router.post("/accounts/{username}/edit", response_class=HTMLResponse)
async def edit_account(
    request: Request,
    username: str,
    password: str = Form(""),
    contact: str = Form(""),
    group: str = Form(""),
) -> HTMLResponse:
    """Edit contact/group and optionally the password (empty = keep current).

    company/username are immutable here: snapshots, attachments and events are
    keyed by company name, and username keys the token cache + status store.
    A password change wipes the cached token (it belongs to the old password's
    session) and fires an automatic verify so the row shows the outcome."""
    require_admin(request)
    password = password.strip()
    contact = contact.strip()
    group = group.strip()

    acc: Account | None = None

    def _err(error: str) -> HTMLResponse:
        ctx = {"error": error, "mode": "edit", "account": acc}
        if acc is None:  # nothing to re-render the form with
            return HTMLResponse(
                f'<div class="p-4 text-sm text-red-500">{_html.escape(error)}</div>',
                headers={"HX-Retarget": "#modal-container", "HX-Reswap": "innerHTML"},
            )
        return templates.TemplateResponse(
            request, "_partials/account_form.html", ctx,
            headers={"HX-Retarget": "#modal-container", "HX-Reswap": "innerHTML"},
        )

    if group and group not in get_groups():
        return _err(f"分组 {group} 不存在")

    with _ACCOUNTS_WRITE_LOCK:
        try:
            accounts_list = load_accounts()
        except ConfigError as exc:
            return _err(_public_error(exc, "load_accounts failed, refusing to write", username=username))
        acc = next((a for a in accounts_list if a.username == username), None)
        if acc is None:
            return _err(f"账号 {username} 不存在")
        acc.contact = contact
        acc.group = group
        if password:
            acc.password = password
        try:
            save_accounts(accounts_list)
        except ConfigError as exc:
            return _err(_public_error(exc, "save_accounts failed", username=username))

    if password:
        # The cached token belongs to the old password's session — drop it so
        # nothing masks the new credential, then verify right away. Bumping the
        # verify generation invalidates any still-running old-password verify.
        session_file = _SESSIONS_DIR / f"{_safe_name(username)}.json"
        session_file.unlink(missing_ok=True)
        status_store.record_password_changed(username)
        _tasks[username] = {"status": "pending", "msg": ""}
        spawn(_verify_async(username, password, acc.company, seq=_bump_verify_seq(username)))

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
    with _ACCOUNTS_WRITE_LOCK:
        try:
            accounts_list = load_accounts()
        except ConfigError as exc:
            return HTMLResponse(_public_error(exc, "load_accounts failed, refusing to write", username=username), status_code=500)
        acc = next((a for a in accounts_list if a.username == username), None)
        if acc is None:
            return _row_error(username)
        acc.group = group
        try:
            save_accounts(accounts_list)
        except ConfigError as exc:
            return HTMLResponse(_public_error(exc, "save_accounts failed", username=username), status_code=500)

    return templates.TemplateResponse(request, "_partials/account_row.html", {"row": _row(acc)})


@router.post("/accounts/{username}/delete", response_class=HTMLResponse)
def delete_account(request: Request, username: str) -> HTMLResponse:
    require_admin(request)
    with _ACCOUNTS_WRITE_LOCK:
        try:
            accounts_list = load_accounts()
        except ConfigError as exc:
            return HTMLResponse(_public_error(exc, "load_accounts failed, refusing to write", username=username), status_code=500)
        new_list = [a for a in accounts_list if a.username != username]
        if len(new_list) == len(accounts_list):
            return HTMLResponse(status_code=404)
        try:
            save_accounts(new_list)
        except ConfigError as exc:
            return HTMLResponse(_public_error(exc, "save_accounts failed", username=username), status_code=500)
    _tasks.pop(username, None)
    status_store.forget_account(username)
    _bump_verify_seq(username)  # an in-flight verify must not recreate the token below
    # Remove cached JWT token so it cannot be reused
    session_file = _SESSIONS_DIR / f"{_safe_name(username)}.json"
    if session_file.exists():
        session_file.unlink()
    return HTMLResponse("")
