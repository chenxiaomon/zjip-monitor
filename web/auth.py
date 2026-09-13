"""Console user store, session cookies, and per-request visibility helpers.

Users:
    admin        — from WEB_USER / WEB_PASS (sees and manages everything)
    group users  — from WEB_USERS="user1:pass1:group_a,user2:pass2:group_b"
                   each bound to one group key defined in config/settings.yaml

Session:
    Stateless HMAC-signed cookie "szt_session" = "username|expiry|signature".
    Secret from SESSION_SECRET, else derived from FERNET_KEY, else WEB_PASS.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import HTTPException, Request
from loguru import logger

from src.config_loader import Account, ConfigError, load_accounts

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")

COOKIE_NAME = "szt_session"
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE_HOURS", "12")) * 3600

_ADMIN_USER = os.getenv("WEB_USER", "admin")
_ADMIN_PASS = os.getenv("WEB_PASS", "changeme")


def _session_secret() -> bytes:
    explicit = os.getenv("SESSION_SECRET", "").strip()
    if explicit:
        return explicit.encode()
    fernet = os.getenv("FERNET_KEY", "").strip()
    if fernet:
        return hmac.new(fernet.encode(), b"web-session", hashlib.sha256).digest()
    return hmac.new(_ADMIN_PASS.encode(), b"web-session", hashlib.sha256).digest()


_SECRET = _session_secret()


def _parse_users() -> dict[str, dict]:
    """Build the user table: admin + group users from WEB_USERS."""
    users: dict[str, dict] = {
        _ADMIN_USER: {"password": _ADMIN_PASS, "role": "admin", "group": ""},
    }
    raw = os.getenv("WEB_USERS", "").strip()
    if not raw:
        return users
    for i, entry in enumerate(raw.split(",")):
        entry = entry.strip()
        if not entry:
            continue
        # username:password:group — split from both ends so the password
        # itself may contain colons. Never echo the entry (it holds a password).
        username, sep1, rest = entry.partition(":")
        password, sep2, group = rest.rpartition(":")
        if not sep1 or not sep2 or not username.strip() or not password.strip() or not group.strip():
            raise RuntimeError(
                f"WEB_USERS 第 {i + 1} 项格式错误（应为 用户名:密码:分组key）"
            )
        username, password, group = username.strip(), password.strip(), group.strip()
        if username in users:
            raise RuntimeError(f"WEB_USERS 用户名重复：{username!r}")
        users[username] = {"password": password, "role": "group", "group": group}
    return users


_USERS = _parse_users()


# ---------------------------------------------------------------------------
# Login / session cookie
# ---------------------------------------------------------------------------

def check_login(username: str, password: str) -> str | None:
    """Return the username if credentials match a known user, else None."""
    matched = None
    for name, info in _USERS.items():
        user_ok = secrets.compare_digest(username.encode(), name.encode())
        pass_ok = secrets.compare_digest(password.encode(), info["password"].encode())
        if user_ok and pass_ok:
            matched = name
    return matched


def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_cookie(username: str) -> str:
    expiry = int(time.time()) + SESSION_MAX_AGE
    payload = f"{username}|{expiry}"
    return f"{payload}|{_sign(payload)}"


def user_from_cookie(cookie: str | None) -> dict | None:
    """Verify the session cookie and return the user dict, else None."""
    if not cookie:
        return None
    try:
        username, expiry_str, sig = cookie.rsplit("|", 2)
        payload = f"{username}|{expiry_str}"
        if not secrets.compare_digest(sig, _sign(payload)):
            return None
        if int(expiry_str) < time.time():
            return None
    except (ValueError, TypeError):
        return None
    info = _USERS.get(username)
    if info is None:
        return None
    return {"username": username, "role": info["role"], "group": info["group"]}


def admin_user() -> dict:
    """Synthesized user for Basic-Auth admin fallback."""
    return {"username": _ADMIN_USER, "role": "admin", "group": ""}


# ---------------------------------------------------------------------------
# Public /view page access
# ---------------------------------------------------------------------------
# /view is deliberately unauthenticated, but it lists every monitored company
# with its case counts and status breakdown — on a public IP, with no robots
# rule. VIEW_TOKEN turns the URL itself into the credential: visit once with
# ?t=<token> and a signed cookie carries access from then on.
#
# Left unset, the page stays open exactly as before: this must not silently
# cut off whoever is using the link today.
VIEW_TOKEN = os.getenv("VIEW_TOKEN", "").strip()
VIEW_COOKIE = "szt_view"
VIEW_COOKIE_MAX_AGE = int(os.getenv("VIEW_COOKIE_DAYS", "30")) * 86400

if VIEW_TOKEN:
    logger.info("VIEW_TOKEN 已配置 — /view 需要 ?t=<token> 或已登录控制台")
else:
    logger.warning("VIEW_TOKEN 未配置 — /view 公开只读页对任何人可见")


def make_view_cookie() -> str:
    expiry = int(time.time()) + VIEW_COOKIE_MAX_AGE
    payload = f"view|{expiry}"
    return f"{payload}|{_sign(payload)}"


def view_cookie_ok(cookie: str | None) -> bool:
    if not cookie:
        return False
    try:
        kind, expiry_str, sig = cookie.rsplit("|", 2)
        if kind != "view":
            return False
        if not secrets.compare_digest(sig, _sign(f"{kind}|{expiry_str}")):
            return False
        return int(expiry_str) >= time.time()
    except (ValueError, TypeError):
        return False


def view_token_ok(token: str | None) -> bool:
    """Constant-time check of the ?t= parameter."""
    if not VIEW_TOKEN or not token:
        return False
    return secrets.compare_digest(token, VIEW_TOKEN)


# ---------------------------------------------------------------------------
# Per-request helpers
# ---------------------------------------------------------------------------

def current_user(request: Request) -> dict | None:
    return getattr(request.state, "user", None)


def is_admin(request: Request) -> bool:
    """No identified user means NO admin rights.

    This used to answer True when request.state.user was unset — i.e. every
    path the auth middleware does not populate (the public allow-list and the
    /api/* lane). Nothing reached those helpers from there yet, but the default
    was "unknown caller = administrator", which is the wrong way round for a
    helper that also backs require_admin() and visible_accounts().
    """
    user = current_user(request)
    return user is not None and user.get("role") == "admin"


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="仅管理员可操作")


def visible_accounts(request: Request) -> list[Account]:
    """Accounts the current user may see (admin → all, group user → own group)."""
    user = current_user(request)
    if user is None:
        return []  # unidentified caller sees nothing
    try:
        accounts = load_accounts()
    except ConfigError as exc:
        # Loud, not silent: an empty account page caused by a bad FERNET_KEY or
        # corrupt accounts.enc must be diagnosable from the logs.
        logger.error(f"加载账号失败（页面将显示为空）：{exc}")
        return []
    if is_admin(request):
        return accounts
    return [a for a in accounts if a.group == user.get("group", "")]


def allowed_usernames(request: Request) -> set[str] | None:
    """None = no restriction (admin); otherwise the set of visible usernames."""
    if is_admin(request):
        return None
    return {a.username for a in visible_accounts(request)}


def allowed_companies(request: Request) -> set[str] | None:
    if is_admin(request):
        return None
    return {a.company for a in visible_accounts(request)}
