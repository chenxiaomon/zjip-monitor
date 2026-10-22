"""操作日志 page: /audit — who did what, a date range, pages of 50 rows.

Admin-only twice over (the middleware's _ADMIN_ONLY_PREFIXES and
require_admin here). The partial lives under /audit/ rather than
/partials/ so the prefix check covers it too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from web.auth import require_admin
from web.deps import templates
from web.routes.changes import _day_bounds, _page_number
from web.services import audit_log

router = APIRouter()

PAGE_SIZE = 50
DEFAULT_DAYS = 7


def _audit_ctx(
    request: Request, start_date: str | None, end_date: str | None, actor: str | None,
    category: str | None, q: str | None, failed: str | None, page: str | None,
) -> dict:
    since, until = _day_bounds(start_date, end_date)
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=DEFAULT_DAYS)
    category = category if category in audit_log.CATEGORIES else ""
    entries = audit_log.query(
        since=since, until=until, actor=actor or "", category=category,
        keyword=q or "", failures_only=failed == "1",
    )
    total = len(entries)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    current = min(_page_number(page), pages)
    filters = {"start_date": start_date or "", "end_date": end_date or "", "actor": actor or "",
               "category": category, "q": q or "", "failed": "1" if failed == "1" else ""}

    def audit_url(partial: bool = False, **overrides) -> str:
        params = {**filters, "page": current, **overrides}
        query = urlencode({k: v for k, v in params.items() if v not in (None, "", 1)})
        return ("/audit/partial" if partial else "/audit") + (f"?{query}" if query else "")

    rows = [
        {**e, "label": audit_log.ACTION_LABELS.get(e.get("action", ""), e.get("action", "")),
         "summary": audit_log.describe(e)}
        for e in entries[(current - 1) * PAGE_SIZE: current * PAGE_SIZE]
    ]
    return {
        "rows": rows,
        "total": total,
        "page": current,
        "pages": pages,
        "filters": filters,
        "default_days": DEFAULT_DAYS,
        "actors": audit_log.actors(since),
        "categories": {k: v[0] for k, v in audit_log.CATEGORIES.items()},
        "audit_url": audit_url,
    }


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    actor: str | None = Query(None),
    category: str | None = Query(None),
    q: str | None = Query(None),
    failed: str | None = Query(None),
    page: str | None = Query(None),
) -> HTMLResponse:
    require_admin(request)
    ctx = _audit_ctx(request, start_date, end_date, actor, category, q, failed, page)
    return templates.TemplateResponse(request, "audit.html", ctx)


@router.get("/audit/partial", response_class=HTMLResponse)
def audit_partial(
    request: Request,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    actor: str | None = Query(None),
    category: str | None = Query(None),
    q: str | None = Query(None),
    failed: str | None = Query(None),
    page: str | None = Query(None),
) -> HTMLResponse:
    require_admin(request)
    ctx = _audit_ctx(request, start_date, end_date, actor, category, q, failed, page)
    return templates.TemplateResponse(request, "_partials/audit_list.html", ctx)
