"""Dashboard route: / and its partials.

One SSE event → one request: the whole body below the year chips is a single
htmx target (/partials/dashboard). The per-section routes remain for the
tests and for anything that wants just one piece; when a section is rendered
inside the body it carries no refresh attributes of its own.

The year chips are 关注范围, 全部年份 and each year present (web/services/
year_scope.py); every count, the trend and the links into the records page
follow the chosen one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src import ops_alerts, scan_plan, status_store, todo
from src.runtime_settings import ops_alert_settings, run_settings
from src.scraper import AUDIT_STATUS
from web.auth import (
    SCOPE_COOKIE, allowed_usernames, is_admin, require_admin, shown_accounts, shown_usernames,
    visible_accounts,
)
from web.deps import STATUS_COLORS, account_dot, int_or_none, templates
from web.deps import status_distribution
from web.services import audit_log, dash_layout, trends, year_scope
from web.services.settings_svc import REFUSED_HINT, SettingsUnreadable
from web.services.year_scope import YearScope
from web.services.events import read_events
from web.services.run_state import run_state_ctx
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


def scan_health(ss: dict | None) -> dict:
    """An account's durable scan status (src/status_store) as the company
    card shows it — the dashboard's and the mini program's alike.

    A 停巡 account is not part of the rounds, so what its last scan or
    weekly sweep did is not "上次巡检失败" for the card or for the phone
    (which is not being updated and cannot say 停巡): scan_failed and
    scan_partial stay false, and `sweep_failed` carries a failed sweep for
    whoever knows the word. The accounts page still shows the raw status."""
    dormant = status_store.is_dormant(ss)
    failed = bool(ss and ss.get("status") not in (None, "ok", "partial"))
    return {
        "scan_failed": failed and not dormant,
        "scan_partial": bool(ss and ss.get("status") == "partial") and not dormant,
        "login_paused": bool(ss and ss.get("login_paused")),
        "dormant": dormant,
        "sweep_failed": failed and dormant,
    }


def _filtered_events(request: Request, hours: int = 24, limit: int = 20) -> list[dict]:
    """The timeline's events: only ones that carry a change, scoped to the
    viewer's accounts before the cap so quiet scans cannot fill it."""
    return read_events(hours=hours, limit=limit, accounts=shown_usernames(request), with_changes=True)


def _apply_year(ms) -> int | None:
    ms = int_or_none(ms)
    return datetime.fromtimestamp(ms / 1000, _SHANGHAI_TZ).year if ms else None


def _run_ctx(request: Request) -> dict:
    """The global run state for the run bar. A group user sees that a scan is
    running, but not the name of a company outside their group."""
    run = run_state_ctx()
    progress = dict(run.get("progress") or {})
    if progress.get("account") and not is_admin(request):
        if progress["account"] not in {a.company for a in visible_accounts(request)}:
            progress["account"] = None
    return {**run, "progress": progress}


def _range_days(raw: str | None) -> int:
    """?range=90 switches the trend to 90 days; anything else means 30."""
    value = int_or_none(raw)
    return value if value in trends.RANGES else trends.RANGES[0]


def _dash_url_factory(scope: YearScope, range_days: int):
    """Links and the SSE refresh URL all carry the same year and range, and
    the defaults (the default year scope, 30 days) never appear in the query
    string. A year chip passes year=<a year | "all" | "focus">; the one that
    is the default drops out."""
    def dash_url(*, partial: bool = False, **overrides) -> str:
        params = {"year": scope.query, "range": range_days, **overrides}
        if "year" in overrides:
            params["year"] = year_scope.resolve(overrides["year"], current=scope.current).query
        if params.get("range") == trends.RANGES[0]:
            params["range"] = None
        query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
        base = "/partials/dashboard" if partial else "/"
        return f"{base}?{query}" if query else base
    return dash_url


def records_link_factory(scope: YearScope):
    """A link into the records page showing the same years as this page —
    a count on the dashboard opens exactly the rows it counted."""
    def records_link(**params) -> str:
        query = urlencode({k: v for k, v in {**params, "year": scope.query}.items() if v not in (None, "")},
                          safe=",")
        return "/records" + (f"?{query}" if query else "")
    return records_link


def _grid_ctx(request: Request, cards: list[dict]) -> dict:
    """The account grid in three parts, in the admin's layout (web/services/
    dash_layout.py): the cards to watch, one segment per 来源; the ones the
    admin hid, folded; the 停巡 ones whose work is done, folded too.

    Hiding wins over 停巡 (it is the explicit choice). Inside a segment a
    停巡 card with records still in review (建议恢复) comes first, as it did
    before segments. Only the cards move: every count on the page is built
    from all of them before this."""
    layout = dash_layout.current()

    def arrange(items: list[dict]) -> list[dash_layout.Segment]:
        return dash_layout.arrange(items, layout, account=lambda c: c["account"])

    def flat(items: list[dict]) -> list[dict]:
        return [c for seg in arrange(items) for c in seg.items]

    hidden = [c for c in cards if c["account"].username in layout.hidden]
    rest = [c for c in cards if c["account"].username not in layout.hidden]
    segments = arrange([c for c in rest if c["resume"] or not c["dormant"]])
    for seg in segments:
        seg.items.sort(key=lambda c: not c["resume"])  # stable: the layout order stays
    return {
        "grid_segments": segments,
        # A heading only says something when there is more than one.
        "show_segments": len(segments) > 1,
        "grid_main": [c for seg in segments for c in seg.items],
        "grid_hidden": flat(hidden),
        "grid_dormant": flat([c for c in rest if c["dormant"] and not c["resume"]]),
        "can_arrange": is_admin(request),
    }


def _dashboard_ctx(request: Request, year: str | None = None, range_days: int = 30) -> dict:
    # 我负责的 or everything visible; the ops banner below stays on the
    # full visible set — a failing scan is everyone's business.
    accounts = shown_accounts(request)
    allowed = shown_usernames(request)
    all_status = status_store.get_all()
    # Once per request: get_settings() is mtime-cached, so this is a stat().
    stale_hours = ops_alert_settings().stale_hours
    scope = year_scope.resolve(year)

    account_snapshots = []
    login_alerts = []
    years: set[int] = set()
    for acc in accounts:
        snap = get_latest_snapshot(acc.company)
        status_counts: dict[int, int] = {}
        last_check = None
        open_records = 0  # every year, not just the chosen ones: what 停巡 is about
        if snap:
            for r in snap.get("records", []):
                if (y := _apply_year(r.get("dataRegApplyTime"))) is not None:
                    years.add(y)
                if isinstance(r, dict) and scan_plan.is_open(r):
                    open_records += 1
                if not scope.matches(r):
                    continue
                code = int_or_none(r.get("dataRegAuditStatus"))
                if code is not None:
                    status_counts[code] = status_counts.get(code, 0) + 1
            last_check = snap.get("snapshot_time")

        ss = all_status.get(acc.username)
        health = scan_health(ss)
        if ss and not health["dormant"] and (
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
            # The durable per-account record beats the snapshot's timestamp:
            # a failed scan leaves the old snapshot in place, and its time
            # would then be read as "last scanned".
            "last_success": (ss or {}).get("last_success_at") or last_check,
            "dot": account_dot(status_counts),
            "total": sum(status_counts.values()),
            "scan_status": ss,
            "scan_failed": health["scan_failed"],
            "scan_partial": health["scan_partial"],
            # 停巡 data is old on purpose; the card says so instead.
            "stale": not health["dormant"] and _is_stale(last_check, stale_hours),
            "dormant": health["dormant"],
            "sweep_failed": health["sweep_failed"],
            # Stopped, yet something is still in review (a stop that was
            # forced): the card stays up top and says 建议恢复.
            "open_records": open_records,
            "resume": health["dormant"] and open_records > 0,
        })

    total_records = sum(s["total"] for s in account_snapshots)
    success_count = sum(s["status_counts"].get(7, 0) for s in account_snapshots)
    pending_correction = sum(s["status_counts"].get(2, 0) for s in account_snapshots)
    events = _filtered_events(request)
    successes = [
        (ss or {}).get("last_success_at")
        for u, ss in all_status.items()
        if isinstance(ss, dict) and (allowed is None or u in allowed)
    ]
    # Every record's notice facts are read here; the banner reuses this when
    # it counts the same accounts (全部, or nothing of one's own).
    todo_data = todo.build(accounts)

    return {
        "account_count": len(accounts),
        "dormant_count": sum(1 for s in account_snapshots if s["dormant"]),
        "total_records": total_records,
        "success_count": success_count,
        "pending_correction": pending_correction,
        "status_dist": status_distribution(account_snapshots),
        # Visible companies only: the trend file also holds deleted accounts'
        # history and other groups' companies.
        "trend": trends.trend_ctx({a.company for a in accounts}, scope.stats_key, range_days),
        "dash_url": _dash_url_factory(scope, range_days),
        "records_link": records_link_factory(scope),
        "account_snapshots": account_snapshots,
        **_grid_ctx(request, account_snapshots),
        "sweep_days": run_settings().dormant_sweep_days,
        "login_alerts": login_alerts,
        # Scoped to what this user may see: a group user must never learn from
        # the banner that another group's company is failing.
        "ops_alerts": ops_alerts.collect_alerts(allowed_usernames(request), todo_of=(accounts, todo_data)),
        "todo": todo_data,
        "events": events,
        "run": _run_ctx(request),
        "years": sorted(years, reverse=True),
        "last_success_at": max((s for s in successes if s), default=None),
        "year_scope": scope,
        "stale_hours": stale_hours,
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
    }


def _safe_next(target: str | None) -> str:
    """Only a path on this site — never //host or a scheme (open redirect)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


@router.get("/scope")
def set_scope(request: Request, to: str = Query("mine"), next_: str | None = Query(None, alias="next")):
    """Switch 我负责的 / 全部 and go back to the page the switch was on. A
    view preference in a cookie, nothing on the server changes."""
    response = RedirectResponse(url=_safe_next(next_), status_code=303)
    response.set_cookie(SCOPE_COOKIE, "all" if to == "all" else "mine",
                        max_age=365 * 86400, httponly=True, samesite="lax", path="/")
    return response


@router.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request, year: str | None = None,
                   range_: str | None = Query(None, alias="range")) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", _dashboard_ctx(request, year, _range_days(range_)))


@router.get("/partials/dashboard", response_class=HTMLResponse)
def partial_dashboard(request: Request, year: str | None = None,
                      range_: str | None = Query(None, alias="range")) -> HTMLResponse:
    """Everything below the year chips, in one response — the SSE target."""
    return templates.TemplateResponse(
        request, "_partials/dashboard_body.html", _dashboard_ctx(request, year, _range_days(range_)))


@router.get("/partials/run-bar", response_class=HTMLResponse)
def partial_run_bar(request: Request) -> HTMLResponse:
    """Polled every 3s while a scan runs, by every logged-in user."""
    return templates.TemplateResponse(request, "_partials/run_bar.html", {"run": _run_ctx(request)})


@router.get("/partials/metrics", response_class=HTMLResponse)
def partial_metrics(request: Request, year: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "_partials/metric_cards.html", _dashboard_ctx(request, year))


@router.get("/partials/account-grid", response_class=HTMLResponse)
def partial_account_grid(request: Request, year: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "_partials/account_grid.html", _dashboard_ctx(request, year))


@router.get("/partials/ops-banner", response_class=HTMLResponse)
def partial_ops_banner(request: Request) -> HTMLResponse:
    """The banner alone. Deliberately does not build the whole dashboard
    context: the banner needs none of the per-company snapshot work."""
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
        request, "_partials/todo.html", {"todo": todo.build(shown_accounts(request))},
    )


@router.get("/partials/timeline", response_class=HTMLResponse)
def partial_timeline(request: Request) -> HTMLResponse:
    events = _filtered_events(request)
    return templates.TemplateResponse(
        request,
        "_partials/timeline.html",
        {"events": events, "STATUS_COLORS": STATUS_COLORS, "AUDIT_STATUS": AUDIT_STATUS},
    )


# ---------------------------------------------------------------------------
# 排序与显示 — the admin's layout of the 账号状态 cards (web/services/dash_layout.py)
# ---------------------------------------------------------------------------

def _layout_ctx(request: Request, *, error: str | None = None) -> dict:
    """The editor lists every company the admin sees (not narrowed by
    我负责的: the layout is everyone's), in the order the dashboard uses."""
    accounts = visible_accounts(request)
    statuses = status_store.get_all()
    layout = dash_layout.current()
    segments = dash_layout.arrange(accounts, layout)
    return {
        "segments": [
            {"key": seg.key, "label": seg.label, "rows": [
                {"account": a, "hidden": a.username in layout.hidden,
                 "dormant": status_store.is_dormant(statuses.get(a.username))}
                for a in seg.items
            ]}
            for seg in segments
        ],
        "total": len(accounts),
        "hidden_count": sum(1 for a in accounts if a.username in layout.hidden),
        "is_default": layout.is_default,
        "error": error,
    }


def _layout_audit(diff: dict, accounts: list) -> dict:
    """The operation log line: 来源 by name, hidden companies by name, and
    only the fact that companies were reordered (thirty logins say nothing)."""
    names = {a.username: a.company for a in accounts}
    detail: dict = {}
    if "group_order" in diff:
        # As the dashboard shows them, not as stored: an empty list is the default order.
        keys = [a.group or dash_layout.UNGROUPED for a in accounts]
        old, new = (dash_layout.segment_order(keys, dash_layout.Layout(group_order=side))
                    for side in diff["group_order"])
        if old != new:
            detail["group_order"] = [old, new]
    if "hidden" in diff:
        detail["hidden"] = [[names.get(u, u) for u in side] for side in diff["hidden"]]
    if "account_order" in diff:
        detail["reordered"] = True
    return detail


@router.get("/dashboard/layout", response_class=HTMLResponse)
def layout_page(request: Request) -> HTMLResponse:
    require_admin(request)
    return templates.TemplateResponse(request, "dashboard_layout.html", _layout_ctx(request))


@router.post("/dashboard/layout")
def save_layout(
    request: Request,
    group: list[str] = Form(default=[]),
    account: list[str] = Form(default=[]),
    shown: list[str] = Form(default=[]),
):
    """The editor's form: `group` and `account` in the order the rows stand
    on the page, `shown` the ticked companies."""
    require_admin(request)
    accounts = visible_accounts(request)
    try:
        diff = dash_layout.save(group, account, shown, accounts)
    except SettingsUnreadable as exc:
        audit_log.record_request(request, "settings.dashboard_layout", ok=False, detail={"error": str(exc)})
        return templates.TemplateResponse(
            request, "dashboard_layout.html", _layout_ctx(request, error=f"{exc}。{REFUSED_HINT}"),
            status_code=409)
    if diff:
        audit_log.record_request(request, "settings.dashboard_layout", detail=_layout_audit(diff, accounts))
    else:
        audit_log.nothing_changed(request)
    return RedirectResponse(url="/", status_code=303)


@router.post("/dashboard/layout/reset")
def reset_layout(request: Request):
    require_admin(request)
    try:
        changed = dash_layout.reset()
    except SettingsUnreadable as exc:
        audit_log.record_request(request, "settings.dashboard_layout", ok=False, detail={"error": str(exc)})
        return templates.TemplateResponse(
            request, "dashboard_layout.html", _layout_ctx(request, error=f"{exc}。{REFUSED_HINT}"),
            status_code=409)
    if changed:
        audit_log.record_request(request, "settings.dashboard_layout", detail={"reset": True})
    else:
        audit_log.nothing_changed(request)
    return RedirectResponse(url="/dashboard/layout", status_code=303)
