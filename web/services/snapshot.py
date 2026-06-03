"""Read snapshots from data/snapshots/ (flat naming: {company}_{YYYY-MM-DD-HHMM}.json)."""

from __future__ import annotations

import json
import re
from pathlib import Path

_SNAPSHOTS_DIR = Path(__file__).parent.parent.parent / "data" / "snapshots"
_DATE_RE = re.compile(r"^(.+)_(\d{4}-\d{2}-\d{2}-\d{4})$")


def _safe_name(company: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in company)


def get_latest_snapshot(company: str) -> dict | None:
    if not _SNAPSHOTS_DIR.exists():
        return None
    prefix = _safe_name(company)
    files = sorted(_SNAPSHOTS_DIR.glob(f"{prefix}_*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def get_all_latest_snapshots() -> list[dict]:
    """Return the latest snapshot for each unique company (by filename prefix)."""
    if not _SNAPSHOTS_DIR.exists():
        return []
    by_prefix: dict[str, Path] = {}
    for f in sorted(_SNAPSHOTS_DIR.glob("*.json")):
        m = _DATE_RE.match(f.stem)
        if m:
            by_prefix[m.group(1)] = f  # alphabetically later = more recent
    results = []
    for path in by_prefix.values():
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return results


def list_snapshot_dates() -> list[str]:
    """Return all dates (YYYY-MM-DD) that have at least one snapshot, newest first."""
    if not _SNAPSHOTS_DIR.exists():
        return []
    dates: set[str] = set()
    for f in _SNAPSHOTS_DIR.glob("*.json"):
        m = _DATE_RE.match(f.stem)
        if m:
            dates.add(m.group(2)[:10])
    return sorted(dates, reverse=True)


def get_snapshots_by_date(date_str: str) -> list[dict]:
    """Return each company's latest snapshot from the given date (YYYY-MM-DD)."""
    if not _SNAPSHOTS_DIR.exists():
        return []
    by_company: dict[str, Path] = {}
    for f in _SNAPSHOTS_DIR.glob("*.json"):
        m = _DATE_RE.match(f.stem)
        if m and m.group(2).startswith(date_str):
            company = m.group(1)
            if company not in by_company or f.name > by_company[company].name:
                by_company[company] = f
    results = []
    for path in by_company.values():
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return results
