#!/usr/bin/env python3
"""Seed data/control/daily_stats.json from the snapshots already on disk.

    python scripts/backfill_daily_stats.py [--dry-run]

The dashboard's trend lines normally start on the day this feature was
deployed; the snapshots the server still keeps (retention.snapshots_keep_per_company
per company, about two weeks at two rounds a day) can fill in the days before.

- Days are Beijing dates; snapshot file names carry UTC stamps.
- A company's count for a day is its last snapshot on or before that day, so
  a day on which its scan failed carries the previous numbers instead of
  showing up as a dip.
- The window starts where every company still has coverage: a company with
  a full set of snapshots may have had older ones pruned, so its earliest
  file is a coverage limit; a company with fewer files has its whole history
  on disk and does not limit the window.
- Dates already recorded are never overwritten.

Holds the global run lock so no round writes the file at the same time.
Output: one JSON line with counts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from src import control, daily_stats  # noqa: E402
from src.logging_setup import setup_logging  # noqa: E402
from src.runtime_settings import retention_settings  # noqa: E402

_SNAPSHOTS_DIR = ROOT / "data" / "snapshots"
# Same shape src/main.py writes and prunes by: "<safe company>_YYYY-MM-DD-HHMM.json"
_NAME_RE = re.compile(r"^(?P<prefix>.+)_(?P<ts>\d{4}-\d{2}-\d{2}-\d{4})$")


def _snapshot_files() -> dict[str, list[tuple[datetime, Path]]]:
    groups: dict[str, list[tuple[datetime, Path]]] = {}
    if not _SNAPSHOTS_DIR.is_dir():
        return groups
    for f in _SNAPSHOTS_DIR.glob("*.json"):
        m = _NAME_RE.match(f.stem)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d-%H%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        groups.setdefault(m.group("prefix"), []).append((ts, f))
    for files in groups.values():
        files.sort()
    return groups


def backfill(*, dry_run: bool = False, heartbeat: Callable[[], None] = lambda: None,
             today: date | None = None) -> dict:
    counts = {"companies": 0, "files_read": 0, "unreadable": 0,
              "window_start": None, "days_found": 0, "days_added": 0, "dry_run": dry_run}
    groups = _snapshot_files()
    if not groups:
        return counts
    today = today or daily_stats.local_date()
    keep = retention_settings().snapshots_keep_per_company

    first_day = {p: daily_stats.local_date(files[0][0]) for p, files in groups.items()}
    limits = [first_day[p] for p, files in groups.items() if len(files) >= keep]
    start = max(limits) if limits else min(first_day.values())
    end = min(today, max(daily_stats.local_date(files[-1][0]) for files in groups.values()))
    counts["window_start"] = start.isoformat()
    if start > end:
        return counts

    parsed: dict[Path, tuple[str, dict] | None] = {}

    def counts_of(path: Path) -> tuple[str, dict] | None:
        if path not in parsed:
            heartbeat()
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                company = payload.get("company") if isinstance(payload, dict) else None
                if not isinstance(company, str) or not company:
                    raise ValueError("no company")
                parsed[path] = (company, daily_stats.count_records(payload.get("records")))
                counts["files_read"] += 1
            except (OSError, ValueError, AttributeError):
                parsed[path] = None
                counts["unreadable"] += 1
        return parsed[path]

    new_days: dict[str, dict] = {}
    day = start
    while day <= end:
        companies: dict[str, dict] = {}
        at: datetime | None = None
        for files in groups.values():
            # The last snapshot taken on or before this Beijing day.
            chosen = None
            for ts, path in files:
                if daily_stats.local_date(ts) > day:
                    break
                chosen = (ts, path)
            if chosen is None:
                continue
            got = counts_of(chosen[1])
            if got is None:
                continue
            company, buckets = got
            companies[company] = buckets
            at = chosen[0] if at is None or chosen[0] > at else at
        if companies:
            new_days[day.isoformat()] = {"at": at.isoformat(), "companies": companies, "source": "backfill"}
        day += timedelta(days=1)

    counts["companies"] = len({c for entry in new_days.values() for c in entry["companies"]})
    counts["days_found"] = len(new_days)
    counts["days_added"] = daily_stats.merge_days(new_days, dry_run=dry_run, today=today)
    return counts


def main(argv: list[str] | None = None) -> int:
    setup_logging("cli")
    parser = argparse.ArgumentParser(description="用服务器上现存的快照回填仪表盘趋势（data/control/daily_stats.json）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = parser.parse_args(argv)

    ctx = control.try_acquire(kind="backfill_daily_stats", trigger="cli")
    if ctx is None:
        print(json.dumps({"error": "另一巡检进行中，稍后再试"}, ensure_ascii=False))
        return 2
    status, error = "done", None
    try:
        counts = backfill(dry_run=args.dry_run, heartbeat=ctx.cancel.heartbeat)
        logger.info(f"每日统计回填：{counts}")
        print(json.dumps(counts, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        status, error = "error", f"{type(exc).__name__}: {exc}"
        print(json.dumps({"error": error}, ensure_ascii=False))
        return 1
    finally:
        ctx.finish(status, error=error)


if __name__ == "__main__":
    sys.exit(main())
