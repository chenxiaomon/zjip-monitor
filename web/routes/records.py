"""Records page: full registration record table with filtering and keyword search."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from src.scraper import AUDIT_STATUS, decode_status
from web.auth import visible_accounts
from web.deps import STATUS_COLORS, int_or_none, templates
from web.services.attachments_svc import archived_attachments, list_attachments
from web.services.snapshot import get_latest_snapshot

router = APIRouter()


def _ms_to_date(ms: int | None) -> str:
    if not ms:
        return "—"
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) + timedelta(hours=8)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "—"


def _date_to_ms(date_str: str, end_of_day: bool = False) -> int | None:
    """Convert 'YYYY-MM-DD' (Asia/Shanghai) to millisecond timestamp."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        # treat as UTC+8
        dt_utc8 = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return int(dt_utc8.timestamp() * 1000)
    except Exception:
        return None


# NOTE on status/account query params: the HTMX filter form carries hidden
# inputs, so partial refreshes send `status=` / `account=` as EMPTY STRINGS —
# typing them `int | None` in the route would 422 every unfiltered search.
# They are accepted as raw strings and coerced with int_or_none / `or None`.


def _build_records(
    accounts: list,
    account_filter: str | None,
    status_filter: int | None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    q: str | None = None,
) -> tuple[list[dict], list, list[dict]]:
    """Returns (rows, accounts_without_snapshot, fetch_warnings)."""
    rows: list[dict] = []
    missing_accounts: list = []
    fetch_warnings: list[dict] = []
    needle = (q or "").strip().lower()

    for acc in accounts:
        snap = get_latest_snapshot(acc.company)
        if not snap:
            # A company with no snapshot yet must not look identical to a
            # company with zero cases — surface it above the table.
            missing_accounts.append(acc)
            continue

        # Reconciliation banner data from the snapshot's fetch meta (additive
        # key — absent on old snapshots).
        fetch = snap.get("fetch") or {}
        if fetch:
            reported = fetch.get("reported_total") or 0
            fetched = fetch.get("fetched_count")
            if not fetch.get("complete", True):
                fetch_warnings.append({
                    "company": acc.company,
                    "kind": "partial",
                    "fetched": fetched,
                    "reported": reported,
                    "error": fetch.get("error"),
                })
            elif reported and fetched is not None and fetched != reported:
                fetch_warnings.append({
                    "company": acc.company,
                    "kind": "mismatch",
                    "fetched": fetched,
                    "reported": reported,
                    "error": None,
                })

        if account_filter and acc.username != account_filter:
            continue

        for r in snap.get("records", []):
            code = int_or_none(r.get("dataRegAuditStatus"))
            if status_filter is not None and code != status_filter:
                continue
            apply_ms = r.get("dataRegApplyTime")
            if start_ms and apply_ms and apply_ms < start_ms:
                continue
            if end_ms and apply_ms and apply_ms > end_ms:
                continue
            reg_no = r.get("dataRegNo", "")
            name = r.get("dataRegName", "")
            if needle:
                haystack = " ".join([
                    str(reg_no), str(name), acc.company, acc.username,
                    str(r.get("dataRegApplyName") or ""),
                ]).lower()
                if needle not in haystack:
                    continue
            rows.append({
                "company": acc.company,
                "username": acc.username,
                "reg_no": reg_no,
                "attachments": list_attachments(acc.company, reg_no) if reg_no else [],
                "archived": archived_attachments(acc.company, reg_no) if reg_no else [],
                "name": name,
                "industry": r.get("dataRegIndustry", ""),
                "data_type": r.get("dataRegDataType", ""),
                "apply_date": _ms_to_date(r.get("dataRegApplyTime")),
                "status_code": code,
                "status_label": decode_status(r.get("dataRegAuditStatus")),
                "_raw": r,
            })

    rows.sort(key=lambda r: r["_raw"].get("dataRegApplyTime") or 0, reverse=True)
    return rows, missing_accounts, fetch_warnings


def _ctx(
    request: Request,
    account: str | None,
    status: int | None,
    start_date: str | None,
    end_date: str | None,
    q: str | None = None,
) -> dict:
    start_ms = _date_to_ms(start_date) if start_date else None
    end_ms = _date_to_ms(end_date, end_of_day=True) if end_date else None
    accounts_list = visible_accounts(request)
    rows, missing_accounts, fetch_warnings = _build_records(
        accounts_list, account, status, start_ms, end_ms, q
    )
    return {
        "rows": rows,
        "accounts_list": accounts_list,
        "missing_accounts": missing_accounts,
        "fetch_warnings": fetch_warnings,
        "filter_account": account,
        "filter_status": status,
        "start_date": start_date or "",
        "end_date": end_date or "",
        "q": (q or "").strip(),
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
        "total": len(rows),
    }


@router.get("/records", response_class=HTMLResponse)
def records_page(
    request: Request,
    account: str | None = None,
    status: str | None = None,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    q: str | None = Query(None),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "records.html",
        _ctx(request, account or None, int_or_none(status), start_date, end_date, q),
    )


@router.get("/partials/records-table", response_class=HTMLResponse)
def records_table_partial(
    request: Request,
    account: str | None = None,
    status: str | None = None,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    q: str | None = Query(None),
) -> HTMLResponse:
    ctx = _ctx(request, account or None, int_or_none(status), start_date, end_date, q)
    return templates.TemplateResponse(request, "_partials/records_box.html", ctx)
