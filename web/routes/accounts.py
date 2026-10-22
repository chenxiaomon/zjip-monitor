"""Accounts management: token status, re-login/verify, edit, and run-once scan."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from fastapi import Form
from loguru import logger

import html as _html
import re

from src import control, status_store
from src.config_loader import Account, ConfigError, load_accounts, save_accounts
from src.exceptions import CredentialsRejectedError
from src.login import _save_token, verify_credentials
from src.main import run_all, run_single
from web.auth import (
    can_edit_accounts, current_user, editable_account, is_admin, require_admin, visible_accounts,
)
from web.deps import executor as _executor, get_groups, group_label, spawn, templates, zjip_base_url
from web.services import audit_log, users
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


# Every login the console starts against the platform (重登, 测试登录, the
# automatic check after a password edit) is a real round-trip, and a wrong
# password repeated often enough can get the CLIENT's platform account
# locked. All of them are counted per account over a sliding window; only
# non-admins are held to the limits — admins are the ones fixing a password
# and need the retry, but their attempts still use up the window.
_LOGIN_WINDOW_SECONDS = 600
_RELOGIN_LIMIT = 1   # 重登: as before, once per window
_VERIFY_LIMIT = 3    # 测试登录 and the check after a password edit
_web_logins: dict[str, deque[float]] = {}
_web_logins_lock = threading.Lock()


def _login_wait(username: str, limit: int) -> int:
    """Seconds until `username` may start another console login under
    `limit` per window (0 = now)."""
    now = time.monotonic()
    with _web_logins_lock:
        dq = _web_logins.get(username)
        if dq is None:
            return 0
        while dq and now - dq[0] >= _LOGIN_WINDOW_SECONDS:
            dq.popleft()
        if len(dq) < limit:
            return 0
        return max(1, int(_LOGIN_WINDOW_SECONDS - (now - dq[-limit])))


def _note_login(username: str) -> None:
    with _web_logins_lock:
        _web_logins.setdefault(username, deque()).append(time.monotonic())


def _relogin_cooldown_left(username: str) -> int:
    return _login_wait(username, _RELOGIN_LIMIT)


def _bump_verify_seq(username: str) -> int:
    _verify_seq[username] = _verify_seq.get(username, 0) + 1
    return _verify_seq[username]


def _owner_error(owner: str, group: str) -> str | None:
    """Why `owner` cannot be made responsible for a company in `group`, or
    None. A 负责人 must be able to see the company — otherwise「我负责的」
    would list nothing for them and nobody would notice."""
    if not owner:
        return None
    rec = users.get(owner)
    if rec is None or rec["disabled"]:
        return f"用户 {owner} 不存在或已停用"
    if rec["role"] != "admin" and group not in rec["groups"]:
        name = rec["display_name"] or owner
        return f"{name} 看不到「{group_label(group)}」分组的企业，请先在「用户」页给该同事加上这个分组"
    return None


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
    cell = f'<td colspan="8" class="px-4 py-2 text-red-500 text-sm">{_html.escape(msg)}</td>'
    return HTMLResponse(f'<tr id="row-{u}">{cell}</tr>')


def _row_error_on(request: Request, username: str, msg: str = "账号不存在") -> HTMLResponse:
    """_row_error from a state-changing route: nothing changed, nothing to log."""
    audit_log.nothing_changed(request)
    return _row_error(username, msg)


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
        return _row_error_on(request, username)

    if not is_admin(request):
        left = _relogin_cooldown_left(username)
        if left:
            audit_log.record_request(request, "account.relogin", company=acc.company, target=username,
                                     ok=False, detail={"error": "过于频繁"})
            return templates.TemplateResponse(
                request,
                "_partials/account_row.html",
                {"row": _row(acc, task={
                    "status": "error",
                    "msg": f"操作过于频繁，请 {left // 60 + 1} 分钟后再试",
                })},
            )

    _note_login(username)
    _tasks[username] = {"status": "pending", "msg": ""}
    spawn(_verify_async(username, acc.password, acc.company, seq=_bump_verify_seq(username)))
    audit_log.record_request(request, "account.relogin", company=acc.company, target=username)

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
        return _row_error_on(request, username)

    already = _scan_tasks.get(username) == "running"
    if not already:
        _scan_tasks[username] = "running"
        spawn(_scan_one_async(username, acc))
    audit_log.record_request(request, "account.scan", company=acc.company, target=username,
                             detail={"already_running": True} if already else None)

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
    audit_log.record_request(request, "scan.start")
    return templates.TemplateResponse(
        request,
        "_partials/run_once_status.html",
        {"run_once": {"status": "running", "msg": "", "progress": {}, "trigger": "web"}},
    )


@router.post("/run-once/stop", response_class=HTMLResponse)
async def run_once_stop(request: Request) -> HTMLResponse:
    control.request_stop()
    audit_log.record_request(request, "scan.stop")
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
    owner: str = Form(""),
) -> HTMLResponse:
    require_admin(request)
    company = company.strip()
    username = username.strip()
    password = password.strip()
    contact = contact.strip()
    group = group.strip()
    owner = users.normalize_username(owner)

    def _err(error: str) -> HTMLResponse:
        audit_log.nothing_changed(request)
        return templates.TemplateResponse(
            request,
            "_partials/account_form.html",
            {"error": error, "mode": "add",
             "form": {"company": company, "username": username, "contact": contact, "group": group,
                      "owner": owner}},
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
    if (problem := _owner_error(owner, group)):
        return _err(problem)

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
            company=company, username=username, password=password, contact=contact, group=group,
            owner=owner,
        ))
        try:
            save_accounts(accounts_list)
        except ConfigError as exc:
            return _err(_public_error(exc, "save_accounts failed", username=username))

    audit_log.record_request(request, "account.add", company=company, target=username,
                             detail={"group": group, "owner": owner})

    # Auto-trigger first login for the new account
    _note_login(username)
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
    password currently stored for that account.

    A 经办人 may only test accounts of their own groups — anything else is
    answered "不存在" without contacting the platform, so the button cannot be
    used to try passwords against other clients' accounts — and only a few
    times per window. Testing an account that is not saved yet (the 新增
    dialog) stays with admins."""
    if not can_edit_accounts(request):
        require_admin(request)
    username = username.strip()
    password = password.strip()

    stored_password = not password
    company = ""

    def _result(ok: bool, msg: str, *, attempted: bool = True) -> HTMLResponse:
        if attempted:
            # Every attempt, refused ones included: trying accounts outside
            # one's groups is exactly what the log is for.
            audit_log.record_request(
                request, "account.verify", company=company, target=username, ok=ok,
                detail={"stored_password": stored_password, **({} if ok else {"error": msg[:120]})},
            )
        else:
            audit_log.nothing_changed(request)
        return templates.TemplateResponse(
            request, "_partials/verify_result.html", {"ok": ok, "msg": msg}
        )

    if not username:
        return _result(False, "请先填写账号", attempted=False)
    if not is_admin(request):
        mine = editable_account(request, username)
        if mine is None:
            return _result(False, f"账号 {username} 不存在")
        company = mine.company
        wait = _login_wait(username, _VERIFY_LIMIT)
        if wait:
            return _result(False, f"测试登录过于频繁，请 {wait // 60 + 1} 分钟后再试")
    if not password:
        try:
            accounts_list = load_accounts()
        except ConfigError as exc:
            return _result(False, _public_error(exc, "verify: load_accounts failed", username=username),
                           attempted=False)
        acc = next((a for a in accounts_list if a.username == username), None)
        if acc is None:
            return _result(False, f"账号 {username} 尚未保存 — 密码留空时测试的是已保存密码，请先输入密码",
                           attempted=False)
        password = acc.password
        company = acc.company

    _note_login(username)
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
    if not can_edit_accounts(request):
        require_admin(request)
    if is_admin(request):
        try:
            accounts_list = load_accounts()
        except ConfigError as exc:
            return HTMLResponse(_public_error(exc, "edit-form: load_accounts failed", username=username), status_code=500)
        acc = next((a for a in accounts_list if a.username == username), None)
    else:
        acc = editable_account(request, username)
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
    owner: str = Form(""),
) -> HTMLResponse:
    """Edit contact/group/owner and optionally the password (empty = keep current).

    company/username are immutable here: snapshots, attachments and events are
    keyed by company name, and username keys the token cache + status store.
    A password change wipes the cached token (it belongs to the old password's
    session) and fires an automatic verify so the row shows the outcome.

    A 经办人 edits password and contact of their own groups' accounts only.
    The group and owner they submit are ignored, not applied: their form has no group
    field, and applying the empty value would move the company out of their
    own sight. Whether the account is theirs is decided again on the list
    loaded under the write lock, not on the one loaded before it."""
    if not can_edit_accounts(request):
        require_admin(request)
    admin = is_admin(request)
    my_groups = set((current_user(request) or {}).get("groups") or [])
    password = password.strip()
    contact = contact.strip()
    group = group.strip()

    acc: Account | None = None

    def _err(error: str) -> HTMLResponse:
        audit_log.nothing_changed(request)
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

    if admin and group and group not in get_groups():
        return _err(f"分组 {group} 不存在")

    with _ACCOUNTS_WRITE_LOCK:
        try:
            accounts_list = load_accounts()
        except ConfigError as exc:
            return _err(_public_error(exc, "load_accounts failed, refusing to write", username=username))
        acc = next((a for a in accounts_list if a.username == username), None)
        if acc is None or (not admin and acc.group not in my_groups):
            acc = None
            return _err(f"账号 {username} 不存在")
        before = {"contact": acc.contact, "group": acc.group, "owner": acc.owner}
        if admin:
            owner = users.normalize_username(owner)
            if (problem := _owner_error(owner, group)) and owner != acc.owner:
                return _err(problem)
        acc.contact = contact
        if admin:
            acc.group = group
            acc.owner = owner
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
        wait = 0 if admin else _login_wait(username, _VERIFY_LIMIT)
        verify_skipped = bool(wait)
        if wait:
            # The password is saved either way; only the platform check waits.
            _bump_verify_seq(username)  # an older in-flight check must not report on it
            _tasks[username] = {
                "status": "error",
                "msg": f"密码已保存；验证过于频繁，请 {wait // 60 + 1} 分钟后点「重登」",
            }
        else:
            _note_login(username)
            _tasks[username] = {"status": "pending", "msg": ""}
            spawn(_verify_async(username, password, acc.company, seq=_bump_verify_seq(username)))

    detail: dict = {"password_changed": bool(password)}
    for field, old in before.items():
        new = getattr(acc, field)
        if old != new:
            detail[field] = [old, new]
    if password and verify_skipped:
        detail["verify_skipped"] = True
    audit_log.record_request(request, "account.edit", company=acc.company, target=username, detail=detail)

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
            return _row_error_on(request, username)
        old_group = acc.group
        acc.group = group
        try:
            save_accounts(accounts_list)
        except ConfigError as exc:
            return HTMLResponse(_public_error(exc, "save_accounts failed", username=username), status_code=500)
    audit_log.record_request(request, "account.group", company=acc.company, target=username,
                             detail={"group": [old_group, group]})

    return templates.TemplateResponse(request, "_partials/account_row.html", {"row": _row(acc)})


@router.post("/accounts/{username}/owner", response_class=HTMLResponse)
def set_account_owner(request: Request, username: str, owner: str = Form("")) -> HTMLResponse:
    """负责人, from the row's drop-down (admins). Whose companies are whose
    is what「我负责的」on the dashboard, calendar and records is built on."""
    require_admin(request)
    owner = users.normalize_username(owner)
    with _ACCOUNTS_WRITE_LOCK:
        try:
            accounts_list = load_accounts()
        except ConfigError as exc:
            return HTMLResponse(_public_error(exc, "load_accounts failed, refusing to write", username=username), status_code=500)
        acc = next((a for a in accounts_list if a.username == username), None)
        if acc is None:
            return _row_error_on(request, username)
        if (problem := _owner_error(owner, acc.group)):
            return PlainTextResponse(problem, status_code=400)
        old_owner = acc.owner
        acc.owner = owner
        try:
            save_accounts(accounts_list)
        except ConfigError as exc:
            return HTMLResponse(_public_error(exc, "save_accounts failed", username=username), status_code=500)
    audit_log.record_request(request, "account.owner", company=acc.company, target=username,
                             detail={"owner": [old_owner, owner]})
    return templates.TemplateResponse(request, "_partials/account_row.html", {"row": _row(acc)})


# What a registration number looks like; anything else is not put in a link.
_REG_NO_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@router.post("/accounts/{username}/platform-access", response_class=HTMLResponse)
def platform_access(
    request: Request, username: str, reg_no: str = Form(""), inline: str = Form(""),
) -> HTMLResponse:
    """去平台办理: show a company's platform account and password so whoever
    handles it can sign in on zjip.org.cn and file there themselves.

    The system stays read-only towards the platform — submitting a filing is
    a person's act on the platform's own site. Showing the password is the
    disclosure, so it is logged here, every time (the copy button is client
    side). 经办人 for their own groups' companies, admins for any; 只读 users
    never see a client's password. POST so no prefetch or history entry
    carries it, and no-store so no cache keeps it.
    """
    if not can_edit_accounts(request):
        require_admin(request)
    acc = editable_account(request, username)
    if acc is None:
        return PlainTextResponse("账号不存在", status_code=404)
    reg_no = reg_no.strip() if _REG_NO_RE.match(reg_no.strip()) else ""
    audit_log.record_request(request, "account.reveal", company=acc.company, target=username,
                             detail={"reg_no": reg_no} if reg_no else None)
    return templates.TemplateResponse(
        request,
        "_partials/platform_access.html",
        {"acc": acc, "reg_no": reg_no, "modal": inline != "1",
         "login_url": f"{zjip_base_url()}/user/login"},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/accounts/{username}/delete", response_class=HTMLResponse)
def delete_account(request: Request, username: str) -> HTMLResponse:
    require_admin(request)
    with _ACCOUNTS_WRITE_LOCK:
        try:
            accounts_list = load_accounts()
        except ConfigError as exc:
            return HTMLResponse(_public_error(exc, "load_accounts failed, refusing to write", username=username), status_code=500)
        gone = next((a for a in accounts_list if a.username == username), None)
        if gone is None:
            return HTMLResponse(status_code=404)
        new_list = [a for a in accounts_list if a.username != username]
        try:
            save_accounts(new_list)
        except ConfigError as exc:
            return HTMLResponse(_public_error(exc, "save_accounts failed", username=username), status_code=500)
    audit_log.record_request(request, "account.delete", company=gone.company, target=username,
                             detail={"group": gone.group})
    _tasks.pop(username, None)
    status_store.forget_account(username)
    _bump_verify_seq(username)  # an in-flight verify must not recreate the token below
    # Remove cached JWT token so it cannot be reused
    session_file = _SESSIONS_DIR / f"{_safe_name(username)}.json"
    if session_file.exists():
        session_file.unlink()
    return HTMLResponse("")
