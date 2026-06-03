"""Public JSON API for the WeChat Mini Program — no authentication required."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from src.scraper import AUDIT_STATUS, decode_status
from web.deps import _fmt_time
from web.services.snapshot import get_all_latest_snapshots

router = APIRouter(prefix="/api")

_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _account_dot(status_counts: dict[int, int]) -> str:
    if status_counts.get(6, 0) > 0:
        return "red"
    if status_counts.get(2, 0) > 0:
        return "amber"
    return "green"


def _is_2026(ms: int | None) -> bool:
    if not ms:
        return False
    return datetime.fromtimestamp(ms / 1000, tz=_SHANGHAI_TZ).year == 2026


def _ms_to_date(ms: int | None) -> str:
    if not ms:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=_SHANGHAI_TZ)
    return dt.strftime("%Y-%m-%d")


@router.get("/status")
def api_status() -> JSONResponse:
    """All accounts' latest status (2026 records only)."""
    accounts = []
    total_records = 0
    success_count = 0
    pending_correction = 0

    for snap in get_all_latest_snapshots():
        status_counts: dict[int, int] = {}
        for r in snap.get("records", []):
            if not _is_2026(r.get("dataRegApplyTime")):
                continue
            code = r.get("dataRegAuditStatus")
            if code is not None:
                status_counts[int(code)] = status_counts.get(int(code), 0) + 1

        total = sum(status_counts.values())
        accounts.append({
            "company": snap.get("company", "未知"),
            "dot": _account_dot(status_counts),
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
    company: str | None = Query(None),
    status: int | None = Query(None),
) -> JSONResponse:
    """Registration records (2026 only), filtered by company name and/or status."""
    rows: list[dict] = []

    for snap in get_all_latest_snapshots():
        snap_company = snap.get("company", "")
        if company and snap_company != company:
            continue
        for r in snap.get("records", []):
            if not _is_2026(r.get("dataRegApplyTime")):
                continue
            code = r.get("dataRegAuditStatus")
            if status is not None and code != status:
                continue
            rows.append({
                "company": snap_company,
                "reg_no": r.get("dataRegNo", ""),
                "name": r.get("dataRegName", ""),
                "apply_date": _ms_to_date(r.get("dataRegApplyTime")),
                "status_code": code,
                "status_label": decode_status(code),
            })

    rows.sort(key=lambda r: r["apply_date"], reverse=True)
    return JSONResponse({"records": rows, "total": len(rows)})
