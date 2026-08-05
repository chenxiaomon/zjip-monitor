"""Read and parse events from data/events.jsonl.

The parsed file is cached on (mtime, size) — the dashboard re-reads it on
every render and the file only ever changes by appends or rotation.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_EVENTS_FILE = Path(__file__).parent.parent.parent / "data" / "events.jsonl"

_cache_lock = threading.Lock()
# (mtime, size) → parsed events with pre-parsed timestamps, file order
_cache: tuple[tuple[float, int], list[tuple[datetime | None, dict]]] | None = None


def _load_parsed() -> list[tuple[datetime | None, dict]]:
    global _cache
    try:
        st = _EVENTS_FILE.stat()
    except OSError:
        return []
    key = (st.st_mtime, st.st_size)
    with _cache_lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1]
    try:
        lines = _EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    parsed: list[tuple[datetime | None, dict]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts: datetime | None = None
        try:
            ts = datetime.fromisoformat(event.get("timestamp", ""))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            ts = None
        parsed.append((ts, event))
    with _cache_lock:
        _cache = (key, parsed)
    return parsed


def read_events(
    account: str | None = None,
    hours: int = 24,
    limit: int = 200,
) -> list[dict]:
    """Read events newest-first from events.jsonl.

    Args:
        account: Filter by account username (None = all accounts).
        hours:   Return only events from the last N hours.
        limit:   Max number of events to return.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    events: list[dict] = []
    for ts, event in reversed(_load_parsed()):
        if ts is None or ts < cutoff:
            continue
        if account and event.get("account") != account:
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return events


def read_change_rows(
    account: str | None = None,
    hours: int = 24 * 30,
    limit: int = 300,
) -> list[dict]:
    """Flatten events into individual change rows for the changes page."""
    from src.scraper import decode_status

    events = read_events(account=account, hours=hours, limit=limit)
    rows: list[dict] = []
    for event in events:
        base = {
            "account": event.get("account", ""),
            "company": event.get("company", ""),
            "timestamp": event.get("timestamp", ""),
        }
        for c in event.get("changed", []):
            rows.append({
                **base,
                "type": "changed",
                "reg_no": c.get("reg_no", ""),
                "name": c.get("name", ""),
                "old_status_code": c.get("old_status_code"),
                "new_status_code": c.get("new_status_code"),
                "old_status": c.get("old_status", ""),
                "new_status": c.get("new_status", ""),
            })
        for a in event.get("added", []):
            code = a.get("dataRegAuditStatus")
            rows.append({
                **base,
                "type": "added",
                "reg_no": a.get("dataRegNo", ""),
                "name": a.get("dataRegName", ""),
                "old_status_code": None,
                "new_status_code": code,
                "old_status": "",
                "new_status": decode_status(code),
            })
        for r in event.get("removed", []):
            rows.append({
                **base,
                "type": "removed",
                "reg_no": r.get("dataRegNo", ""),
                "name": r.get("dataRegName", ""),
                "old_status_code": r.get("dataRegAuditStatus"),
                "new_status_code": None,
                "old_status": decode_status(r.get("dataRegAuditStatus")),
                "new_status": "",
            })
    return rows
