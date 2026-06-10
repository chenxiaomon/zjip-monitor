"""Public read-only status page — no authentication required."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.scraper import AUDIT_STATUS
from web.deps import STATUS_COLORS, templates
from web.services.snapshot import get_all_latest_snapshots

router = APIRouter(prefix="/view")

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


def _build_ctx() -> dict:
    account_cards: list[dict] = []
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
        account_cards.append({
            "company": snap.get("company", "未知"),
            "status_counts": status_counts,
            "last_check": snap.get("snapshot_time"),
            "dot": _account_dot(status_counts),
            "total": total,
        })
        total_records += total
        success_count += status_counts.get(7, 0)
        pending_correction += status_counts.get(2, 0)

    return {
        "account_cards": account_cards,
        "total_records": total_records,
        "success_count": success_count,
        "pending_correction": pending_correction,
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
    }


@router.get("/", response_class=HTMLResponse)
def view_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "view.html", _build_ctx())


@router.get("/partial", response_class=HTMLResponse)
def view_partial(request: Request) -> HTMLResponse:
    """HTMX polling target — returns only the dynamic content region."""
    return templates.TemplateResponse(request, "_partials/view_content.html", _build_ctx())
