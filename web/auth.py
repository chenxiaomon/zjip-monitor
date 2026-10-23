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
from fastapi import HTTPException, Query, Request
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
    # See users.RESERVED_NAMES: an old /view cookie must not sign anyone in.
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


def lookup(username: str, via: str = "cookie") -> dict | None:
    """The user dict for a login name, as user_from_cookie would build it."""
    if _is_env_admin(username):
        return _env_admin(via)
    rec = users.get(username)
    return _file_user(users.normalize_username(username), rec, via) if rec else None


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


# ---------------------------------------------------------------------------
# Mini program tokens
# ---------------------------------------------------------------------------
# Colleagues log in to the mini program with their console account and get a
# token: "username|expiry|sig", the same shape as the cookie but signed over
# a different, newline-separated message — a cookie cannot be replayed as a
# token or the other way round. It carries the user's session_key, so every
# console revocation (password reset, 强制下线, disable, role change) ends
# the phone's access too. 30 days: re-typing a password on a phone every
# day is what makes people save it somewhere worse.
API_TOKEN_MAX_AGE = int(os.getenv("API_TOKEN_DAYS", "30")) * 86400


def _sign_api(username: str, expiry: str, key: str) -> str:
    message = f"api-token\n{username}\n{expiry}\n{key}"
    return hmac.new(_SECRET, message.encode(), hashlib.sha256).hexdigest()


def make_api_token(username: str) -> tuple[str, int]:
    """(token, expiry epoch seconds) for a user who just logged in."""
    expiry = int(time.time()) + API_TOKEN_MAX_AGE
    return f"{username}|{expiry}|{_sign_api(username, str(expiry), _session_key(username))}", expiry


def user_from_api_token(token: str | None) -> dict | None:
    """Verify a mini program token, like user_from_cookie does a cookie.

    Unlike a cookie, it is refused while the user still has to change an
    initial password: /api/login no longer hands such users a token, and one
    handed out before that rule must not keep working for its 30 days.
    """
    if not token:
        return None
    try:
        username, expiry_str, sig = token.rsplit("|", 2)
        if int(expiry_str) < time.time():
            return None
    except (ValueError, TypeError):
        return None
    if _is_env_admin(username):
        ok = secrets.compare_digest(sig, _sign_api(username, expiry_str, ""))
        return _env_admin("api") if ok else None
    rec = users.get(username)
    if rec is None or rec["disabled"] or rec["must_change_password"]:
        return None
    if not secrets.compare_digest(sig, _sign_api(username, expiry_str, rec["session_key"])):
        return None
    return _file_user(users.normalize_username(username), rec, "api")


def is_https(request) -> bool:
    """True when the browser reached us over HTTPS. nginx terminates TLS and
    sends X-Forwarded-Proto; uvicorn trusts it from 127.0.0.1 by default."""
    return request.url.scheme == "https"


def set_session_cookie(response, username: str, *, secure: bool = False) -> None:
    """Sign `username` in on `response` (login, and after changing one's own
    password — which rotated the key every older cookie was signed with).

    `secure` (pass is_https(request)): someone typing http:// would otherwise
    send the cookie in clear on the request nginx then redirects to HTTPS.
    Plain-HTTP local runs and tests keep a cookie the browser sends back."""
    response.set_cookie(
        COOKIE_NAME,
        make_cookie(username),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


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


def can_edit_accounts(request: Request) -> bool:
    """Admins and 经办人 may change a visible company's platform password and
    contact, run 测试登录 and 立即巡检, reveal its platform login (去平台办理),
    and add or delete companies in their own groups. Regrouping, 负责人 and
    the full scan stay admin-only."""
    user = current_user(request)
    return user is not None and user.get("role") in ("admin", "operator")


def editable_account(request: Request, username: str) -> Account | None:
    """The account `username` if the current user may edit it, else None."""
    if not can_edit_accounts(request):
        return None
    return next((a for a in visible_accounts(request) if a.username == username), None)


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


# ---------------------------------------------------------------------------
# 我负责的 — a display preference, never a permission
# ---------------------------------------------------------------------------
# Colleagues split the work by client: whoever is a company's 负责人 sees
# 「我负责的」by default on the dashboard, calendar, records and changes, and
# can switch to everything they may see. The console keeps the choice in a
# cookie; the mini program sends it on every call (?scope=, use_query_scope).
# Rights (editing, attachments, the record drawer) keep using visible_accounts().
SCOPE_COOKIE = "zjip_scope"


def use_query_scope(request: Request, scope: str | None = Query(None)) -> None:
    """Router dependency of /api: ?scope=mine|all. Anything else — and every
    build from before the switch, which sends nothing — means 全部, so an old
    mini program keeps getting exactly what it always got."""
    request.state.scope_pref = scope if scope in ("mine", "all") else "all"


def _wants_mine(request: Request) -> bool:
    pref = getattr(request.state, "scope_pref", None)
    if pref is not None:
        return pref == "mine"
    return request.cookies.get(SCOPE_COOKIE) != "all"


def view_scope(request: Request) -> dict:
    """{"mine": bool, "owned": n, "visible": n, "accounts": [...]} — cached
    on the request, since a page and its template both ask."""
    cached = getattr(request.state, "view_scope", None)
    if cached is not None:
        return cached
    visible = visible_accounts(request)
    me = (current_user(request) or {}).get("username")
    owned = [a for a in visible if me and a.owner == me]
    mine = bool(owned) and _wants_mine(request)
    scope = {"mine": mine, "owned": len(owned), "visible": len(visible),
             "accounts": owned if mine else visible}
    request.state.view_scope = scope
    return scope


def shown_accounts(request: Request) -> list[Account]:
    """The companies a page lists: 我负责的 or everything visible."""
    return view_scope(request)["accounts"]


def shown_usernames(request: Request) -> set[str] | None:
    """As allowed_usernames(), narrowed to 我负责的 when that is on."""
    scope = view_scope(request)
    if not scope["mine"]:
        return allowed_usernames(request)
    return {a.username for a in scope["accounts"]}


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
