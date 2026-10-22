"""Operation log (操作日志): who did what, and when — append-only JSONL.

    data/control/audit/audit-YYYY-MM.jsonl     one file per Beijing month

Several people now share the console, and some of what they can do is hard
to take back: a client's platform password changed, a company deleted, a
notification webhook replaced, notices released from the server. This file
is how an administrator finds out afterwards who did it. Writers are the web
console (every state-changing route, logins, downloads, CSV exports) and
scripts/archive_release.py (run over ssh by the laptop's archive tool).

One line per action:

    {"ts": "<UTC ISO>", "actor": "alice", "actor_name": "张三", "role": "operator",
     "via": "cookie", "ip": "203.0.113.9", "action": "account.edit",
     "company": "甲公司", "target": "acct01", "ok": true,
     "detail": {"password_changed": true}}

Empty optional fields are left out; readers use .get().

Rules every writer relies on:

- record() never raises and never prints. A failed audit write must not
  fail the action it describes, and archive_release.py's stdout is a JSON
  protocol its caller parses — loguru's CLI sink is stderr.
- No secret ever lands here. Callers pass flags (password_changed: true,
  webhook_changed: true), never values; _scrub() is the backstop that masks
  any non-boolean value whose key looks like a credential.
- Appends need no lock: each line is one os.write() on an O_APPEND
  descriptor, which POSIX places atomically at the end of the file, so the
  web process and an archive release cannot interleave half-lines. Readers
  sort by "ts", so two processes landing a few milliseconds out of order
  does not matter.
- Nothing is pruned. A busy day is a few hundred lines (~100 KB); a year
  fits in tens of MB on a disk the attachments already dominate, and
  `python -m archive pull` keeps a copy on the laptop.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

_ROOT = Path(__file__).parent.parent
_AUDIT_DIR = _ROOT / "data" / "control" / "audit"

# Month boundaries follow the operators' calendar, like daily_stats.
_SHANGHAI_TZ = timezone(timedelta(hours=8))

# Strings are capped so a pasted essay cannot bloat the log.
_MAX_STR = 200
_MAX_LIST = 50

# Backstop only — callers must not pass secrets in the first place.
_SECRET_KEY_RE = re.compile(r"pass|secret|webhook|token|key", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def month_file(ts: datetime) -> Path:
    """The file a line stamped `ts` belongs to (Beijing month)."""
    return _AUDIT_DIR / f"audit-{ts.astimezone(_SHANGHAI_TZ):%Y-%m}.jsonl"


def files() -> list[Path]:
    """All month files, oldest first (the zero-padded name sorts by date)."""
    try:
        return sorted(_AUDIT_DIR.glob("audit-*.jsonl"))
    except OSError:
        return []


def _scrub(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {str(k)[:_MAX_STR]: _scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_scrub(v, key) for v in list(value)[:_MAX_LIST]]
    if (
        key
        and _SECRET_KEY_RE.search(key)
        and not isinstance(value, bool)
        and value not in (None, "")
    ):
        return "***"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_MAX_STR]


def record(
    actor: str,
    action: str,
    *,
    actor_name: str = "",
    role: str = "",
    via: str = "",
    ip: str = "",
    target: str = "",
    company: str = "",
    ok: bool = True,
    detail: dict | None = None,
) -> None:
    """Append one line to this month's file. Never raises."""
    try:
        ts = _now()
        entry: dict[str, Any] = {"ts": ts.isoformat(), "actor": str(actor)[:_MAX_STR]}
        for name, value in (
            ("actor_name", actor_name), ("role", role), ("via", via), ("ip", ip),
        ):
            if value:
                entry[name] = str(value)[:_MAX_STR]
        entry["action"] = str(action)[:_MAX_STR]
        if company:
            entry["company"] = str(company)[:_MAX_STR]
        if target:
            entry["target"] = str(target)[:_MAX_STR]
        entry["ok"] = bool(ok)
        if detail:
            entry["detail"] = _scrub(detail)

        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        _AUDIT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(month_file(ts), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception as exc:  # noqa: BLE001 — the action itself already happened
        logger.warning(f"操作日志写入失败（非致命）action={action}: {exc!r}")
