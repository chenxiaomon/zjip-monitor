"""Console identities, session cookies, and per-request visibility helpers.

Users:
    .env admin   — WEB_USER / WEB_PASS: sees and manages everything; the way
                   back in when config/users.yaml is broken, and the Basic-Auth
                   credential scripts use. Never stored in or editable from the
                   user file.
    file users   — config/users.yaml (web/services/users.py), managed on the
                   用户 page: admin / operator (经办人) / viewer (只读), each
                   non-admin bound to one or more group keys from
                   config/settings.yaml. Role and groups are read on every
                   request, so a change applies without a new login.

Session:
    Stateless HMAC-signed cookie "szt_session" = "username|expiry|signature".
    Secret from SESSION_SECRET, else derived from FERNET_KEY, else WEB_PASS.
    The signature also covers the user's session_key: rotating it (password
    reset, 强制下线, disable, role change) ends every session issued so far.
    The .env admin and users imported from the old WEB_USERS table have an
    empty key, which signs exactly what the old cookies signed — so the
    switch logged nobody out. An .env admin session can only be ended by
    rotating SESSION_SECRET.
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
from web.services import users
from web.services.attachments_svc import _safe_name as _safe_company_key

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

if _ADMIN_USER.lower() in users.RESERVED_NAMES:
    # "view|expiry|sig" is the /view page's cookie; an admin of that name
    # would make the two interchangeable.
    logger.error(f"WEB_USER 不能是保留名 {_ADMIN_USER!r}，请在 .env 中改名")


# ---------------------------------------------------------------------------
# Login / session cookie
# ---------------------------------------------------------------------------

def _is_env_admin(username: str) -> bool:
    return secrets.compare_digest(username.encode(), _ADMIN_USER.encode())


def _env_admin(via: str) -> dict:
    return {
        "username": _ADMIN_USER, "display_name": "", "role": "admin",
        "groups": [], "source": "env", "via": via,
    }


def _file_user(username: str, rec: dict, via: str) -> dict:
    return {
        "username": username, "display_name": rec["display_name"], "role": rec["role"],
        "groups": list(rec["groups"]), "source": "file", "via": via,
        "must_change_password": rec["must_change_password"],
    }


def authenticate(username: str, password: str) -> tuple[str | None, str]:
    """Check a login. Returns (username, "ok"), (None, "bad") or
    (None, "disabled") — the last only for the right password, so it tells
    a guesser nothing.

    Slow on purpose (one PBKDF2 per call): run it off the event loop. Every
    attempt costs exactly one hash — a dummy one for an unknown name and for
    the .env admin, whose password is compared in plain — so the response
    time does not reveal which usernames exist. A file user who shares the
    .env admin's name is ignored: the rescue login always wins.
    """
    if len(password) > users.MAX_PASSWORD_LEN:
        return None, "bad"
    if _is_env_admin(username):
        users.dummy_verify(password)
        ok = secrets.compare_digest(password.encode(), _ADMIN_PASS.encode())
        return (_ADMIN_USER, "ok") if ok else (None, "bad")
    name = users.normalize_username(username)
    rec = users.get(name)
    if rec is None or not rec["password_hash"]:
        users.dummy_verify(password)
        return None, "bad"
    if not users.verify_password(password, rec["password_hash"]):
        return None, "bad"
    if rec["disabled"]:
        return None, "disabled"
    return name, "ok"


def is_known_user(username: str) -> bool:
    """Whether a login name belongs to someone — the operation log stores
    an unknown one masked (it is often a password typed in the wrong box)."""
    return _is_env_admin(username) or users.get(username) is not None


def _sign(payload: str, key: str = "") -> str:
    # An empty key signs the bare payload: byte-for-byte the pre-users.yaml
    # signature, so the .env admin's and imported users' cookies stay valid.
    message = f"{payload}|{key}" if key else payload
    return hmac.new(_SECRET, message.encode(), hashlib.sha256).hexdigest()


def _session_key(username: str) -> str:
    if _is_env_admin(username):
        return ""
    rec = users.get(username)
    return rec["session_key"] if rec else ""


def make_cookie(username: str) -> str:
    expiry = int(time.time()) + SESSION_MAX_AGE
    payload = f"{username}|{expiry}"
    return f"{payload}|{_sign(payload, _session_key(username))}"


def user_from_cookie(cookie: str | None) -> dict | None:
    """Verify the session cookie and return the user dict, else None.

    Looks the user up on every call: a deleted or disabled user, or one whose
    session_key was rotated, is signed out on the next request.
    """
    if not cookie:
        return None
    try:
        username, expiry_str, sig = cookie.rsplit("|", 2)
        if int(expiry_str) < time.time():
            return None
    except (ValueError, TypeError):
        return None
    payload = f"{username}|{expiry_str}"
    if _is_env_admin(username):
        return _env_admin("cookie") if secrets.compare_digest(sig, _sign(payload)) else None
    rec = users.get(username)
    if rec is None or rec["disabled"]:
        return None
    if not secrets.compare_digest(sig, _sign(payload, rec["session_key"])):
        return None
    return _file_user(users.normalize_username(username), rec, "cookie")


def admin_user(via: str = "basic") -> dict:
    """The .env admin, for the Basic-Auth fallback and the API-key lane."""
    return _env_admin(via)


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
    # Not intersected with settings.yaml's groups: a settings file that
    # fails to parse reads as {} and would otherwise hide every company.
    groups = set(user.get("groups") or [])
    return [a for a in accounts if a.group and a.group in groups]


def allowed_usernames(request: Request) -> set[str] | None:
    """None = no restriction (admin); otherwise the set of visible usernames."""
    if is_admin(request):
        return None
    return {a.username for a in visible_accounts(request)}


def allowed_companies(request: Request) -> set[str] | None:
    if is_admin(request):
        return None
    return {a.company for a in visible_accounts(request)}


def allowed_company_keys(request: Request) -> set[str] | None:
    """Visible companies as their on-disk directory names.

    Attachments live under data/attachments/<sanitized company>/, and the
    sanitizer maps every non-alphanumeric character to "_" — so "A 公司" and
    "A_公司" share a directory while comparing as different strings. Checking
    the raw name therefore guarded a different key than the one that selects
    the file.
    """
    if is_admin(request):
        return None
    return {_safe_company_key(a.company) for a in visible_accounts(request)}
