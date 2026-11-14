"""停巡与抽查：which accounts a batch round scans.

An administrator can set an account to 停巡 (status_store `dormant`) once its
work is done; batch rounds — the daemon's schedule and 立即巡检全部 — then
leave it out. The rules here keep that from turning into blindness:

- 办结 (assess) is what the console offers to stop: a baseline, at least one
  record, a last scan that succeeded completely, every record in a terminal
  status (archive_policy.TERMINAL_STATUSES — 公示中 and 待发证 are not done),
  every document already fetched, and no certificate in its renewal window.
  The system only suggests; a person stops an account. Nothing here ever
  stops one.
- A stopped account is swept once its last success is `run.dormant_sweep_days`
  old (default 7), a few per round, oldest attempt first; one whose
  documents are still owed (the platform did not answer — see
  attachments.docs_pending) is retried every round until they arrive.
- It is switched back on automatically, never off: when a scan of it finds a
  new open application or a record moving back into review (resume_cause),
  when someone opens 去平台办理 for it (web/routes/accounts.py), and when one
  of its certificates enters the renewal window after it was stopped
  (renewal_resume_cause).

Pure functions over accounts, status entries and snapshots: no I/O beyond the
attachment/fact readers they call, and no import of src.main.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from . import attachments, todo
from .archive_policy import TERMINAL_STATUSES
from .config_loader import Account
from .scraper import decode_status, int_or_none
from .status_store import is_dormant

_SHANGHAI_TZ = timezone(timedelta(hours=8))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
RENEWAL_WINDOW_DAYS = todo.RENEWAL_WINDOW_DAYS
# A stopped account owing documents is retried every round only while the
# platform changed that record recently — the documents of a fresh 登记成功
# can lag, and a query the platform did not answer is worth asking again
# soon. A document that keeps failing for longer than this waits for the
# weekly sweep, or 停巡 would turn into "logged into every round" for good.
RETRY_DAYS = 3


def is_open(record: dict) -> bool:
    """Not finished: anything outside TERMINAL_STATUSES, including 14/15/16
    and a status the platform may add later (the same rule as archive
    release and 关注范围)."""
    return int_or_none(record.get("dataRegAuditStatus")) not in TERMINAL_STATUSES


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _local_date(value: Any) -> date | None:
    dt = _parse_iso(value)
    return dt.astimezone(_SHANGHAI_TZ).date() if dt else None


def _mm_dd(value: Any) -> str:
    dt = _parse_iso(value)
    return dt.astimezone(_SHANGHAI_TZ).strftime("%m-%d") if dt else "未知"


# ---------------------------------------------------------------------------
# 办结 — what the console offers to stop
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Assessment:
    has_baseline: bool          # a snapshot exists
    record_count: int
    open_records: int           # not in TERMINAL_STATUSES
    docs_pending: int           # finished records whose documents are still owed
    renewals: tuple = ()        # todo.Renewal inside the renewal window
    unknown_expiry: int = 0     # 登记成功 records whose certificate expiry is unknown
    last_scan_ok: bool = False  # status_store says "ok" (complete)
    last_scan_status: str | None = None
    as_of: str | None = None    # the snapshot's time

    @property
    def done(self) -> bool:
        return (self.has_baseline and self.record_count > 0 and self.last_scan_ok
                and not self.open_records and not self.docs_pending and not self.renewals)

    @property
    def reasons(self) -> list[str]:
        """Why the account still needs every round, most decisive first."""
        if not self.has_baseline:
            return ["还没有巡检过"]
        out: list[str] = []
        if not self.last_scan_ok:
            what = "不完整" if self.last_scan_status == "partial" else "失败"
            out.append(f"上次巡检{what}（数据截至 {_mm_dd(self.as_of)}）")
        if self.record_count == 0:
            out.append("暂无登记记录")
        if self.open_records:
            out.append(f"{self.open_records} 条在审")
        if self.docs_pending:
            out.append(f"{self.docs_pending} 条文件未下载完")
        if self.renewals:
            first = min(self.renewals, key=lambda r: r.valid_to)
            what = "已过期，未见续展" if first.expired else "到期，在续展期内"
            more = f" 等 {len(self.renewals)} 张" if len(self.renewals) > 1 else ""
            out.append(f"证书 {first.valid_to} {what}{more}")
        return out

    @property
    def notes(self) -> list[str]:
        """Worth knowing, but no reason to keep scanning."""
        return [f"{self.unknown_expiry} 张证书到期日未知"] if self.unknown_expiry else []


def assess(
    acc: Account,
    snapshot: dict | None,
    entry: dict | None,
    *,
    today: date | None = None,
    docs_check: bool = True,
) -> Assessment:
    """One account's 办结 assessment from its latest snapshot and status entry.
    `docs_check` is attachments.enabled(): with downloads switched off no
    document is ever owed."""
    today = today or todo.today_local()
    entry = entry if isinstance(entry, dict) else {}
    status = entry.get("status")
    if not isinstance(snapshot, dict):
        return Assessment(has_baseline=False, record_count=0, open_records=0, docs_pending=0,
                          last_scan_status=status)
    records = [r for r in snapshot.get("records") or [] if isinstance(r, dict)]
    open_records = docs = unknown = 0
    renewals = []
    for r in records:
        if is_open(r):
            open_records += 1
            continue
        if docs_check and attachments.docs_pending(acc.company, r):
            docs += 1
        if int_or_none(r.get("dataRegAuditStatus")) == todo.REGISTERED_STATUS:
            ren = todo.renewal_for(acc, r, today, window=None)
            if ren is None:
                unknown += 1
            elif ren.days_left <= RENEWAL_WINDOW_DAYS:
                renewals.append(ren)
    return Assessment(
        has_baseline=True,
        record_count=len(records),
        open_records=open_records,
        docs_pending=docs,
        renewals=tuple(renewals),
        unknown_expiry=unknown,
        last_scan_ok=status == "ok",
        last_scan_status=status,
        as_of=snapshot.get("snapshot_time"),
    )


# ---------------------------------------------------------------------------
# Automatic resume — only ever towards scanning more
# ---------------------------------------------------------------------------

def resume_cause(diff: Any, records: Iterable[dict], had_baseline: bool) -> str | None:
    """Why a scan of a stopped account should put it back on every round:
    a new application still open, or a record that moved (back) into an
    unfinished status. A new record that is already finished, or a move into
    a finished status (撤销, 续展登记), is a change worth logging but not a
    reason to scan every round. Without a previous snapshot the diff is
    empty, so any open record counts."""
    if not had_baseline:
        n = sum(1 for r in records if isinstance(r, dict) and is_open(r))
        return f"{n} 条在审（之前没有快照）" if n else None
    for r in getattr(diff, "added", None) or []:
        if isinstance(r, dict) and is_open(r):
            return f"新申请 {r.get('dataRegNo') or r.get('dataRegName') or ''}".rstrip()
    for c in getattr(diff, "changed", None) or []:
        if int_or_none(c.new_status_code) not in TERMINAL_STATUSES:
            return f"{c.reg_no} 变为{c.new_status or decode_status(c.new_status_code)}"
    return None


def renewal_resume_cause(acc: Account, snapshot: dict | None, dormant_since: Any,
                         today: date) -> str | None:
    """A certificate whose renewal window (expiry − 180 days) opened after the
    account was stopped. A window that was already open when an administrator
    stopped the account is that administrator's call and is left alone, so
    the account is not switched back on every round."""
    stopped_on = _local_date(dormant_since)
    for r in (snapshot or {}).get("records") or []:
        if not isinstance(r, dict):
            continue
        ren = todo.renewal_for(acc, r, today, window=None)
        if ren is None:
            continue
        try:
            opens = date.fromisoformat(ren.valid_to) - timedelta(days=RENEWAL_WINDOW_DAYS)
        except (TypeError, ValueError):
            continue
        if opens > today or (stopped_on is not None and opens <= stopped_on):
            continue
        return f"证书 {ren.valid_to} 到期，进入续展期"
    return None


# ---------------------------------------------------------------------------
# One batch round
# ---------------------------------------------------------------------------

@dataclass
class RoundPlan:
    order: list[Account]                      # every account, load_accounts order
    active: list[Account] = field(default_factory=list)   # every round (incl. renewal resumes)
    swept: list[Account] = field(default_factory=list)    # stopped, due for the weekly sweep
    retry: list[Account] = field(default_factory=list)    # stopped, documents still owed
    resumes: list[tuple[Account, str]] = field(default_factory=list)
    dormant_total: int = 0

    @property
    def targets(self) -> list[Account]:
        chosen = {a.username for a in (*self.active, *self.swept, *self.retry)}
        return [a for a in self.order if a.username in chosen]

    @property
    def skipped(self) -> int:
        """Stopped accounts this round leaves out."""
        return self.dormant_total - len(self.swept) - len(self.retry)


def sweep_cap(dormant_total: int, sweep_days: int) -> int:
    """How many stopped accounts one round sweeps. Measured against every
    stopped account, not just the ones due: a backlog (say, 20 accounts
    stopped on the same day) then clears in sweep_days rounds instead of
    trickling out one by one as the due list shrinks."""
    if sweep_days <= 0 or dormant_total <= 0:
        return 0
    return max(1, math.ceil(dormant_total / sweep_days))


def plan_round(
    accounts: list[Account],
    statuses: dict[str, dict],
    snapshot_of: Callable[[str], dict | None],
    *,
    now: datetime | None = None,
    today: date | None = None,
    sweep_days: int = 7,
    docs_check: bool = True,
    disk_low: bool = False,
    include_dormant: bool = False,
) -> RoundPlan:
    """Which accounts this round scans.

    - every account not stopped, plus a stopped one whose certificate just
      entered its renewal window (listed in `resumes` for the caller to
      persist and log);
    - stopped accounts owing documents for a record the platform changed in
      the last RETRY_DAYS days, every round, unless the disk guard would skip
      the download anyway (`disk_low`);
    - stopped accounts due for the sweep: last success on a Beijing date at
      least `sweep_days` before today (or never, or a stamp from the future
      after a clock step) — by date, not to the minute, or a sweep that
      finished at 09:05 would miss the 09:00 round a week later — oldest
      attempt first so one that keeps failing goes to the back, at most
      sweep_cap() per round;
    - neither for an account paused after repeated login failures: its scan
      would only be skipped without contacting the platform;
    - `include_dormant` (run_once.py --all): every stopped account.
    """
    now = now or datetime.now(timezone.utc)
    today = today or now.astimezone(_SHANGHAI_TZ).date()
    plan = RoundPlan(order=list(accounts))
    stopped: list[Account] = []
    for acc in accounts:
        entry = statuses.get(acc.username)
        if not is_dormant(entry):
            plan.active.append(acc)
            continue
        cause = renewal_resume_cause(acc, snapshot_of(acc.company), entry.get("dormant_since"), today)
        if cause:
            plan.resumes.append((acc, cause))
            plan.active.append(acc)
            continue
        stopped.append(acc)
    plan.dormant_total = len(stopped)
    if not stopped:
        return plan
    if include_dormant:
        plan.swept = stopped
        return plan

    paused = {a.username for a in stopped if (statuses.get(a.username) or {}).get("login_paused")}
    if docs_check and not disk_low:
        recent_ms = (now - timedelta(days=RETRY_DAYS)).timestamp() * 1000
        for acc in stopped:
            if acc.username in paused:
                continue
            snap = snapshot_of(acc.company)
            records = (snap or {}).get("records") or []
            if any(
                isinstance(r, dict)
                and (int_or_none(r.get("updateTime")) or 0) >= recent_ms
                and attachments.docs_pending(acc.company, r)
                for r in records
            ):
                plan.retry.append(acc)

    cap = sweep_cap(len(stopped), sweep_days)
    if cap:
        last_due_day = today - timedelta(days=sweep_days)
        skip = paused | {a.username for a in plan.retry}
        due = []
        for acc in stopped:
            if acc.username in skip:
                continue
            last_ok = _parse_iso((statuses.get(acc.username) or {}).get("last_success_at"))
            if (last_ok is None or last_ok > now
                    or last_ok.astimezone(_SHANGHAI_TZ).date() <= last_due_day):
                due.append(acc)
        due.sort(key=lambda a: _parse_iso((statuses.get(a.username) or {}).get("last_attempt_at")) or _EPOCH)
        plan.swept = due[:cap]
    return plan


# ---------------------------------------------------------------------------
# "No successful scan for a while" once some accounts are stopped
# ---------------------------------------------------------------------------

def success_threshold_hours(entries: dict[str, dict], stale_hours: float,
                            sweep_days: int) -> float | None:
    """How old the newest success among `entries` may get before the banner
    and /health say scanning has stalled. While any account in view is
    scanned every round that is ops_alerts.stale_hours, as before. When all
    of them are stopped only the weekly sweep still runs, so the bar is two
    sweep intervals plus the usual slack; with no sweep at all nothing is
    expected to run, and there is nothing to check (None)."""
    rows = [e for e in entries.values() if isinstance(e, dict)]
    if not rows or any(not is_dormant(e) for e in rows):
        return stale_hours
    if sweep_days <= 0:
        return None
    return 2 * sweep_days * 24 + stale_hours
