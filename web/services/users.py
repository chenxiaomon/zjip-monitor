"""Console users — config/users.yaml, managed from the 用户 page.

    version: 1
    users:
      zhangsan:
        display_name: 张三
        role: operator            # admin | operator | viewer
        groups: [group_a]         # settings.yaml → groups keys; ignored for admins
        password_hash: pbkdf2_sha256$600000$<salt>$<hash>
        disabled: false
        session_key: 3f9c…        # rotating it signs every open session out
        must_change_password: false
        created_at / created_by / updated_at / password_changed_at

The .env admin (WEB_USER / WEB_PASS) is not in this file and never will be:
it is the way back in when this file is broken or every file admin is gone,
and the Basic-Auth credential scripts use. web/auth.py merges the two.

Same discipline as settings_svc: display reads are lenient and cached (a
broken file reads as "no file users" — only the .env admin can log in, and
an error is logged), every change is a strict read-modify-write under one
lock that refuses to overwrite a file it cannot parse. Only the web process
writes this file, so a threading lock is enough.

Passwords are PBKDF2-HMAC-SHA256 from the standard library (no scrypt in the
LibreSSL-built Pythons some laptops run, and no new dependency). Each hash
carries its own iteration count, so raising PBKDF2_ITERATIONS later only
affects new hashes — and the test suite can lower it without invalidating
anything. At 600k iterations a check costs a few hundred ms on the 1 vCPU
production box; callers run it off the event loop and _HASH_SLOTS keeps a
burst of logins from queueing up every worker thread behind it.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from src import audit
from src.fsutil import atomic_write_text

_USERS_FILE = Path(__file__).parent.parent.parent / "config" / "users.yaml"
_LOCK = threading.RLock()

PBKDF2_ITERATIONS = 600_000
# A hand-edited hash must not be able to pin a worker for minutes.
_MAX_ITERATIONS = 5_000_000
_HASH_SLOTS = threading.BoundedSemaphore(2)

MIN_PASSWORD_LEN = 10
MAX_PASSWORD_LEN = 256

ROLES = ("admin", "operator", "viewer")
ROLE_LABELS = {"admin": "管理员", "operator": "经办人", "viewer": "只读"}

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
# "view" once collided with the public /view page's own signed cookie
# ("view|expiry|sig"). That cookie is gone; the name stays reserved so an
# old cookie of that shape can never belong to anyone.
RESERVED_NAMES = frozenset({"view"})

_cache_lock = threading.Lock()
# ((st_mtime_ns, st_size), users, error message or None)
_cache: tuple[tuple[int, int], dict[str, dict], str | None] | None = None


class UsersUnreadable(Exception):
    """users.yaml exists but cannot be read or parsed."""


class UserError(ValueError):
    """A change the operator asked for is not allowed; str() is shown as-is."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def env_admin_name() -> str:
    return os.getenv("WEB_USER", "admin")


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    with _HASH_SLOTS:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_b64, digest_b64 = str(stored).split("$")
        iterations = int(iters)
        if scheme != "pbkdf2_sha256" or not 0 < iterations <= _MAX_ITERATIONS:
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
    except (ValueError, TypeError):
        return False
    with _HASH_SLOTS:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def dummy_verify(password: str) -> None:
    """Spend what one real check costs, so the response time of a login
    attempt does not tell whether the username exists."""
    with _HASH_SLOTS:
        hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"\0" * 16, PBKDF2_ITERATIONS)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def validate_username(username: str) -> str:
    name = normalize_username(username)
    if not _USERNAME_RE.match(name):
        raise UserError("用户名需为 2–32 位小写字母、数字或 . _ -，且以字母或数字开头")
    if name in RESERVED_NAMES:
        raise UserError(f"「{name}」是保留名，请换一个")
    if name == env_admin_name().lower():
        raise UserError(f"「{name}」与 .env 备用管理员同名，请换一个")
    return name


def validate_password(password: str, username: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise UserError(f"口令至少 {MIN_PASSWORD_LEN} 位")
    if len(password) > MAX_PASSWORD_LEN:
        raise UserError(f"口令最多 {MAX_PASSWORD_LEN} 位")
    if not password.strip():
        raise UserError("口令不能全是空格")
    if password.strip().lower() == normalize_username(username):
        raise UserError("口令不能与用户名相同")


def _clean_groups(role: str, groups: list[str]) -> list[str]:
    cleaned = sorted({str(g).strip() for g in groups or [] if str(g).strip()})
    if role != "admin" and not cleaned:
        raise UserError("经办人和只读用户至少要属于一个分组")
    return cleaned


def _clean_role(role: str) -> str:
    if role not in ROLES:
        raise UserError("角色无效")
    return role


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _normalize(rec: Any) -> dict:
    """Fill defaults so readers never KeyError; an unknown role reads as the
    least privileged one rather than as an admin."""
    rec = dict(rec) if isinstance(rec, dict) else {}
    if rec.get("role") not in ROLES:
        rec["role"] = "viewer"
    groups = rec.get("groups")
    rec["groups"] = [str(g) for g in groups] if isinstance(groups, list) else []
    rec["display_name"] = str(rec.get("display_name") or "")
    rec["password_hash"] = str(rec.get("password_hash") or "")
    rec["session_key"] = str(rec.get("session_key") or "")
    rec["disabled"] = bool(rec.get("disabled"))
    rec["must_change_password"] = bool(rec.get("must_change_password"))
    return rec


def _read_strict() -> dict:
    """The whole document for a read-modify-write; a missing file is empty,
    an unparseable one raises instead of pretending to be empty."""
    try:
        text = _USERS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "users": {}}
    except OSError as exc:
        raise UsersUnreadable(f"读不了 config/users.yaml（{exc.strerror or exc}）") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise UsersUnreadable("config/users.yaml 有格式错误") from exc
    if data is None:
        return {"version": 1, "users": {}}
    if not isinstance(data, dict) or not isinstance(data.get("users", {}), dict):
        raise UsersUnreadable("config/users.yaml 的结构不对（缺少 users 表）")
    data.setdefault("version", 1)
    data["users"] = {str(k): v for k, v in (data.get("users") or {}).items()}
    return data


def _load_cached() -> tuple[dict[str, dict], str | None]:
    global _cache
    try:
        st = _USERS_FILE.stat()
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, f"读不了 config/users.yaml（{exc.strerror or exc}）"
    key = (st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1], _cache[2]
    try:
        users = {name: _normalize(rec) for name, rec in _read_strict()["users"].items()}
        error = None
    except UsersUnreadable as exc:
        users, error = {}, str(exc)
        # Once per version of the file, not once per request.
        logger.error(f"控制台用户文件无法读取，只有 .env 备用管理员能登录：{exc}")
    with _cache_lock:
        _cache = (key, users, error)
    return users, error


def load() -> dict[str, dict]:
    """All file users (copies); {} when the file is missing or unreadable."""
    return copy.deepcopy(_load_cached()[0])


def load_error() -> str | None:
    """Why the file could not be read, for the 用户 page; None when fine."""
    return _load_cached()[1]


def get(username: str) -> dict | None:
    rec = _load_cached()[0].get(normalize_username(username))
    return copy.deepcopy(rec) if rec is not None else None


def invalidate() -> None:
    global _cache
    with _cache_lock:
        _cache = None


# ---------------------------------------------------------------------------
# Changes — each one a strict read-modify-write under _LOCK
# ---------------------------------------------------------------------------

def _save(doc: dict) -> None:
    atomic_write_text(
        _USERS_FILE,
        yaml.safe_dump(doc, allow_unicode=True, default_flow_style=False, sort_keys=False),
        mode=0o600,
    )
    invalidate()


def _existing(doc: dict, username: str) -> dict:
    rec = doc["users"].get(username)
    if not isinstance(rec, dict):
        raise UserError("用户不存在")
    return rec


def create_user(
    username: str, *, display_name: str, role: str, groups: list[str], password: str, by: str,
    must_change: bool = True,
) -> str:
    """Returns the normalized username. The administrator picked the first
    password and handed it over, so by default its owner is asked to change it."""
    name = validate_username(username)
    role = _clean_role(role)
    groups = _clean_groups(role, groups)
    validate_password(password, name)
    password_hash = hash_password(password)  # outside the lock: it is the slow part
    with _LOCK:
        doc = _read_strict()
        if name in doc["users"]:
            raise UserError(f"用户名「{name}」已存在")
        now = _now_iso()
        doc["users"][name] = {
            "display_name": (display_name or "").strip()[:32],
            "role": role,
            "groups": groups,
            "password_hash": password_hash,
            "disabled": False,
            "session_key": secrets.token_hex(16),
            "must_change_password": bool(must_change),
            "created_at": now,
            "created_by": by,
            "updated_at": now,
            "password_changed_at": now,
        }
        _save(doc)
    return name


def update_user(username: str, *, display_name: str, role: str, groups: list[str]) -> dict:
    """Change name / role / groups. Returns {field: [old, new]} for what
    changed. A role change signs the user out everywhere: a page left open
    by a demoted admin would otherwise keep polling admin-only partials and
    collect a 403 toast on every tick, instead of one clean trip to /login.
    Groups are read live on every request and need no sign-out."""
    role = _clean_role(role)
    groups = _clean_groups(role, groups)
    display_name = (display_name or "").strip()[:32]
    with _LOCK:
        doc = _read_strict()
        rec = _existing(doc, normalize_username(username))
        old = _normalize(rec)
        diff: dict[str, list] = {}
        if old["display_name"] != display_name:
            diff["display_name"] = [old["display_name"], display_name]
        if old["role"] != role:
            diff["role"] = [old["role"], role]
            rec["session_key"] = secrets.token_hex(16)
        if old["groups"] != groups:
            diff["groups"] = [old["groups"], groups]
        if diff:
            rec.update(display_name=display_name, role=role, groups=groups, updated_at=_now_iso())
            _save(doc)
    return diff


def set_password(username: str, password: str, *, must_change: bool = False) -> None:
    """New password; every existing session of the user ends."""
    name = normalize_username(username)
    validate_password(password, name)
    password_hash = hash_password(password)
    with _LOCK:
        doc = _read_strict()
        rec = _existing(doc, name)
        now = _now_iso()
        rec.update(
            password_hash=password_hash,
            session_key=secrets.token_hex(16),
            must_change_password=bool(must_change),
            updated_at=now,
            password_changed_at=now,
        )
        _save(doc)


def set_disabled(username: str, disabled: bool) -> None:
    with _LOCK:
        doc = _read_strict()
        rec = _existing(doc, normalize_username(username))
        rec["disabled"] = bool(disabled)
        if disabled:
            rec["session_key"] = secrets.token_hex(16)
        rec["updated_at"] = _now_iso()
        _save(doc)


def revoke_sessions(username: str) -> None:
    """强制下线: every cookie issued so far stops working."""
    with _LOCK:
        doc = _read_strict()
        rec = _existing(doc, normalize_username(username))
        rec["session_key"] = secrets.token_hex(16)
        rec["updated_at"] = _now_iso()
        _save(doc)


def delete_user(username: str) -> dict:
    """Remove the user; returns the removed record (normalized)."""
    with _LOCK:
        doc = _read_strict()
        name = normalize_username(username)
        rec = _normalize(_existing(doc, name))
        del doc["users"][name]
        _save(doc)
    return rec


# ---------------------------------------------------------------------------
# One-time import of the old WEB_USERS env table
# ---------------------------------------------------------------------------

def parse_web_users(raw: str) -> list[tuple[str, str, str]]:
    """WEB_USERS="user1:pass1:group_a,user2:pass2:group_b" → [(user, pass, group)].

    Split from both ends so the password itself may contain colons. A bad
    entry raises RuntimeError naming its position — never echoing it, the
    entry holds a password.
    """
    entries: list[tuple[str, str, str]] = []
    seen = {env_admin_name()}
    for i, entry in enumerate((raw or "").split(",")):
        entry = entry.strip()
        if not entry:
            continue
        username, sep1, rest = entry.partition(":")
        password, sep2, group = rest.rpartition(":")
        if not sep1 or not sep2 or not username.strip() or not password.strip() or not group.strip():
            raise RuntimeError(f"WEB_USERS 第 {i + 1} 项格式错误（应为 用户名:密码:分组key）")
        username, password, group = username.strip(), password.strip(), group.strip()
        if username in seen:
            raise RuntimeError(f"WEB_USERS 用户名重复：{username!r}")
        seen.add(username)
        entries.append((username, password, group))
    return entries


def migrate_from_env() -> int:
    """Import WEB_USERS into users.yaml once, when the file does not exist yet.

    Runs from the web app's startup (off the event loop: one hash per user),
    never at import or on a read — importing web.app happens in the deploy
    smoke test and in the test suite, neither of which may write the real
    file. Imported users keep their password and get an empty session_key,
    which signs cookies exactly as before, so nobody is logged out by the
    deploy. They are 只读, like the group users they were, and flagged to
    change a password that sat in plain text in .env.
    """
    raw = os.getenv("WEB_USERS", "").strip()
    if not raw:
        return 0
    if _USERS_FILE.exists():
        logger.warning(
            "WEB_USERS 已停用：控制台用户在 config/users.yaml 里管理，"
            "请从 .env 删除 WEB_USERS（其中是明文口令）"
        )
        return 0
    try:
        entries = parse_web_users(raw)
    except RuntimeError as exc:
        logger.error(f"WEB_USERS 未导入：{exc}")
        return 0

    imported: list[str] = []
    with _LOCK:
        if _USERS_FILE.exists():
            return 0
        now = _now_iso()
        users: dict[str, dict] = {}
        for username, password, group in entries:
            name = normalize_username(username)
            if not _USERNAME_RE.match(name) or name in RESERVED_NAMES:
                logger.error(f"WEB_USERS 中的用户名 {username!r} 不符合规则，未导入；请在「用户」页重新建立")
                continue
            users[name] = {
                "display_name": "",
                "role": "viewer",
                "groups": [group],
                "password_hash": hash_password(password),
                "disabled": False,
                "session_key": "",
                "must_change_password": True,
                "created_at": now,
                "created_by": "WEB_USERS 导入",
                "updated_at": now,
                "password_changed_at": now,
            }
            imported.append(name)
        if not users:
            return 0
        _save({"version": 1, "users": users})
    logger.info(f"已把 WEB_USERS 中的 {len(imported)} 个用户导入 config/users.yaml；可从 .env 删除 WEB_USERS")
    audit.record("系统", "user.import", via="system", detail={"users": imported})
    return len(imported)
