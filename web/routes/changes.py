"""Changes history route: /changes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.scraper import AUDIT_STATUS
from web.auth import allowed_usernames, visible_accounts
from web.deps import STATUS_COLORS, templates
from web.services.events import read_change_rows

router = APIRouter()


def _changes_ctx(request: Request, account: str | None) -> dict:
    # The allow-list goes INTO the reader: filtering afterwards let a busy
    # account the user may not see push that user's own changes out of the
    # newest-300 window, leaving them an empty page.
    rows = read_change_rows(
        account=account, hours=24 * 30, limit=300, accounts=allowed_usernames(request)
    )
    accounts_list = visible_accounts(request)
    return {
        "rows": rows,
        "filter_account": account,
        "accounts_list": accounts_list,
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
    }


@router.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request, account: str | None = None) -> HTMLResponse:
    ctx = _changes_ctx(request, account)
    return templates.TemplateResponse(request, "changes.html", ctx)


@router.get("/partials/changes", response_class=HTMLResponse)
def partial_changes(request: Request, account: str | None = None) -> HTMLResponse:
    ctx = _changes_ctx(request, account)
    return templates.TemplateResponse(request, "_partials/changes_list.html", ctx)
