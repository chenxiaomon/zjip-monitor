"""Read-only status board (/view) — one screen, every company you may see.

It used to be public: anyone who knew the address saw every monitored
company with its case counts. Colleagues now share the console and must not
see each other's (or the owner's) companies, so the page sits behind the
console login like everything else and shows exactly the companies the
signed-in user's role and groups allow.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from src import status_store
from src.focus import focus_default, this_year
from src.scraper import AUDIT_STATUS
from web.auth import visible_accounts
from web.deps import STATUS_COLORS, account_dot, int_or_none, templates
from web.services import year_scope
from web.services.snapshot import get_latest_snapshot

router = APIRouter(prefix="/view")

# Search engines must never index the client list.
_NOINDEX = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}


def _build_ctx(request: Request, year: str | None = None) -> dict:
    # Without ?year=: 关注范围 when the console defaults to it, otherwise the
    # current application year (was hard-coded 2026, which would have
    # silently zeroed the page on 2027-01-01).
    if not (year or "").strip() and not focus_default():
        year = str(this_year())
    scope = year_scope.resolve(year)

    account_cards: list[dict] = []
    total_records = 0
    success_count = 0
    pending_correction = 0

    statuses = status_store.get_all()
    for acc in visible_accounts(request):
        snap = get_latest_snapshot(acc.company)
        if snap is None:
            continue
        status_counts: dict[int, int] = {}
        for r in snap.get("records", []):
            if not scope.matches(r):
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
            # 停巡 (src/scan_plan.py): its data stops at the last sweep on
            # purpose — the card says so, and it goes to the end.
            "dormant": status_store.is_dormant(statuses.get(acc.username)),
        })
        total_records += total
        success_count += status_counts.get(7, 0)
        pending_correction += status_counts.get(2, 0)
    account_cards.sort(key=lambda c: c["dormant"])  # stable: each group keeps its order

    return {
        "account_cards": account_cards,
        "total_records": total_records,
        "success_count": success_count,
        "pending_correction": pending_correction,
        "year_scope": scope,
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
    }


@router.get("/", response_class=HTMLResponse)
def view_page(request: Request, year: str | None = Query(None)) -> HTMLResponse:
    return templates.TemplateResponse(request, "view.html", _build_ctx(request, year), headers=dict(_NOINDEX))


@router.get("/partial", response_class=HTMLResponse)
def view_partial(request: Request, year: str | None = Query(None)) -> HTMLResponse:
    """HTMX polling target — returns only the dynamic content region."""
    return templates.TemplateResponse(
        request, "_partials/view_content.html", _build_ctx(request, year), headers=dict(_NOINDEX)
    )
