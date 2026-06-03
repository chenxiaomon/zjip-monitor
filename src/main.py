"""Main orchestration: iterate all accounts, fetch, diff, notify, report.

Serial for ≤10 accounts; asyncio ThreadPoolExecutor (max 3) for >10.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from .config_loader import Account, ConfigError, load_accounts
from .differ import DiffResult, diff_snapshots
from .exceptions import (
    CaptchaRequiredError,
    LoginError,
    ScraperError,
    TokenExpiredError,
)
from .login import get_token
from .notifier import notify
from .reporter import generate_report
from .scraper import decode_status, fetch_all_records

_ROOT = Path(__file__).parent.parent
_SNAPSHOTS_DIR = _ROOT / "data" / "snapshots"
_EVENTS_FILE = _ROOT / "data" / "events.jsonl"


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


def save_snapshot(company: str, records: list[dict], ts: datetime | None = None) -> Path:
    """Persist a full record list as a JSON snapshot.

    Args:
        company: Company name (used in filename).
        records: List of raw API record dicts.
        ts:      Snapshot timestamp (defaults to now UTC).

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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
    pattern = f"{safe_company}_*.json"
    files = sorted(_SNAPSHOTS_DIR.glob(pattern))
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

def scan_account_and_notify(account: Account) -> dict | None:
    """Login, fetch, and snapshot one account.

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

    # ── Login ──────────────────────────────────────────────────────────────
    try:
        token = get_token(account.username, account.password)
    except CaptchaRequiredError as exc:
        logger.error(f"[{account.company}] Captcha required — skipping: {exc}")
        result["error"] = f"captcha_required: {exc}"
        return result
    except LoginError as exc:
        logger.error(f"[{account.company}] Login failed — skipping: {exc}")
        result["error"] = f"login_failed: {exc}"
        return result

    # ── Fetch records (re-login once on token expiry) ───────────────────────
    try:
        records = fetch_all_records(token)
    except TokenExpiredError:
        logger.warning(f"[{account.company}] Token expired mid-run — re-logging in…")
        try:
            token = get_token(account.username, account.password, force_refresh=True)
            records = fetch_all_records(token)
        except (LoginError, ScraperError) as exc:
            logger.error(f"[{account.company}] Re-login or re-fetch failed: {exc}")
            result["error"] = str(exc)
            return result
    except ScraperError as exc:
        logger.error(f"[{account.company}] Scrape failed — skipping: {exc}")
        result["error"] = f"scrape_failed: {exc}"
        return result

    fetch_time = datetime.now(timezone.utc)

    # ── Diff against previous snapshot ────────────────────────────────────
    prev = load_latest_snapshot(account.company)
    prev_records: list[dict] = prev["records"] if prev else []
    diff = diff_snapshots(account.company, records, prev_records)

    # ── Save new snapshot ──────────────────────────────────────────────────
    snapshot_path = save_snapshot(account.company, records, ts=fetch_time)
    result["records"] = records
    result["snapshot_path"] = str(snapshot_path)
    result["diff"] = diff
    result["fetch_time"] = fetch_time

    # ── Notify if there are changes ────────────────────────────────────────
    if diff.has_changes:
        notify(diff, fetch_time)
    else:
        logger.debug(f"[{account.company}] No changes — notification skipped")

    # ── Append event to events.jsonl ───────────────────────────────────────
    _append_event(account.username, account.company, diff, fetch_time)

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

def _run_serial(accounts: list[Account]) -> list[dict]:
    results: list[dict] = []
    for i, account in enumerate(accounts):
        logger.info(f"── Account {i + 1}/{len(accounts)}: {account.company} ──")
        results.append(scan_account_and_notify(account))
        if i < len(accounts) - 1:
            delay = random.uniform(2.0, 5.0)
            logger.debug(f"Sleeping {delay:.1f}s before next account…")
            time.sleep(delay)
    return results


async def _run_concurrent(accounts: list[Account], max_workers: int = 3) -> list[dict]:
    """Run accounts concurrently using a thread pool (sync Playwright is thread-safe)."""
    loop = asyncio.get_running_loop()
    semaphore = asyncio.Semaphore(max_workers)

    async def _one(account: Account) -> dict:
        async with semaphore:
            return await loop.run_in_executor(None, scan_account_and_notify, account)

    return list(await asyncio.gather(*[_one(a) for a in accounts]))


def run_all(*, force_login: bool = False) -> dict[str, Any]:
    """Process all accounts. Serial for ≤10; asyncio concurrent for >10.

    Args:
        force_login: Bypass token cache for every account.

    Returns:
        Dict with keys: results (list of per-account dicts), report_path (str | None).
    """
    try:
        accounts = load_accounts()
    except ConfigError as exc:
        logger.error(f"Cannot load accounts: {exc}")
        return []

    run_time = datetime.now(timezone.utc)

    if len(accounts) <= 10:
        results = _run_serial(accounts)
    else:
        logger.info(f"{len(accounts)} accounts — using asyncio concurrency (max 3)")
        results = asyncio.run(_run_concurrent(accounts))

    # ── Generate HTML report ───────────────────────────────────────────────
    diffs: dict[str, DiffResult] = {}
    for r in results:
        if r.get("diff"):
            diffs[r["company"]] = r["diff"]
    report_path = None
    try:
        report_path = generate_report(results, diffs, report_time=run_time)
    except Exception as exc:
        logger.warning(f"Report generation failed (non-fatal): {exc}")

    # ── Summary ────────────────────────────────────────────────────────────
    ok = [r for r in results if not r["error"]]
    failed = [r for r in results if r["error"]]
    logger.info(
        f"Run complete — {len(ok)} succeeded, {len(failed)} failed"
        + (f": {[r['company'] for r in failed]}" if failed else "")
    )
    return {"results": results, "report_path": report_path}
