"""Read and parse change events from data/events.jsonl (+ rotated files).

src.main rotates the log to `events-<UTC timestamp>.jsonl` once it passes a
size threshold. Reading only the live file — as this module used to — made the
whole 30-day history on the 变更 page and the 通知 page vanish the moment a
rotation happened, even though the data was still sitting on disk. Rotated
files are therefore part of the read path, newest first.

Each file is parsed at most once per (mtime, size); the files only ever change
by appends or by being rotated away.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_EVENTS_FILE = _DATA_DIR / "events.jsonl"
_ROTATED_GLOB = "events-*.jsonl"

_cache_lock = threading.Lock()
# path → ((mtime, size), parsed events with pre-parsed timestamps, file order)
_cache: dict[str, tuple[tuple[float, int], list[tuple[datetime | None, dict]]]] = {}


def _event_files() -> list[Path]:
    """Newest first: the live log, then rotated logs by descending timestamp.

    Rotated names carry a zero-padded UTC stamp, so the lexicographic order is
    the chronological one.
    """
    files = [_EVENTS_FILE] if _EVENTS_FILE.exists() else []
    try:
        files += sorted(_DATA_DIR.glob(_ROTATED_GLOB), reverse=True)
    except OSError:
        pass
    with _cache_lock:  # forget files that have been pruned away
        live = {str(f) for f in files}
        for stale in [k for k in _cache if k not in live]:
            del _cache[stale]
    return files


def _load_parsed(path: Path) -> list[tuple[datetime | None, dict]]:
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return []
    stamp = (st.st_mtime, st.st_size)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
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
        _cache[key] = (stamp, parsed)
    return parsed


def has_changes(event: dict) -> bool:
    return bool(event.get("changed") or event.get("added") or event.get("removed"))


def read_events(
    account: str | None = None,
    hours: int = 24,
    limit: int | None = 200,
    accounts: set[str] | None = None,
    *,
    with_changes: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Read events newest-first, across the live and rotated logs.

    Args:
        account:  Keep only this account's events (None = all).
        hours:    Only events from the last N hours (ignored when `since` is given).
        limit:    Max number of events to return (None = no cap).
        accounts: Visibility allow-list (None = unrestricted). Applied BEFORE
                  `limit`, so a group user's own events cannot be crowded out
                  of the window by busier accounts they are not allowed to see.
        with_changes: Skip events that record no change — every successful
                  scan writes one, and only about one in ten carries a change,
                  so a cap counted in raw events covered days, not weeks.
        since/until: An explicit window instead of `hours`.
    """
    cutoff = since or (datetime.now(timezone.utc) - timedelta(hours=hours))
    events: list[dict] = []
    for path in _event_files():
        for ts, event in reversed(_load_parsed(path)):
            if ts is None:
                continue
            if ts < cutoff:
                # Entries are appended in order and files are walked newest
                # first, so everything from here on is older than the window.
                return events
            if until is not None and ts > until:
                continue
            who = event.get("account")
            if account and who != account:
                continue
            if accounts is not None and who not in accounts:
                continue
            if with_changes and not has_changes(event):
                continue
            events.append(event)
            if limit is not None and len(events) >= limit:
                return events
    return events


def read_change_rows(
    account: str | None = None,
    hours: int = 24 * 30,
    limit: int | None = None,
    accounts: set[str] | None = None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    reg_no: str | None = None,
) -> list[dict]:
    """Flatten events into individual change rows, newest first.

    `limit` caps ROWS (None = every row in the window); events without
    changes never count, so the window really is the window. `reg_no`
    keeps only that record's rows (the detail drawer's history)."""
    from src.scraper import decode_status

    events = read_events(
        account=account, hours=hours, limit=None, accounts=accounts,
        with_changes=True, since=since, until=until,
    )
    rows: list[dict] = []
    for event in events:
        first_row = len(rows)
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
        if reg_no is not None:
            rows[first_row:] = [r for r in rows[first_row:] if r["reg_no"] == reg_no]
        if limit is not None and len(rows) >= limit:
            return rows[:limit]
    return rows
