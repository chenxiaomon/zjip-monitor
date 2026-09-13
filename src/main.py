"""Main orchestration: iterate all accounts, fetch, diff, notify, report.

Serial for ≤10 accounts; asyncio + thread pool (max 3) for >10.

All entry points (web console, daemon, CLI) serialize through the global
run lock in src/control.py; a CancelToken threads checkpoints through the
pipeline so a run can be stopped between accounts/pages.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from . import control, status_store
from .attachments import sync_attachments
from .config_loader import Account, ConfigError, load_accounts
from .control import CancelToken
from .differ import DiffResult, diff_snapshots
from .fsutil import atomic_write_text
from .exceptions import (
    CaptchaRequiredError,
    CredentialsRejectedError,
    LoginError,
    PlatformUnavailableError,
    RunCancelled,
    ScraperError,
    TokenExpiredError,
)
from .login import forget_cached_token, get_token, has_valid_cached_token
from .notifier import notify, notify_batch, notify_login_failure
from .reporter import generate_report
from .scraper import FetchResult, decode_status, fetch_all_records

_ROOT = Path(__file__).parent.parent
_SNAPSHOTS_DIR = _ROOT / "data" / "snapshots"
_REPORTS_DIR = _ROOT / "data" / "reports"
_EVENTS_FILE = _ROOT / "data" / "events.jsonl"
# One-shot manual override for the shrink guard: touch
# data/control/force_save/{safe_username}.flag to accept the next shrunk fetch.
_FORCE_SAVE_DIR = _ROOT / "data" / "control" / "force_save"

# Guards concurrent appends from the >10-account thread-pool path
_EVENTS_LOCK = threading.Lock()

# Retention limits applied by _prune_data() at the end of each full run
_SNAPSHOTS_KEEP_PER_COMPANY = 30
_REPORTS_KEEP_DAYS = 60
_EVENTS_ROTATE_BYTES = 5 * 1024 * 1024
# Rotated events-*.jsonl had no retention at all: 5 MB blocks accumulated
# forever on a root partition that has been as full as 96%.
_EVENTS_ROTATED_KEEP = 6
# Snapshot stems end with "_YYYY-MM-DD-HHMM" (16 chars incl. underscore)
_SNAPSHOT_TS_SUFFIX_LEN = 16
_SNAPSHOT_TS_RE = re.compile(r"^_\d{4}-\d{2}-\d{2}-\d{4}$")

# Wall-clock ceiling per account in the concurrent (>10 accounts) path.
# Login (≤120s, see login.LOGIN_HARD_TIMEOUT_SECONDS) + paging + attachments
# normally finish in well under 2 minutes; 10 minutes is the "something is
# wedged" line. Second line of defence behind the login watchdog: guarantees
# run_all() always reaches ctx.finish() and releases the global run lock.
ACCOUNT_HARD_TIMEOUT_SECONDS = 600


def _prune_data() -> None:
    """Bounded data growth: old snapshots/reports removed, big event log rotated."""
    try:
        if _SNAPSHOTS_DIR.exists():
            groups: dict[str, list[Path]] = {}
            for f in sorted(_SNAPSHOTS_DIR.glob("*.json")):
                stem = f.stem
                key = stem[:-_SNAPSHOT_TS_SUFFIX_LEN] if len(stem) > _SNAPSHOT_TS_SUFFIX_LEN else stem
                groups.setdefault(key, []).append(f)
            for files in groups.values():
                for old in files[:-_SNAPSHOTS_KEEP_PER_COMPANY]:
                    old.unlink(missing_ok=True)

        if _REPORTS_DIR.exists():
            cutoff = time.time() - _REPORTS_KEEP_DAYS * 86400
            for f in _REPORTS_DIR.glob("*.html"):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)

        with _EVENTS_LOCK:
            if _EVENTS_FILE.exists() and _EVENTS_FILE.stat().st_size > _EVENTS_ROTATE_BYTES:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                rotated = _EVENTS_FILE.with_name(f"events-{ts}.jsonl")
                _EVENTS_FILE.rename(rotated)
                logger.info(f"events.jsonl rotated → {rotated.name}")
            # Names carry a zero-padded UTC stamp, so sorted() is chronological.
            old_rotations = sorted(_EVENTS_FILE.parent.glob("events-*.jsonl"))
            for stale in old_rotations[:-_EVENTS_ROTATED_KEEP]:
                stale.unlink(missing_ok=True)
                logger.info(f"Pruned rotated event log {stale.name}")
    except Exception as exc:
        logger.warning(f"Data pruning failed (non-fatal): {exc!r}")


def _append_event(account: str, company: str, diff: DiffResult, ts: datetime) -> None:
    """Append a diff event to data/events.jsonl and broadcast to web SSE if running."""
    event: dict = {
        "account": account,
        "company": company,
        "timestamp": ts.isoformat(),
        "added": diff.added,
        "changed": [
            {
                "reg_no": c.reg_no,
                "name": c.name,
                "old_status_code": c.old_status_code,
                "new_status_code": c.new_status_code,
                "old_status": c.old_status,
                "new_status": c.new_status,
            }
            for c in diff.changed
        ],
        "removed": diff.removed,
    }
    with _EVENTS_LOCK:
        _EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _EVENTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def _snapshot_path(company: str, ts: datetime) -> Path:
    """Build snapshot file path: data/snapshots/{company}_{YYYY-MM-DD-HHMM}.json"""
    safe_company = "".join(c if c.isalnum() or c in "-_" else "_" for c in company)
    ts_str = ts.strftime("%Y-%m-%d-%H%M")
    return _SNAPSHOTS_DIR / f"{safe_company}_{ts_str}.json"


def save_snapshot(
    company: str,
    records: list[dict],
    ts: datetime | None = None,
    fetch_meta: dict | None = None,
) -> Path:
    """Persist a full record list as a JSON snapshot.

    Args:
        company:    Company name (used in filename).
        records:    List of raw API record dicts.
        ts:         Snapshot timestamp (defaults to now UTC).
        fetch_meta: Optional reconciliation info stored under the additive
                    "fetch" key (reported_total / fetched_count / complete /
                    error / merged_with_previous). Old snapshots without the
                    key stay fully readable — consumers use .get("fetch").

    Returns:
        Path to the written snapshot file.
    """
    if ts is None:
        ts = datetime.now(timezone.utc)

    _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(company, ts)

    payload = {
        "company": company,
        "snapshot_time": ts.isoformat(),
        "record_count": len(records),
        "records": records,
    }
    if fetch_meta:
        payload["fetch"] = fetch_meta
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info(f"[{company}] Snapshot saved → {path.name} ({len(records)} records)")
    return path


def load_latest_snapshot(company: str) -> dict | None:
    """Load the most recent snapshot for a company.

    Returns:
        Snapshot dict with keys: company, snapshot_time, record_count, records.
        None if no snapshot exists yet.
    """
    if not _SNAPSHOTS_DIR.exists():
        return None

    safe_company = "".join(c if c.isalnum() or c in "-_" else "_" for c in company)
    # Anchor the prefix to the timestamp suffix: a plain glob would let
    # "公司" match "公司_分部_2026-…" and cross-contaminate diffs.
    files = sorted(
        f for f in _SNAPSHOTS_DIR.glob(f"{safe_company}_*.json")
        if _SNAPSHOT_TS_RE.match(f.stem[len(safe_company):])
    )
    if not files:
        return None

    latest = files[-1]
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"[{company}] Could not read snapshot {latest.name}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Per-account processing
# ---------------------------------------------------------------------------

def _safe_flag_name(username: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in username)


def _consume_force_save_flag(username: str) -> bool:
    """One-shot operator override for the shrink guard (check = consume)."""
    flag = _FORCE_SAVE_DIR / f"{_safe_flag_name(username)}.flag"
    if flag.exists():
        flag.unlink(missing_ok=True)
        return True
    return False


def _current_password(account: Account) -> str:
    """Re-read the stored password just before a fresh login. A password edited
    in the web console mid-run must win over this run's stale in-memory copy —
    otherwise a re-login would cache a token minted with the OLD password and
    pollute the lockout streak."""
    try:
        for a in load_accounts():
            if a.username == account.username:
                return a.password
    except Exception:
        pass
    return account.password


def _record_login_failure(account: Account, exc: Exception) -> None:
    """Durable bookkeeping for a failed login. Only genuine credential
    rejections count toward the lockout streak (a platform outage must not
    pause scheduled scans); a notification goes out when a new failure streak
    starts or the account just got auto-paused — at most twice per streak.

    A platform-side refusal (captcha demanded / throttled / maintenance) gets
    its own status so the console can say what to do about it: nothing but
    wait, as opposed to "go fix the password"."""
    kind = "platform_error" if isinstance(exc, PlatformUnavailableError) else "login_failed"
    status_store.record_scan(
        account.username, account.company, ok=False, error=str(exc), kind=kind
    )
    if not isinstance(exc, CredentialsRejectedError):
        return
    flags = status_store.record_login(
        account.username, account.company, ok=False, error=str(exc)
    )
    if flags["streak_started"] or flags["just_paused"]:
        try:
            notify_login_failure(
                account.company, account.username, str(exc), paused=flags["just_paused"]
            )
        except Exception as nexc:
            logger.error(
                f"[{account.company}] Login-failure notification failed (non-fatal): {nexc!r}"
            )


def scan_account_and_notify(
    account: Account,
    cancel: CancelToken | None = None,
    *,
    force_login: bool = False,
    notify_inline: bool = True,
    ignore_pause: bool = False,
) -> dict | None:
    """Login, fetch, and snapshot one account.

    Args:
        cancel:        Optional cancel token; RunCancelled propagates to the caller.
        force_login:   Bypass the token cache and do a fresh browser login.
        notify_inline: Send notifications immediately (single-account scans).
                       run_all() passes False and aggregates at the end instead.
        ignore_pause:  Attempt the login even when the account is auto-paused
                       after repeated credential rejections. run_single() (the
                       manual per-account scan) passes True — a click is an
                       explicit retry, and a success un-pauses the account.

    Returns:
        Dict with keys: account, company, records, snapshot_path, error.
        On failure, records is [] and error contains the reason.
    """
    result: dict = {
        "account": account.username,
        "company": account.company,
        "contact": account.contact,
        "records": [],
        "snapshot_path": None,
        "error": None,
    }

    # ── Lockout protection: skip paused accounts without touching the platform
    if not ignore_pause and status_store.is_login_paused(account.username):
        msg = "login_paused: 连续登录失败已自动暂停，请在控制台更新密码（手动巡检可强制重试）"
        logger.warning(f"[{account.company}] {msg}")
        result["error"] = msg
        return result

    # ── Login ──────────────────────────────────────────────────────────────
    # A cache hit is not a login. Recording success for one would reset the
    # lockout streak (and stamp a fake last_login_ok_at) for an account the
    # platform is in fact rejecting, postponing the auto-pause indefinitely.
    fresh_login = force_login or not has_valid_cached_token(account.username)
    try:
        if cancel:
            cancel.checkpoint(phase="login")
        token = get_token(account.username, _current_password(account), force_refresh=force_login)
    except CaptchaRequiredError as exc:
        logger.error(f"[{account.company}] Captcha required — skipping: {exc}")
        result["error"] = f"captcha_required: {exc}"
        status_store.record_scan(
            account.username, account.company, ok=False, error=str(exc), kind="captcha_required"
        )
        return result
    except LoginError as exc:
        logger.error(f"[{account.company}] Login failed — skipping: {exc}")
        result["error"] = f"login_failed: {exc}"
        _record_login_failure(account, exc)
        return result
    except RunCancelled:
        raise
    except Exception as exc:
        # Playwright timeouts/launch errors are not LoginError subclasses;
        # without this, one flaky account would abort the whole run.
        logger.error(f"[{account.company}] Unexpected login failure — skipping: {exc!r}")
        result["error"] = f"login_failed: {exc}"
        status_store.record_scan(
            account.username, account.company, ok=False, error=str(exc), kind="login_failed"
        )
        return result
    if fresh_login:
        status_store.record_login(account.username, account.company, ok=True)

    # ── Fetch records (re-login once on token expiry) ───────────────────────
    try:
        if cancel:
            cancel.checkpoint(phase="fetch")
        fetch: FetchResult = fetch_all_records(token, cancel=cancel)
    except TokenExpiredError:
        logger.warning(f"[{account.company}] Token expired mid-run — re-logging in…")
        # The cached file still looks valid locally; without dropping it every
        # later scan would replay the same dead token.
        forget_cached_token(account.username)
        try:
            token = get_token(account.username, _current_password(account), force_refresh=True)
            status_store.record_login(account.username, account.company, ok=True)
            fetch = fetch_all_records(token, cancel=cancel)
        except (LoginError, ScraperError) as exc:
            logger.error(f"[{account.company}] Re-login or re-fetch failed: {exc}")
            result["error"] = str(exc)
            if isinstance(exc, LoginError):
                _record_login_failure(account, exc)
            else:
                status_store.record_scan(
                    account.username, account.company,
                    ok=False, error=str(exc), kind="scrape_failed",
                )
            return result
    except ScraperError as exc:
        logger.error(f"[{account.company}] Scrape failed — skipping: {exc}")
        result["error"] = f"scrape_failed: {exc}"
        status_store.record_scan(
            account.username, account.company, ok=False, error=str(exc), kind="scrape_failed"
        )
        return result

    records = fetch.records
    fetch_time = datetime.now(timezone.utc)

    # ── Diff against previous snapshot ────────────────────────────────────
    prev = load_latest_snapshot(account.company)
    prev_records: list[dict] = prev["records"] if prev else []

    if not fetch.complete and not prev_records:
        # Nothing to merge with: baselining a partial fetch would flood the
        # next complete run with false 新增 events. Refuse, keep the records
        # visible in the result, and let the next run establish the baseline.
        msg = (
            f"partial_first_fetch: {fetch.error} — 首次抓取不完整，暂不保存基线，"
            "下次巡检完整后自动建立"
        )
        logger.error(f"[{account.company}] {msg}")
        result["error"] = msg
        result["records"] = records
        status_store.record_scan(
            account.username, account.company, ok=False, error=msg, kind="scrape_failed"
        )
        return result

    merged_with_previous = False
    if not fetch.complete and prev_records:
        # Partial fetch: merge the fetched records over the previous snapshot
        # so the unfetched tail is neither reported as 消失 nor dropped from
        # the UI. Fetched records win by dataRegNo; previous keyless records
        # are dropped (the differ ignores them anyway).
        prev_by_no = {r["dataRegNo"]: r for r in prev_records if r.get("dataRegNo")}
        curr_by_no = {r["dataRegNo"]: r for r in records if r.get("dataRegNo")}
        keyless = [r for r in records if not r.get("dataRegNo")]
        records = list({**prev_by_no, **curr_by_no}.values()) + keyless
        merged_with_previous = True
        logger.warning(
            f"[{account.company}] Partial fetch ({fetch.error}) — merged "
            f"{len(curr_by_no)} fetched records over the previous snapshot "
            f"({len(records)} total)"
        )

    fetch_meta = {
        "reported_total": fetch.reported_total,
        "total_from_server": fetch.total_from_server,
        "fetched_count": len(fetch.records),
        "pages_fetched": fetch.pages_fetched,
        "complete": fetch.complete,
        "error": fetch.error,
        "merged_with_previous": merged_with_previous,
    }
    result["fetch_meta"] = fetch_meta

    # Sanity check: a sudden >50% shrink almost always means a truncated fetch,
    # not real deletions (complete fetches only — a merged partial cannot
    # shrink). Two escape hatches so a genuine large shrink cannot freeze the
    # account's data forever: the server-reported total confirming the smaller
    # count, and the one-shot force_save flag.
    if (
        fetch.complete
        and len(prev_records) >= 10
        and len(records) < len(prev_records) * 0.5
    ):
        if (
            fetch.total_from_server
            and fetch.reported_total > 0
            and len(records) == fetch.reported_total
        ):
            logger.warning(
                f"[{account.company}] Large shrink ({len(prev_records)} → {len(records)}) "
                "matches the server-reported total — accepting as genuine"
            )
        elif _consume_force_save_flag(account.username):
            logger.warning(f"[{account.company}] Large shrink accepted via force_save flag")
        else:
            msg = (
                f"suspicious_shrink: fetched {len(records)} records but previous "
                f"snapshot has {len(prev_records)} — snapshot NOT saved, no diff emitted"
            )
            logger.error(f"[{account.company}] {msg}")
            result["error"] = msg
            result["records"] = records
            status_store.record_scan(
                account.username, account.company,
                ok=False, error=msg, kind="shrink_refused", fetch_meta=fetch_meta,
            )
            return result

    diff = diff_snapshots(account.company, records, prev_records)

    # ── Save new snapshot ──────────────────────────────────────────────────
    snapshot_path = save_snapshot(account.company, records, ts=fetch_time, fetch_meta=fetch_meta)
    result["records"] = records
    result["snapshot_path"] = str(snapshot_path)
    result["diff"] = diff
    result["fetch_time"] = fetch_time
    status_store.record_scan(account.username, account.company, ok=True, fetch_meta=fetch_meta)

    # ── Notify if there are changes (non-fatal) ────────────────────────────
    if diff.has_changes and notify_inline:
        try:
            notify(diff, fetch_time)
        except Exception as exc:
            logger.error(f"[{account.company}] Notification failed (non-fatal): {exc!r}")
    elif not diff.has_changes:
        logger.debug(f"[{account.company}] No changes — notification skipped")

    # ── Append event to events.jsonl (non-fatal: the snapshot is already
    # saved and the durable status already says ok — a failed event write must
    # not bubble into _run_serial's catch-all and flip the status to error) ──
    try:
        _append_event(account.username, account.company, diff, fetch_time)
    except Exception as exc:
        logger.error(f"[{account.company}] events.jsonl append failed (non-fatal): {exc!r}")

    # ── Sync 通知书/证书 attachments (non-fatal) ────────────────────────────
    try:
        if cancel:
            cancel.checkpoint(phase="attachments")
        try:
            new_files = sync_attachments(token, account.company, records, cancel=cancel)
        except TokenExpiredError:
            # Token can expire mid-sync on large first runs — re-login once
            logger.warning(f"[{account.company}] Token expired during attachment sync — re-logging in…")
            forget_cached_token(account.username)
            token = get_token(account.username, _current_password(account), force_refresh=True)
            status_store.record_login(account.username, account.company, ok=True)
            new_files = sync_attachments(token, account.company, records, cancel=cancel)
        if new_files:
            logger.info(f"[{account.company}] Downloaded {len(new_files)} attachment(s)")
        result["attachments"] = [str(p) for p in new_files]
    except RunCancelled:
        raise
    except Exception as exc:
        logger.warning(f"[{account.company}] Attachment sync failed (non-fatal): {exc}")

    # Log status summary
    status_counts: dict[str, int] = {}
    for r in records:
        label = decode_status(r.get("dataRegAuditStatus"))
        status_counts[label] = status_counts.get(label, 0) + 1
    summary = ", ".join(f"{s}×{n}" for s, n in sorted(status_counts.items()))
    logger.info(f"[{account.company}] {len(records)} records — {summary}")

    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _error_result(account: Account, error: str) -> dict:
    return {
        "account": account.username,
        "company": account.company,
        "contact": account.contact,
        "records": [],
        "snapshot_path": None,
        "error": error,
    }


def _run_serial(
    accounts: list[Account],
    cancel: CancelToken | None = None,
    *,
    force_login: bool = False,
) -> tuple[list[dict], bool]:
    """Returns (results, cancelled)."""
    results: list[dict] = []
    try:
        for i, account in enumerate(accounts):
            if cancel:
                cancel.checkpoint(account=account.company, index=i + 1, total=len(accounts))
            logger.info(f"── Account {i + 1}/{len(accounts)}: {account.company} ──")
            try:
                results.append(
                    scan_account_and_notify(
                        account, cancel, force_login=force_login, notify_inline=False
                    )
                )
            except RunCancelled:
                raise
            except Exception as exc:
                logger.error(f"[{account.company}] Unexpected failure — skipping: {exc!r}")
                results.append(_error_result(account, f"{type(exc).__name__}: {exc}"))
                status_store.record_scan(
                    account.username, account.company,
                    ok=False, error=f"{type(exc).__name__}: {exc}", kind="error",
                )
            if i < len(accounts) - 1:
                delay = random.uniform(2.0, 5.0)
                logger.debug(f"Sleeping {delay:.1f}s before next account…")
                if cancel:
                    cancel.wait(delay)
                else:
                    time.sleep(delay)
    except RunCancelled:
        logger.info("Stop requested — run cancelled after current account")
        return results, True
    return results, False


async def _run_concurrent(
    accounts: list[Account],
    cancel: CancelToken | None = None,
    *,
    force_login: bool = False,
    max_workers: int = 3,
) -> list[dict]:
    """Run accounts concurrently using a thread pool (sync Playwright is thread-safe).

    Each account gets a hard wall-clock budget (ACCOUNT_HARD_TIMEOUT_SECONDS).
    A worker thread that never returns is abandoned, not joined: the run must
    still reach ctx.finish() and release the global lock. That is why a
    private executor is used — asyncio.run() would join the default
    executor's threads on shutdown and hang on the very thread we gave up on.
    """
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scan")

    async def _one(account: Account) -> dict:
        fut = loop.run_in_executor(
            executor,
            lambda: scan_account_and_notify(
                account, cancel, force_login=force_login, notify_inline=False
            ),
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(fut), timeout=ACCOUNT_HARD_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.error(
                f"[{account.company}] Account scan exceeded "
                f"{ACCOUNT_HARD_TIMEOUT_SECONDS}s — abandoning its worker thread"
            )
            status_store.record_scan(
                account.username, account.company,
                ok=False, error=f"超过 {ACCOUNT_HARD_TIMEOUT_SECONDS}s 未完成", kind="timeout",
            )
            return _error_result(
                account, f"timeout: 超过 {ACCOUNT_HARD_TIMEOUT_SECONDS}s 未完成"
            )

    try:
        raw = await asyncio.gather(*[_one(a) for a in accounts], return_exceptions=True)
    finally:
        # Don't wait for stuck workers; nothing new is submitted anyway.
        executor.shutdown(wait=False, cancel_futures=True)

    results: list[dict] = []
    for account, r in zip(accounts, raw):
        if isinstance(r, RunCancelled):
            results.append(_error_result(account, "cancelled"))
        elif isinstance(r, BaseException):
            logger.error(f"[{account.company}] Unexpected failure: {r!r}")
            results.append(_error_result(account, f"{type(r).__name__}: {r}"))
        else:
            results.append(r)
    return results


def run_all(*, force_login: bool = False, trigger: str = "cli") -> dict[str, Any]:
    """Process all accounts. Serial for ≤10; asyncio concurrent for >10.

    Takes the global run lock; if another run (any process) holds it, returns
    immediately with skipped="already_running".

    Args:
        force_login: Bypass token cache for every account.
        trigger:     Who started this run ("web" | "daemon" | "cli") — shown in the UI.

    Returns:
        Dict with keys: results (list of per-account dicts), report_path (str | None).
        May also carry skipped="already_running" or cancelled=True.
    """
    ctx = control.try_acquire(kind="run_all", trigger=trigger)
    if ctx is None:
        holder = control.read_state() or {}
        logger.warning(
            f"Another run in progress (pid={holder.get('pid')}, "
            f"trigger={holder.get('trigger')}) — skipping"
        )
        return {"results": [], "report_path": None, "skipped": "already_running"}

    try:
        accounts = load_accounts()
    except ConfigError as exc:
        logger.error(f"Cannot load accounts: {exc}")
        ctx.finish("error", error=f"config: {exc}"[:200])
        return {"results": [], "report_path": None}

    run_time = datetime.now(timezone.utc)
    try:
        if len(accounts) <= 10:
            results, cancelled = _run_serial(accounts, ctx.cancel, force_login=force_login)
        else:
            logger.info(f"{len(accounts)} accounts — using asyncio concurrency (max 3)")
            results = asyncio.run(
                _run_concurrent(accounts, ctx.cancel, force_login=force_login)
            )
            cancelled = ctx.cancel.should_stop()
    except Exception as exc:
        ctx.finish("error", error=f"{type(exc).__name__}: {exc}"[:200])
        raise

    diffs: dict[str, DiffResult] = {}
    for r in results:
        if r.get("diff"):
            diffs[r["company"]] = r["diff"]

    # ── Aggregated notifications — one message per channel for the whole run.
    # Sent even when cancelled: completed accounts' snapshots are already
    # saved, so unsent changes would never be notified again.
    try:
        notify_batch(diffs, fetch_time=run_time)
    except Exception as exc:
        logger.error(f"Batch notification failed (non-fatal): {exc!r}")

    # ── Generate HTML report (skipped when stopping — exit fast) ────────────
    report_path = None
    if not cancelled:
        try:
            report_path = generate_report(results, diffs, report_time=run_time)
        except Exception as exc:
            logger.warning(f"Report generation failed (non-fatal): {exc}")
        _prune_data()

    # ── Summary ────────────────────────────────────────────────────────────
    ok = [r for r in results if not r["error"]]
    failed = [r for r in results if r["error"]]
    logger.info(
        f"Run {'cancelled' if cancelled else 'complete'} — "
        f"{len(ok)} succeeded, {len(failed)} failed"
        + (f": {[r['company'] for r in failed]}" if failed else "")
    )
    ctx.finish("cancelled" if cancelled else "done")
    return {"results": results, "report_path": report_path, "cancelled": cancelled}


def run_single(account: Account, *, trigger: str = "web") -> dict[str, Any]:
    """Scan one account under the global run lock.

    Returns the scan result dict; skipped="already_running" if the lock is held.
    """
    ctx = control.try_acquire(kind="scan_one", trigger=trigger)
    if ctx is None:
        return {**_error_result(account, ""), "error": None, "skipped": "already_running"}
    try:
        ctx.cancel.checkpoint(account=account.company, index=1, total=1)
        # ignore_pause: a manual per-account scan is an explicit retry — it may
        # attempt the login even when auto-paused, and success un-pauses.
        result = scan_account_and_notify(account, ctx.cancel, ignore_pause=True)
    except RunCancelled:
        ctx.finish("cancelled")
        return _error_result(account, "cancelled")
    except Exception as exc:
        ctx.finish("error", error=f"{type(exc).__name__}: {exc}"[:200])
        raise
    ctx.finish("done")
    return result
