"""Records page: full registration record table with filtering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
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


def _build_records(
    account_filter: str | None,
    status_filter: int | None,
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


def _ctx(request: Request, account: str | None, status: int | None) -> dict:
    rows, accounts = _build_records(account, status)
    try:
        accounts_list = load_accounts()
    except ConfigError:
        accounts_list = []
    return {
        "rows": rows,
        "accounts_list": accounts_list,
        "filter_account": account,
        "filter_status": status,
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
        "total": len(rows),
    }


@router.get("/records", response_class=HTMLResponse)
def records_page(
    request: Request,
    account: str | None = None,
    status: int | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(request, "records.html", _ctx(request, account, status))


@router.get("/partials/records-table", response_class=HTMLResponse)
def records_table_partial(
    request: Request,
    account: str | None = None,
    status: int | None = None,
) -> HTMLResponse:
    ctx = _ctx(request, account, status)
    return templates.TemplateResponse(request, "_partials/records_table.html", ctx)
