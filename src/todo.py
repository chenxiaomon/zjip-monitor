"""待办 for the console and the ops banner.

Two lists the operator otherwise has to dig out of PDFs:

    corrections  records at 待补正 with the deadline printed on the current
                 补正通知书 — miss it and the platform treats the application
                 as withdrawn (视为撤回)
    renewals     registered records (登记成功) whose certificate, per the
                 登记公告通知书, expires within RENEWAL_WINDOW_DAYS — the
                 renewal window opens six months before expiry

Sources: the latest snapshot per company (what the platform says the status
is) and the facts src/attachments.py stored in each record's .sync.json when
it downloaded the notice (src/pdftext). A record whose notice was never parsed
— released to the laptop before facts existed, or a PDF without a text layer —
still appears, with the deadline marked unknown, so nothing is silently missed.

Reads are cached on (mtime, size): the dashboard calls this on every refresh
and the banner on every page.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import attachments
from .config_loader import Account
from .scraper import int_or_none

_SHANGHAI_TZ = timezone(timedelta(hours=8))
_SNAPSHOT_RE = re.compile(r"^(?P<prefix>.+)_(\d{4}-\d{2}-\d{2}-\d{4})$")
# An older round kept under a dated name (src/attachments._archive_name)
_DATED_RE = re.compile(r"_\d{4}-\d{2}-\d{2}(?:_\d+)?$")

CORRECTION_STATUS = 2
REGISTERED_STATUS = 7
RENEWAL_WINDOW_DAYS = 180
RED_DAYS = 3
AMBER_DAYS = 7

_cache_lock = threading.Lock()
_json_cache: dict[str, tuple[tuple[int, int], Any]] = {}
_CACHE_MAX = 4096


@dataclass
class Correction:
    company: str
    username: str
    reg_no: str
    name: str
    deadline: str | None      # YYYY-MM-DD from the notice; None = unknown
    days_left: int | None
    urgency: str              # overdue | red | amber | normal | unknown
    issued_on: str | None
    reasons: str | None       # full text, for a tooltip
    summary: str | None       # first line, for the row


@dataclass
class Renewal:
    company: str
    username: str
    reg_no: str
    name: str
    valid_to: str
    days_left: int
    expired: bool


def today_local() -> date:
    return datetime.now(_SHANGHAI_TZ).date()


def _cached_json(path: Path) -> Any:
    try:
        st = path.stat()
    except OSError:
        return None
    key, stamp = str(path), (st.st_mtime_ns, st.st_size)
    with _cache_lock:
        hit = _json_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    with _cache_lock:
        if len(_json_cache) >= _CACHE_MAX:
            _json_cache.clear()
        _json_cache[key] = (stamp, data)
    return data


def _latest_snapshot(company: str) -> dict | None:
    from . import main as _main  # heavy import (playwright); both processes already have it

    snapshots_dir: Path = _main._SNAPSHOTS_DIR
    prefix = attachments._safe_name(company)
    try:
        files = sorted(
            f for f in snapshots_dir.glob(f"{prefix}_*.json")
            if (m := _SNAPSHOT_RE.match(f.stem)) and m.group("prefix") == prefix
        )
    except OSError:
        return None
    if not files:
        return None
    data = _cached_json(files[-1])
    return data if isinstance(data, dict) else None


def record_facts(company: str, reg_no: str) -> dict[str, dict]:
    """facts of one record's .sync.json: {filename: pdftext.as_record()}."""
    rec_dir = attachments._ATTACHMENTS_DIR / attachments._safe_name(company) / attachments._safe_name(reg_no)
    facts = attachments.load_manifest_cached(rec_dir).get("facts")
    return {k: v for k, v in (facts or {}).items() if isinstance(v, dict)} if isinstance(facts, dict) else {}


def notices_of(company: str, reg_no: str) -> list[dict]:
    """Every notice parsed for one record, newest first, older rounds included
    (dated file names): [{file, kind, issued_on, deadline, valid_from,
    valid_to, reasons, current}] — current marks the live one of its kind."""
    facts = record_facts(company, reg_no)
    live = {kind: _current_notice(facts, kind) for kind in {v.get("kind") for v in facts.values()}}
    out = [
        {"file": name, "kind": v.get("kind"), "issued_on": v.get("issued_on"),
         "deadline": v.get("deadline"), "valid_from": v.get("valid_from"),
         "valid_to": v.get("valid_to"), "reasons": v.get("reasons"),
         "current": live.get(v.get("kind")) is v}
        for name, v in facts.items()
    ]
    out.sort(key=lambda n: (n["issued_on"] or "", n["file"]), reverse=True)
    return out


def _current_notice(facts: dict[str, dict], kind: str) -> dict | None:
    """The live notice of a kind: the undated file name; if several, the
    latest issued."""
    candidates = [
        v for name, v in facts.items()
        if v.get("kind") == kind and not _DATED_RE.search(Path(name).stem)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda v: v.get("issued_on") or "")


def _days(until: str | None, today: date) -> int | None:
    try:
        return (date.fromisoformat(until) - today).days if until else None
    except ValueError:
        return None


def urgency_of(days_left: int | None) -> str:
    if days_left is None:
        return "unknown"
    if days_left < 0:
        return "overdue"
    if days_left <= RED_DAYS:
        return "red"
    if days_left <= AMBER_DAYS:
        return "amber"
    return "normal"


def correction_for(acc: Account, record: dict, today: date | None = None) -> Correction | None:
    if int_or_none(record.get("dataRegAuditStatus")) != CORRECTION_STATUS:
        return None
    today = today or today_local()
    reg_no = str(record.get("dataRegNo") or "")
    notice = _current_notice(record_facts(acc.company, reg_no), "补正") or {}
    deadline = notice.get("deadline")
    days_left = _days(deadline, today)
    reasons = notice.get("reasons") or None
    return Correction(
        company=acc.company, username=acc.username, reg_no=reg_no,
        name=str(record.get("dataRegName") or ""),
        deadline=deadline, days_left=days_left, urgency=urgency_of(days_left),
        issued_on=notice.get("issued_on"), reasons=reasons,
        summary=reasons.splitlines()[0] if reasons else None,
    )


def renewal_for(acc: Account, record: dict, today: date | None = None,
                window: int | None = RENEWAL_WINDOW_DAYS) -> Renewal | None:
    """A Renewal when the certificate expires within `window` days (or already
    has, the record still sitting at 登记成功); None otherwise. window=None
    drops the limit — the detail drawer and the calendar want every expiry."""
    if int_or_none(record.get("dataRegAuditStatus")) != REGISTERED_STATUS:
        return None
    today = today or today_local()
    reg_no = str(record.get("dataRegNo") or "")
    notice = _current_notice(record_facts(acc.company, reg_no), "公告") or {}
    valid_from, valid_to = notice.get("valid_from"), notice.get("valid_to")
    if valid_from and valid_to and valid_to <= valid_from:
        return None  # the notice itself is wrong (facts stored before the parser refused these)
    days_left = _days(valid_to, today)
    if days_left is None or (window is not None and days_left > window):
        return None
    return Renewal(
        company=acc.company, username=acc.username, reg_no=reg_no,
        name=str(record.get("dataRegName") or ""),
        valid_to=notice["valid_to"], days_left=days_left, expired=days_left < 0,
    )


def build(accounts: list[Account], today: date | None = None) -> dict[str, Any]:
    """{"corrections": [...], "renewals": [...], "next_renewal": {...} | None}
    for the given (already visibility-filtered) accounts, as plain dicts."""
    today = today or today_local()
    corrections: list[Correction] = []
    renewals: list[Renewal] = []
    upcoming: list[tuple[str, dict]] = []  # (valid_to, row) beyond the window
    for acc in accounts:
        snap = _latest_snapshot(acc.company)
        for r in (snap or {}).get("records") or []:
            if not isinstance(r, dict) or not r.get("dataRegNo"):
                continue
            c = correction_for(acc, r, today)
            if c is not None:
                corrections.append(c)
                continue
            code = int_or_none(r.get("dataRegAuditStatus"))
            if code != REGISTERED_STATUS:
                continue
            rn = renewal_for(acc, r, today)
            if rn is not None:
                renewals.append(rn)
            else:
                valid_to = (_current_notice(record_facts(acc.company, str(r["dataRegNo"])), "公告") or {}).get("valid_to")
                if valid_to and _days(valid_to, today) is not None and _days(valid_to, today) > RENEWAL_WINDOW_DAYS:
                    upcoming.append((valid_to, {
                        "company": acc.company, "reg_no": str(r["dataRegNo"]),
                        "name": str(r.get("dataRegName") or ""), "valid_to": valid_to,
                    }))
    corrections.sort(key=lambda c: (c.deadline is None, c.deadline or "", c.company))
    renewals.sort(key=lambda r: (r.valid_to, r.company))
    return {
        "corrections": [asdict(c) for c in corrections],
        "renewals": [asdict(r) for r in renewals],
        "next_renewal": min(upcoming, key=lambda t: t[0])[1] if (upcoming and not renewals) else None,
        "today": today.isoformat(),
    }


def certificate_urgency(days_left: int) -> str:
    """Colour of a certificate expiry: expired, due within 30 days, inside the
    6-month renewal window, or not yet renewable (grey)."""
    if days_left < 0:
        return "overdue"
    if days_left <= 30:
        return "amber"
    if days_left <= RENEWAL_WINDOW_DAYS:
        return "normal"
    return "unknown"


def deadline_items(accounts: list[Account], today: date | None = None) -> dict[str, Any]:
    """Every dated deadline of the given (visibility-filtered) accounts, for
    the calendar: {"items": [...], "undated": [...]}.

    items: {date, kind ("correction" | "certificate"), company, username,
    reg_no, name, days_left, urgency}, sorted by date. Corrections whose
    notice gave no deadline go to "undated" so they are never silently missed.
    Unlike build(), certificates are not limited to the renewal window.
    """
    today = today or today_local()
    items: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []
    for acc in accounts:
        snap = _latest_snapshot(acc.company)
        for r in (snap or {}).get("records") or []:
            if not isinstance(r, dict) or not r.get("dataRegNo"):
                continue
            c = correction_for(acc, r, today)
            if c is not None:
                row = {"kind": "correction", "company": c.company, "username": c.username,
                       "reg_no": c.reg_no, "name": c.name, "days_left": c.days_left, "urgency": c.urgency}
                if c.deadline:
                    items.append({**row, "date": c.deadline})
                else:
                    undated.append(row)
                continue
            rn = renewal_for(acc, r, today, window=None)
            if rn is not None:
                items.append({"kind": "certificate", "company": rn.company, "username": rn.username,
                              "reg_no": rn.reg_no, "name": rn.name, "days_left": rn.days_left,
                              "urgency": certificate_urgency(rn.days_left), "date": rn.valid_to})
    items.sort(key=lambda i: (i["date"], i["kind"], i["company"], i["reg_no"]))
    undated.sort(key=lambda i: (i["company"], i["reg_no"]))
    return {"items": items, "undated": undated, "today": today.isoformat()}
