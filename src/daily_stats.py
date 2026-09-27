"""Per-day status counts per company, for the dashboard's trend lines.

data/control/daily_stats.json:

    {"version": 1,
     "days": {"2026-09-27": {"at": "<ISO UTC>",
                             "companies": {"甲公司": {"2026": {"7": 3, "2": 1},
                                                    "none": {"1": 2}}}}}}

- One entry per Beijing date; a later round the same day replaces the earlier one.
- Counts use the dashboard's rules: the application year is taken in
  Asia/Shanghai ("none" when the record has no usable apply time) and a
  record whose status is not an integer is not counted at all. Summing every
  bucket therefore reproduces the dashboard's "all years" totals, and one
  year's bucket reproduces that year's.
- Keyed by company, like the snapshots it is derived from, so a backfill
  needs nothing but snapshots; the web side filters by the viewer's
  companies, which also hides companies whose account was deleted.

Written by src.main._finalize_run at the end of every round, under the
global run lock, and by scripts/backfill_daily_stats.py. Writing never
raises: this runs in the finalizer, ahead of the lock release. Reading is
stat-cached on (mtime_ns, size) like diskstat.read_stats; the returned
objects are shared and must not be mutated.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from loguru import logger

from .fsutil import atomic_write_text
from .scraper import int_or_none

_ROOT = Path(__file__).parent.parent
_STATS_FILE = _ROOT / "data" / "control" / "daily_stats.json"

KEEP_DAYS = 400
NO_YEAR = "none"
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_read_lock = threading.Lock()
_read_cache: tuple[tuple[int, int], dict[str, Any]] | None = None  # ((mtime_ns, size), days)


def local_date(ts: datetime | None = None) -> date:
    """The Beijing calendar date of `ts` (default: now)."""
    ts = ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(_SHANGHAI_TZ).date()


def _year_key(ms: Any) -> str:
    ms = int_or_none(ms)
    if not ms:
        return NO_YEAR
    try:
        return str(datetime.fromtimestamp(ms / 1000, _SHANGHAI_TZ).year)
    except (ValueError, OverflowError, OSError):
        return NO_YEAR  # an out-of-range timestamp must not sink the whole count


def count_records(records: Any) -> dict[str, dict[str, int]]:
    """One snapshot's records → {year_key: {status_code: n}} (keys are strings)."""
    out: dict[str, dict[str, int]] = {}
    for r in records if isinstance(records, list) else []:
        if not isinstance(r, dict):
            continue
        code = int_or_none(r.get("dataRegAuditStatus"))
        if code is None:
            continue
        bucket = out.setdefault(_year_key(r.get("dataRegApplyTime")), {})
        bucket[str(code)] = bucket.get(str(code), 0) + 1
    return out


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _clean_days(raw: Any) -> dict[str, dict]:
    days = raw.get("days") if isinstance(raw, dict) else None
    if not isinstance(days, dict):
        return {}
    return {
        k: v for k, v in days.items()
        if isinstance(k, str) and _DATE_RE.match(k) and isinstance(v, dict)
        and isinstance(v.get("companies"), dict)
    }


def _load_for_update() -> dict[str, dict]:
    """A private, mutable copy of the stored days (never the shared cache).

    A file that exists but cannot be parsed is moved aside rather than
    silently overwritten, so its history can still be recovered by hand.
    """
    try:
        text = _STATS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        return _clean_days(json.loads(text))
    except ValueError:
        aside = _STATS_FILE.with_name(f"{_STATS_FILE.name}.bad-{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
        _STATS_FILE.replace(aside)
        logger.warning(f"daily_stats.json 无法解析，已另存为 {aside.name}，从空白重新记录")
        return {}


def _trim(days: dict[str, dict], today: date) -> None:
    cutoff = (today - timedelta(days=KEEP_DAYS - 1)).isoformat()
    for k in [k for k in days if k < cutoff]:
        del days[k]


def _write(days: dict[str, dict]) -> None:
    global _read_cache
    payload = {"version": 1, "days": dict(sorted(days.items()))}
    atomic_write_text(_STATS_FILE, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    with _read_lock:
        _read_cache = None  # same-process readers must see this write immediately


def read_days() -> dict[str, dict]:
    """{date: {"at", "companies"}}, stat-cached; {} when missing or unreadable."""
    global _read_cache
    try:
        st = _STATS_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _read_lock:
        if _read_cache is not None and _read_cache[0] == key:
            return _read_cache[1]
    try:
        days = _clean_days(json.loads(_STATS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    with _read_lock:
        _read_cache = (key, days)
    return days


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def record_round(
    accounts: Iterable[Any],
    load_snapshot: Callable[[str], dict | None],
    *,
    now: datetime | None = None,
) -> bool:
    """Count every account's latest snapshot into today's entry. Never raises.

    A company whose snapshot cannot be read right now keeps the counts it
    last had, so a transient read error does not show up as a dip.
    Returns True when an entry was written.
    """
    try:
        now = now or datetime.now(timezone.utc)
        today = local_date(now)
        days = _load_for_update()
        previous: dict[str, Any] = {}
        for k in sorted(days):
            if k <= today.isoformat():
                previous = days[k]["companies"]
        companies: dict[str, dict] = {}
        for acc in accounts:
            company = getattr(acc, "company", None)
            if not isinstance(company, str) or not company or company in companies:
                continue
            snap = load_snapshot(company)
            if isinstance(snap, dict):
                companies[company] = count_records(snap.get("records"))
            elif company in previous:
                companies[company] = previous[company]
        if not companies:
            return False  # nothing scanned yet: leave the file alone
        days[today.isoformat()] = {"at": now.isoformat(), "companies": companies}
        _trim(days, today)
        _write(days)
        return True
    except Exception as exc:  # noqa: BLE001 — bookkeeping is never fatal
        logger.warning(f"daily_stats.json 写入失败（非致命）: {exc!r}")
        return False


def merge_days(new_days: dict[str, dict], *, dry_run: bool = False, today: date | None = None) -> int:
    """Add backfilled {date: {"at", "companies"}} entries for dates not yet
    recorded. Existing dates are never overwritten. Returns how many dates
    were (or, with dry_run, would be) added."""
    days = _load_for_update()
    added = 0
    for k, entry in sorted(new_days.items()):
        if k in days or not _DATE_RE.match(k):
            continue
        days[k] = entry
        added += 1
    if added and not dry_run:
        _trim(days, today or local_date())
        _write(days)
    return added


# ---------------------------------------------------------------------------
# Readers for the dashboard
# ---------------------------------------------------------------------------

def series(
    companies: Iterable[str],
    year: int | None,
    days: int,
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Daily 登记总数 / 登记成功 (7) / 待补正 (2) over the last `days` days,
    summed over `companies`, ascending by date; days without an entry are
    simply absent."""
    wanted = set(companies)
    today = today or local_date()
    start = (today - timedelta(days=max(1, days) - 1)).isoformat()
    end = today.isoformat()
    year_key = None if year is None else str(year)
    out = []
    for k, entry in sorted(read_days().items()):
        if not (start <= k <= end):
            continue
        total = success = pending = 0
        for company, buckets in entry["companies"].items():
            if company not in wanted or not isinstance(buckets, dict):
                continue
            for ykey, codes in buckets.items():
                if year_key is not None and ykey != year_key:
                    continue
                if not isinstance(codes, dict):
                    continue
                for code, n in codes.items():
                    n = int_or_none(n) or 0
                    total += n
                    if code == "7":
                        success += n
                    elif code == "2":
                        pending += n
        out.append({"date": k, "total": total, "success": success, "pending": pending})
    return out


def first_date() -> str | None:
    """The earliest date on record, for the dashboard's "since" note."""
    days = read_days()
    return min(days) if days else None
