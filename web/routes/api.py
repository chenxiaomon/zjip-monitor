"""JSON API for the WeChat Mini Program — requires X-API-Key header (or Basic Auth)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from loguru import logger

from src.scraper import AUDIT_STATUS, decode_status
from web.auth import visible_accounts
from web.deps import _fmt_time, account_dot, detail_url, in_year, int_or_none
from web.services.snapshot import get_latest_snapshot

router = APIRouter(prefix="/api")

_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _ms_to_date(ms: int | None) -> str:
    """Same defensive shape as the console's copy: an out-of-range timestamp
    from the platform must not 500 the whole endpoint."""
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=_SHANGHAI_TZ).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError, TypeError):
        logger.warning(f"Unparsable dataRegApplyTime: {ms!r}")
        return ""


def _visible_snapshots(request: Request) -> list[dict]:
    """Latest snapshot per account the caller is allowed to see.

    This used to read every snapshot on disk regardless of the caller, so the
    console's group isolation stopped at the API boundary. The key carries
    admin scope today, so the set is the same minus orphan snapshots of
    accounts that no longer exist — but binding keys to groups is now a change
    to the middleware, not to every handler.
    """
    snaps = []
    for acc in visible_accounts(request):
        snap = get_latest_snapshot(acc.company)
        if snap is not None:
            snaps.append(snap)
    return snaps


@router.get("/status")
def api_status(request: Request, year: int | None = Query(None)) -> JSONResponse:
    """All accounts' latest status (optionally limited to one application year)."""
    accounts = []
    total_records = 0
    success_count = 0
    pending_correction = 0

    for snap in _visible_snapshots(request):
        status_counts: dict[int, int] = {}
        for r in snap.get("records", []):
            if not in_year(r.get("dataRegApplyTime"), year):
                continue
            code = int_or_none(r.get("dataRegAuditStatus"))
            if code is not None:
                status_counts[code] = status_counts.get(code, 0) + 1

        total = sum(status_counts.values())
        accounts.append({
            "company": snap.get("company", "未知"),
            "dot": account_dot(status_counts),
            "total": total,
            "status_counts": {str(k): v for k, v in status_counts.items()},
            "last_check": _fmt_time(snap.get("snapshot_time")),
        })
        total_records += total
        success_count += status_counts.get(7, 0)
        pending_correction += status_counts.get(2, 0)

    return JSONResponse({
        "total_records": total_records,
        "success_count": success_count,
        "pending_correction": pending_correction,
        "accounts": accounts,
        "status_labels": {str(k): v for k, v in AUDIT_STATUS.items()},
    })


@router.get("/records")
def api_records(
    request: Request,
    company: str | None = Query(None),
    status: int | None = Query(None),
    year: int | None = Query(None),
) -> JSONResponse:
    """Registration records, filtered by company name, status and/or application year."""
    rows: list[dict] = []

    for snap in _visible_snapshots(request):
        snap_company = snap.get("company", "")
        if company and snap_company != company:
            continue
        for r in snap.get("records", []):
            if not in_year(r.get("dataRegApplyTime"), year):
                continue
            code = int_or_none(r.get("dataRegAuditStatus"))
            if status is not None and code != status:
                continue
            reg_no = r.get("dataRegNo", "")
            rows.append({
                "company": snap_company,
                "reg_no": reg_no,
                "name": r.get("dataRegName", ""),
                "apply_date": _ms_to_date(r.get("dataRegApplyTime")),
                "status_code": code,
                "status_label": decode_status(code),
                "detail_url": detail_url(reg_no) if reg_no else "",
            })

    rows.sort(key=lambda r: r["apply_date"], reverse=True)
    return JSONResponse({"records": rows, "total": len(rows)})
