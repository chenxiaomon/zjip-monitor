"""Dashboard route: / and partial refresh endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.config_loader import ConfigError, load_accounts
from src.scraper import AUDIT_STATUS
from web.deps import STATUS_COLORS, templates
from web.services.events import read_events
from web.services.snapshot import get_latest_snapshot

router = APIRouter()


def _account_dot(status_counts: dict[int, int]) -> str:
    if status_counts.get(6, 0) > 0:
        return "red"
    if status_counts.get(2, 0) > 0:
        return "amber"
    return "green"


def _dashboard_ctx() -> dict:
    try:
        accounts = load_accounts()
    except ConfigError:
        accounts = []

    account_snapshots = []
    for acc in accounts:
        snap = get_latest_snapshot(acc.company)
        status_counts: dict[int, int] = {}
        last_check = None
        if snap:
            for r in snap.get("records", []):
                code = r.get("dataRegAuditStatus")
                if code is not None:
                    status_counts[int(code)] = status_counts.get(int(code), 0) + 1
            last_check = snap.get("snapshot_time")
        account_snapshots.append({
            "account": acc,
            "status_counts": status_counts,
            "last_check": last_check,
            "dot": _account_dot(status_counts),
            "total": sum(status_counts.values()),
        })

    total_records = sum(s["total"] for s in account_snapshots)
    success_count = sum(s["status_counts"].get(7, 0) for s in account_snapshots)
    pending_correction = sum(s["status_counts"].get(2, 0) for s in account_snapshots)
    events = read_events(hours=24, limit=50)

    return {
        "account_count": len(accounts),
        "total_records": total_records,
        "success_count": success_count,
        "pending_correction": pending_correction,
        "account_snapshots": account_snapshots,
        "events": events,
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request) -> HTMLResponse:
    ctx = _dashboard_ctx()
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/partials/metrics", response_class=HTMLResponse)
def partial_metrics(request: Request) -> HTMLResponse:
    ctx = _dashboard_ctx()
    return templates.TemplateResponse(request, "_partials/metric_cards.html", ctx)


@router.get("/partials/account-grid", response_class=HTMLResponse)
def partial_account_grid(request: Request) -> HTMLResponse:
    ctx = _dashboard_ctx()
    return templates.TemplateResponse(request, "_partials/account_grid.html", ctx)


@router.get("/partials/timeline", response_class=HTMLResponse)
def partial_timeline(request: Request) -> HTMLResponse:
    events = read_events(hours=24, limit=50)
    return templates.TemplateResponse(
        request,
        "_partials/timeline.html",
        {"events": events, "STATUS_COLORS": STATUS_COLORS, "AUDIT_STATUS": AUDIT_STATUS},
    )
