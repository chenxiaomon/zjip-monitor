"""Public read-only status page — no authentication required."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from src.scraper import AUDIT_STATUS
from web import auth
from web.deps import STATUS_COLORS, account_dot, current_year, in_year, int_or_none, templates
from web.services.snapshot import get_all_latest_snapshots

router = APIRouter(prefix="/view")

# Search engines must never index the client list.
_NOINDEX = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}


def _access(request: Request) -> tuple[bool, bool]:
    """(allowed, should_set_cookie).

    Order: an authenticated console user, then a correct ?t=<token> (which
    mints the cookie), then a previously minted cookie. With no VIEW_TOKEN
    configured the page stays open, exactly as it was.
    """
    if not auth.VIEW_TOKEN:
        return True, False
    if auth.user_from_cookie(request.cookies.get(auth.COOKIE_NAME)):
        return True, False
    if auth.view_token_ok(request.query_params.get("t")):
        return True, True
    return auth.view_cookie_ok(request.cookies.get(auth.VIEW_COOKIE)), False


def _denied() -> HTMLResponse:
    """404, not 403: the page's existence is not worth confirming."""
    return HTMLResponse("页面不存在", status_code=404, headers=_NOINDEX)


def _build_ctx(year: int | None = None) -> dict:
    # Default to the current application year (was hard-coded 2026, which
    # would have silently zeroed the page on 2027-01-01).
    if year is None:
        year = current_year()

    account_cards: list[dict] = []
    total_records = 0
    success_count = 0
    pending_correction = 0

    for snap in get_all_latest_snapshots():
        status_counts: dict[int, int] = {}
        for r in snap.get("records", []):
            if not in_year(r.get("dataRegApplyTime"), year):
                continue
            code = int_or_none(r.get("dataRegAuditStatus"))
            if code is not None:
                status_counts[code] = status_counts.get(code, 0) + 1

        total = sum(status_counts.values())
        account_cards.append({
            "company": snap.get("company", "未知"),
            "status_counts": status_counts,
            "last_check": snap.get("snapshot_time"),
            "dot": account_dot(status_counts),
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
        "filter_year": year,
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
    }


def _render(request: Request, template: str, year: int | None) -> HTMLResponse:
    allowed, set_cookie = _access(request)
    if not allowed:
        return _denied()
    response = templates.TemplateResponse(
        request, template, _build_ctx(year), headers=dict(_NOINDEX)
    )
    if set_cookie:
        response.set_cookie(
            auth.VIEW_COOKIE,
            auth.make_view_cookie(),
            max_age=auth.VIEW_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/view",
        )
    return response


@router.get("/", response_class=HTMLResponse)
def view_page(request: Request, year: int | None = Query(None)) -> HTMLResponse:
    return _render(request, "view.html", year)


@router.get("/partial", response_class=HTMLResponse)
def view_partial(request: Request, year: int | None = Query(None)) -> HTMLResponse:
    """HTMX polling target — returns only the dynamic content region."""
    return _render(request, "_partials/view_content.html", year)
