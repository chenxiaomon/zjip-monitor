"""期限日历: correction deadlines and certificate expiries on a month grid.

Named deadlines.py, not calendar.py, so it cannot shadow the standard
library module it uses.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src import todo
from web.auth import shown_accounts
from web.deps import templates

router = APIRouter()

WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")
CELL_ITEMS = 3


def _month(raw: str | None, today: date) -> date:
    """?month=YYYY-MM → the first of that month; anything else → this month."""
    try:
        y, m = (raw or "").split("-")
        first = date(int(y), int(m), 1)
        if 2000 <= first.year <= 2100:
            return first
    except ValueError:
        pass
    return today.replace(day=1)


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, month: str | None = None) -> HTMLResponse:
    today = todo.today_local()
    first = _month(month, today)
    data = todo.deadline_items(shown_accounts(request), today)
    key = first.strftime("%Y-%m")

    by_day: dict[str, list[dict]] = {}
    for item in data["items"]:
        by_day.setdefault(item["date"], []).append(item)
    grid = [
        [{"date": d, "iso": d.isoformat(), "in_month": d.month == first.month,
          "today": d == today, "past": d < today, "entries": by_day.get(d.isoformat(), [])}
         for d in week]
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    ]
    agenda = []
    for iso in sorted(k for k in by_day if k[:7] == key):
        d = date.fromisoformat(iso)
        agenda.append({"date": d, "weekday": WEEKDAYS[d.weekday()], "days": (d - today).days, "entries": by_day[iso]})
    month_items = [i for i in data["items"] if i["date"][:7] == key]
    overdue = [i for i in data["items"] if i["days_left"] is not None and i["days_left"] < 0]

    return templates.TemplateResponse(request, "calendar.html", {
        "first": first,
        "today": today,
        "is_current": first == today.replace(day=1),
        "prev_month": (first - timedelta(days=1)).strftime("%Y-%m"),
        "next_month": (first + timedelta(days=32)).replace(day=1).strftime("%Y-%m"),
        "weekdays": WEEKDAYS,
        "grid": grid,
        "cell_items": CELL_ITEMS,
        "agenda": agenda,
        "n_corrections": sum(1 for i in month_items if i["kind"] == "correction"),
        "n_certificates": sum(1 for i in month_items if i["kind"] == "certificate"),
        "overdue": overdue,
        "undated": data["undated"],
    })
