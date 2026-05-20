"""Changes history route: /changes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.config_loader import ConfigError, load_accounts
from src.scraper import AUDIT_STATUS
from web.deps import STATUS_COLORS, templates
from web.services.events import read_change_rows

router = APIRouter()


def _changes_ctx(account: str | None) -> dict:
    rows = read_change_rows(account=account, hours=24 * 30, limit=300)
    try:
        accounts_list = load_accounts()
    except ConfigError:
        accounts_list = []
    return {
        "rows": rows,
        "filter_account": account,
        "accounts_list": accounts_list,
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
    }


@router.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request, account: str | None = None) -> HTMLResponse:
    ctx = _changes_ctx(account)
    return templates.TemplateResponse(request, "changes.html", ctx)


@router.get("/partials/changes", response_class=HTMLResponse)
def partial_changes(request: Request, account: str | None = None) -> HTMLResponse:
    ctx = _changes_ctx(account)
    return templates.TemplateResponse(request, "_partials/changes_list.html", ctx)
