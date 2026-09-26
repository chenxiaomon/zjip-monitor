"""Changes history route: /changes — a date range, pages of 50 rows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from src.scraper import AUDIT_STATUS
from web.auth import allowed_usernames, visible_accounts
from web.deps import STATUS_COLORS, templates
from web.services.events import read_change_rows

router = APIRouter()

PAGE_SIZE = 50
DEFAULT_DAYS = 30
_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _day_bounds(start_date: str | None, end_date: str | None) -> tuple[datetime | None, datetime | None]:
    """Beijing calendar days → UTC instants; an unparseable date is ignored."""
    def parse(value: str | None, end: bool) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=_SHANGHAI_TZ)
        except ValueError:
            return None
        return (dt + timedelta(days=1)) if end else dt

    return parse(start_date, False), parse(end_date, True)


def _page_number(page: str | None) -> int:
    try:
        return max(1, int(page or 1))
    except ValueError:
        return 1


def _changes_ctx(
    request: Request, account: str | None, start_date: str | None, end_date: str | None, page: str | None,
) -> dict:
    since, until = _day_bounds(start_date, end_date)
    if since is None and until is None:
        since = datetime.now(timezone.utc) - timedelta(days=DEFAULT_DAYS)
    # The allow-list goes INTO the reader: filtering afterwards let a busy
    # account the user may not see push that user's own changes out of the
    # window, leaving them an empty page.
    rows = read_change_rows(account=account, accounts=allowed_usernames(request), since=since, until=until)
    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    current = min(_page_number(page), pages)
    filters = {"account": account or "", "start_date": start_date or "", "end_date": end_date or ""}

    def changes_url(partial: bool = False, **overrides) -> str:
        params = {**filters, "page": current, **overrides}
        query = urlencode({k: v for k, v in params.items() if v not in (None, "", 1)})
        return ("/partials/changes" if partial else "/changes") + (f"?{query}" if query else "")

    return {
        "rows": rows[(current - 1) * PAGE_SIZE: current * PAGE_SIZE],
        "total": total,
        "page": current,
        "pages": pages,
        "filter_account": account,
        "start_date": start_date or "",
        "end_date": end_date or "",
        "default_days": DEFAULT_DAYS,
        "accounts_list": visible_accounts(request),
        "changes_url": changes_url,
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
    }


@router.get("/changes", response_class=HTMLResponse)
def changes_page(
    request: Request,
    account: str | None = None,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    page: str | None = Query(None),
) -> HTMLResponse:
    ctx = _changes_ctx(request, account or None, start_date, end_date, page)
    return templates.TemplateResponse(request, "changes.html", ctx)


@router.get("/partials/changes", response_class=HTMLResponse)
def partial_changes(
    request: Request,
    account: str | None = None,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    page: str | None = Query(None),
) -> HTMLResponse:
    ctx = _changes_ctx(request, account or None, start_date, end_date, page)
    return templates.TemplateResponse(request, "_partials/changes_list.html", ctx)
