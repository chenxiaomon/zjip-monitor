"""Records page: full registration record table with filtering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from src.config_loader import ConfigError, load_accounts
from src.scraper import AUDIT_STATUS, decode_status
from web.deps import STATUS_COLORS, templates
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


def _build_records(
    account_filter: str | None,
    status_filter: int | None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> tuple[list[dict], list]:
    try:
        accounts = load_accounts()
    except ConfigError:
        accounts = []

    rows: list[dict] = []
    for acc in accounts:
        if account_filter and acc.username != account_filter:
            continue
        snap = get_latest_snapshot(acc.company)
        if not snap:
            continue
        for r in snap.get("records", []):
            code = r.get("dataRegAuditStatus")
            if status_filter is not None and code != status_filter:
                continue
            apply_ms = r.get("dataRegApplyTime")
            if start_ms and apply_ms and apply_ms < start_ms:
                continue
            if end_ms and apply_ms and apply_ms > end_ms:
                continue
            rows.append({
                "company": acc.company,
                "username": acc.username,
                "reg_no": r.get("dataRegNo", ""),
                "name": r.get("dataRegName", ""),
                "industry": r.get("dataRegIndustry", ""),
                "data_type": r.get("dataRegDataType", ""),
                "apply_date": _ms_to_date(r.get("dataRegApplyTime")),
                "status_code": code,
                "status_label": decode_status(code),
                "_raw": r,
            })

    rows.sort(key=lambda r: r["_raw"].get("dataRegApplyTime") or 0, reverse=True)
    return rows, accounts


def _ctx(
    request: Request,
    account: str | None,
    status: int | None,
    start_date: str | None,
    end_date: str | None,
) -> dict:
    start_ms = _date_to_ms(start_date) if start_date else None
    end_ms = _date_to_ms(end_date, end_of_day=True) if end_date else None
    rows, accounts = _build_records(account, status, start_ms, end_ms)
    try:
        accounts_list = load_accounts()
    except ConfigError:
        accounts_list = []
    return {
        "rows": rows,
        "accounts_list": accounts_list,
        "filter_account": account,
        "filter_status": status,
        "start_date": start_date or "",
        "end_date": end_date or "",
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
        "total": len(rows),
    }


@router.get("/records", response_class=HTMLResponse)
def records_page(
    request: Request,
    account: str | None = None,
    status: int | None = None,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
) -> HTMLResponse:
    return templates.TemplateResponse(request, "records.html", _ctx(request, account, status, start_date, end_date))


@router.get("/partials/records-table", response_class=HTMLResponse)
def records_table_partial(
    request: Request,
    account: str | None = None,
    status: int | None = None,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
) -> HTMLResponse:
    ctx = _ctx(request, account, status, start_date, end_date)
    return templates.TemplateResponse(request, "_partials/records_table.html", ctx)
