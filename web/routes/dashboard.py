"""Dashboard route: / and partial refresh endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src import status_store
from src.scraper import AUDIT_STATUS
from web.auth import allowed_usernames, visible_accounts
from web.deps import STATUS_COLORS, account_dot, in_year, int_or_none, templates
from web.services.events import read_events
from web.services.snapshot import get_latest_snapshot

router = APIRouter()

_SHANGHAI_TZ = timezone(timedelta(hours=8))

# A snapshot older than this is flagged 数据可能过期 on the account card.
_STALE_AFTER_HOURS = 36


def _is_stale(snapshot_time: str | None) -> bool:
    if not snapshot_time:
        return False
    try:
        dt = datetime.fromisoformat(snapshot_time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return datetime.now(timezone.utc) - dt > timedelta(hours=_STALE_AFTER_HOURS)


def _filtered_events(request: Request, hours: int = 24, limit: int = 50) -> list[dict]:
    events = read_events(hours=hours, limit=limit)
    allowed = allowed_usernames(request)
    if allowed is None:
        return events
    return [e for e in events if e.get("account") in allowed]


def _dashboard_ctx(request: Request, year: int | None = None) -> dict:
    accounts = visible_accounts(request)
    all_status = status_store.get_all()

    account_snapshots = []
    login_alerts = []
    for acc in accounts:
        snap = get_latest_snapshot(acc.company)
        status_counts: dict[int, int] = {}
        last_check = None
        if snap:
            for r in snap.get("records", []):
                if not in_year(r.get("dataRegApplyTime"), year):
                    continue
                code = int_or_none(r.get("dataRegAuditStatus"))
                if code is not None:
                    status_counts[code] = status_counts.get(code, 0) + 1
            last_check = snap.get("snapshot_time")

        ss = all_status.get(acc.username)
        scan_failed = bool(ss and ss.get("status") not in (None, "ok", "partial"))
        scan_partial = bool(ss and ss.get("status") == "partial")
        if ss and (ss.get("login_paused") or ss.get("status") == "login_failed"):
            login_alerts.append({
                "company": acc.company,
                "username": acc.username,
                "paused": bool(ss.get("login_paused")),
                "error": ss.get("error"),
            })

        account_snapshots.append({
            "account": acc,
            "status_counts": status_counts,
            "last_check": last_check,
            "dot": account_dot(status_counts),
            "total": sum(status_counts.values()),
            "scan_status": ss,
            "scan_failed": scan_failed,
            "scan_partial": scan_partial,
            "stale": _is_stale(last_check),
        })

    total_records = sum(s["total"] for s in account_snapshots)
    success_count = sum(s["status_counts"].get(7, 0) for s in account_snapshots)
    pending_correction = sum(s["status_counts"].get(2, 0) for s in account_snapshots)
    events = _filtered_events(request)

    return {
        "account_count": len(accounts),
        "total_records": total_records,
        "success_count": success_count,
        "pending_correction": pending_correction,
        "account_snapshots": account_snapshots,
        "login_alerts": login_alerts,
        "events": events,
        "filter_year": year,
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request, year: int | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html", _dashboard_ctx(request, year))


@router.get("/partials/metrics", response_class=HTMLResponse)
def partial_metrics(request: Request, year: int | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "_partials/metric_cards.html", _dashboard_ctx(request, year))


@router.get("/partials/account-grid", response_class=HTMLResponse)
def partial_account_grid(request: Request, year: int | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "_partials/account_grid.html", _dashboard_ctx(request, year))


@router.get("/partials/timeline", response_class=HTMLResponse)
def partial_timeline(request: Request) -> HTMLResponse:
    events = _filtered_events(request)
    return templates.TemplateResponse(
        request,
        "_partials/timeline.html",
        {"events": events, "STATUS_COLORS": STATUS_COLORS, "AUDIT_STATUS": AUDIT_STATUS},
    )
