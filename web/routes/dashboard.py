"""Dashboard route: / and partial refresh endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src import ops_alerts, status_store, todo
from src.runtime_settings import ops_alert_settings
from src.scraper import AUDIT_STATUS
from web.auth import allowed_usernames, visible_accounts
from web.deps import STATUS_COLORS, account_dot, in_year, int_or_none, templates
from web.services.events import read_events
from web.services.snapshot import get_latest_snapshot

router = APIRouter()

_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _is_stale(snapshot_time: str | None, stale_hours: float) -> bool:
    """A snapshot older than `stale_hours` (settings.yaml → ops_alerts.stale_hours)
    is flagged 数据可能过期 on the account card."""
    if not snapshot_time:
        return False
    try:
        dt = datetime.fromisoformat(snapshot_time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return datetime.now(timezone.utc) - dt > timedelta(hours=stale_hours)


def _filtered_events(request: Request, hours: int = 24, limit: int = 50) -> list[dict]:
    events = read_events(hours=hours, limit=limit)
    allowed = allowed_usernames(request)
    if allowed is None:
        return events
    return [e for e in events if e.get("account") in allowed]


def _dashboard_ctx(request: Request, year: int | None = None) -> dict:
    accounts = visible_accounts(request)
    allowed = allowed_usernames(request)
    all_status = status_store.get_all()
    # Once per request: get_settings() is mtime-cached, so this is a stat().
    stale_hours = ops_alert_settings().stale_hours

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
        if ss and (
            ss.get("login_paused")
            or ss.get("status") in ("login_failed", "platform_error", "captcha_required")
        ):
            login_alerts.append({
                "company": acc.company,
                "username": acc.username,
                "paused": bool(ss.get("login_paused")),
                "kind": ss.get("status"),
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
            "stale": _is_stale(last_check, stale_hours),
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
        # Scoped to what this user may see: a group user must never learn from
        # the banner that another group's company is failing.
        "ops_alerts": ops_alerts.collect_alerts(allowed),
        "todo": todo.build(accounts),
        "events": events,
        "filter_year": year,
        "stale_hours": stale_hours,
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


@router.get("/partials/ops-banner", response_class=HTMLResponse)
def partial_ops_banner(request: Request) -> HTMLResponse:
    """The banner alone — the SSE-triggered refresh target. Deliberately does
    not build the whole dashboard context: this runs on every scan event, and
    the banner needs none of the per-company snapshot work."""
    return templates.TemplateResponse(
        request,
        "_partials/ops_banner.html",
        {"ops_alerts": ops_alerts.collect_alerts(allowed_usernames(request))},
    )


@router.get("/partials/todo", response_class=HTMLResponse)
def partial_todo(request: Request) -> HTMLResponse:
    """待办 alone: corrections with deadlines, certificates due for renewal —
    scoped like everything else to the accounts this user may see."""
    return templates.TemplateResponse(
        request, "_partials/todo.html", {"todo": todo.build(visible_accounts(request))},
    )


@router.get("/partials/timeline", response_class=HTMLResponse)
def partial_timeline(request: Request) -> HTMLResponse:
    events = _filtered_events(request)
    return templates.TemplateResponse(
        request,
        "_partials/timeline.html",
        {"events": events, "STATUS_COLORS": STATUS_COLORS, "AUDIT_STATUS": AUDIT_STATUS},
    )
