"""Durable per-account scan/login status — data/control/accounts_status.json.

Written by BOTH the daemon/CLI (scan outcomes in src/main.py) and the web
process (relogin/edit routes), so unlike state.json — whose writers are
serialized by the global run lock — this file needs a cross-process
filelock around every read-modify-write. Readers get an mtime-cached view.

Every public function swallows its own errors: status bookkeeping must never
break a scan or a web request.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock
from loguru import logger

from .fsutil import atomic_write_text
from .settings_cache import get_settings

_ROOT = Path(__file__).parent.parent
_STATUS_FILE = _ROOT / "data" / "control" / "accounts_status.json"
_LOCK_TIMEOUT_SECONDS = 10

# 连续登录失败达到该次数后暂停定时巡检登录（防平台锁号）；0/负数 = 不暂停
_DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

# Durable store caps error text; the web's in-memory task dict keeps more.
_ERROR_MAX_CHARS = 500

_read_lock = threading.Lock()
_read_cache: tuple[float, dict] | None = None  # (mtime, parsed)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock() -> FileLock:
    _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(_STATUS_FILE) + ".lock", timeout=_LOCK_TIMEOUT_SECONDS)


def _load_unlocked() -> dict:
    try:
        data = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"accounts_status.json unreadable ({exc!r}) — starting fresh")
    return {"version": 1, "accounts": {}}


def _mutate(fn):
    """Read-modify-write under the cross-process lock; returns fn's result
    only AFTER the write persisted (callers act on flags — e.g. send a pause
    notification — so the flags must describe durable state, not intent)."""
    global _read_cache
    with _lock():
        data = _load_unlocked()
        out = fn(data)
        atomic_write_text(_STATUS_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    with _read_lock:
        _read_cache = None  # same-process readers must see this write immediately
    return out


def _entry(data: dict, username: str, company: str | None = None) -> dict:
    entry = data["accounts"].setdefault(username, {
        "company": company or "",
        "status": None,
        "error": None,
        "last_attempt_at": None,
        "last_success_at": None,
        "fetched": None,
        "reported_total": None,
        "complete": None,
        "last_login_ok_at": None,
        "consecutive_login_failures": 0,
        "login_paused": False,
        "failure_notified_at": None,
    })
    if company:
        entry["company"] = company
    return entry


def _max_failures() -> int:
    raw = get_settings().get("login", {}).get(
        "max_consecutive_failures", _DEFAULT_MAX_CONSECUTIVE_FAILURES
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_CONSECUTIVE_FAILURES


def record_scan(
    username: str,
    company: str = "",
    *,
    ok: bool,
    error: str | None = None,
    kind: str | None = None,
    fetch_meta: dict | None = None,
) -> None:
    """Persist one scan attempt's outcome (called from main.py on every path)."""
    try:
        def fn(data: dict) -> dict:
            e = _entry(data, username, company)
            now = _now_iso()
            e["last_attempt_at"] = now
            if ok:
                complete = (fetch_meta or {}).get("complete", True)
                e["status"] = "ok" if complete else "partial"
                e["error"] = None
                e["last_success_at"] = now
            else:
                e["status"] = kind or "error"
                e["error"] = (error or "")[:_ERROR_MAX_CHARS] or None
            if fetch_meta:
                e["fetched"] = fetch_meta.get("fetched_count")
                e["reported_total"] = fetch_meta.get("reported_total")
                e["complete"] = fetch_meta.get("complete")
            return e

        _mutate(fn)
    except Exception as exc:  # noqa: BLE001 — bookkeeping is never fatal
        logger.warning(f"[{username}] record_scan failed (non-fatal): {exc!r}")


def record_login(
    username: str,
    company: str = "",
    *,
    ok: bool,
    error: str | None = None,
) -> dict[str, bool]:
    """Track the login-failure streak for lockout protection.

    Call ONLY for attempts against the *persisted* password (a modal test of
    an unsaved password must not touch the streak). Returns flags for the
    notifier: streak_started (0→1) and just_paused (crossed the threshold).
    """
    try:
        def fn(data: dict) -> dict:
            flags = {"streak_started": False, "just_paused": False}
            e = _entry(data, username, company)
            if ok:
                e["consecutive_login_failures"] = 0
                e["login_paused"] = False
                e["failure_notified_at"] = None
                e["last_login_ok_at"] = _now_iso()
            else:
                prev = int(e.get("consecutive_login_failures") or 0)
                e["consecutive_login_failures"] = prev + 1
                e["error"] = (error or "")[:_ERROR_MAX_CHARS] or e.get("error")
                flags["streak_started"] = prev == 0
                limit = _max_failures()
                if limit > 0 and prev + 1 >= limit and not e.get("login_paused"):
                    e["login_paused"] = True
                    flags["just_paused"] = True
            return flags

        return _mutate(fn)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[{username}] record_login failed (non-fatal): {exc!r}")
        # Nothing persisted → report nothing happened, so a broken store can
        # neither spam notifications nor fake a streak reset.
        return {"streak_started": False, "just_paused": False}


def record_password_changed(username: str) -> None:
    """Reset the failure streak and un-pause after a password edit."""
    try:
        def fn(data: dict) -> dict:
            e = _entry(data, username)
            e["consecutive_login_failures"] = 0
            e["login_paused"] = False
            e["failure_notified_at"] = None
            return e

        _mutate(fn)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[{username}] record_password_changed failed (non-fatal): {exc!r}")


def forget_account(username: str) -> None:
    """Drop an account's entry (called when the account is deleted)."""
    try:
        def fn(data: dict) -> dict:
            return data["accounts"].pop(username, {})

        _mutate(fn)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[{username}] forget_account failed (non-fatal): {exc!r}")


def get_all() -> dict[str, dict[str, Any]]:
    """stat-cached read of every account's status ({} on any problem).

    Keyed on (mtime_ns, size) so back-to-back cross-process writes within one
    mtime tick cannot pin a stale view; same-process writes invalidate the
    cache directly in _mutate."""
    global _read_cache
    try:
        st = _STATUS_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _read_lock:
        if _read_cache is not None and _read_cache[0] == key:
            return _read_cache[1]
    accounts = _load_unlocked().get("accounts", {})
    with _read_lock:
        _read_cache = (key, accounts)
    return accounts


def get_status(username: str) -> dict[str, Any] | None:
    return get_all().get(username)


def is_login_paused(username: str) -> bool:
    entry = get_all().get(username)
    return bool(entry and entry.get("login_paused"))
