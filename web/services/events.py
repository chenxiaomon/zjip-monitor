"""Read and parse events from data/events.jsonl."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

_EVENTS_FILE = Path(__file__).parent.parent.parent / "data" / "events.jsonl"


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
    if not _EVENTS_FILE.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        lines = _EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    events: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if account and event.get("account") != account:
            continue
        ts_str = event.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
        except ValueError:
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
