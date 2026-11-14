"""关注范围: this year's applications plus last year's still in review.

The rule (src/focus.py), the ?year= values and the default (web/services/
year_scope.py), the trend data, and every page that counts or lists records:
dashboard, records page and CSV, /view, the mini program's API — plus the
settings switch. Years are relative to today so the tests do not expire.
"""

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from src import daily_stats, focus
from tests.conftest import ADMIN, API_KEY, OPERATOR_A
from web.services import year_scope

BJ = timezone(timedelta(hours=8))
Y = focus.this_year()


def _ms(y, m=3, d=1):
    return int(datetime(y, m, d, 10, tzinfo=BJ).timestamp() * 1000)


def _rec(no, status, year):
    return {"dataRegNo": no, "dataRegName": f"数据{no}", "dataRegAuditStatus": status,
            "dataRegApplyTime": _ms(year) if year else None}


IN_FOCUS = {"DIP-N1", "DIP-N2", "DIP-L1", "DIP-X1", "DIP-C1"}
OUT_OF_FOCUS = {"DIP-L2", "DIP-O1", "DIP-C2"}


@pytest.fixture
def world(fake_accounts, isolated_snapshots, isolated_settings):
    isolated_snapshots.write("甲 公司", [
        _rec("DIP-N1", 7, Y),          # this year, finished        → in
        _rec("DIP-N2", 1, Y),          # this year, in review       → in
        _rec("DIP-L1", 2, Y - 1),      # last year, 待补正           → in
        _rec("DIP-L2", 7, Y - 1),      # last year, 登记成功         → out
        _rec("DIP-O1", 1, Y - 2),      # two years ago, in review   → out
        _rec("DIP-X1", 1, None),       # no application date        → in
    ])
    isolated_snapshots.write("丙公司", [
        _rec("DIP-C1", 3, Y - 1),      # last year, 公示中           → in
        _rec("DIP-C2", 7, Y - 3),      # long done                  → out
    ])
    return isolated_settings


def _focus_on(settings, on=True):
    data = settings.read()
    data["display"] = {"focus_default": on}
    settings.write(data)


def _total(html):
    return int(re.search(r'登记总数</div>\s*<div class="stat-value">(\d+)<', html).group(1))


def _reg_nos(html):
    return set(re.findall(r"DIP-[A-Z]\d", html))


# --- the rule -----------------------------------------------------------------

@pytest.mark.parametrize("year, status, expected", [
    (2026, 7, True), (2026, 1, True), (2027, 7, True),         # this year (or later): everything
    (2025, 1, True), (2025, 2, True), (2025, 3, True), (2025, 17, True), (2025, 99, True),
    (2025, 7, False), (2025, 6, False), (2025, 11, False), (2025, 13, False),  # last year, finished
    (2024, 1, False), (2024, 2, False),                        # older: out, even if still open
    (None, 7, True),                                           # no date: cannot rule it out
])
def test_the_rule(year, status, expected):
    assert focus.in_focus(year, status, 2026) is expected


def test_only_a_real_true_turns_the_default_on(isolated_settings):
    for value, expected in ((True, True), ("true", False), (1, False), (None, False)):
        _focus_on(isolated_settings, value)
        assert focus.focus_default() is expected


def test_year_values_and_the_default(isolated_settings):
    off = year_scope.resolve(None)
    assert (off.kind, off.is_default, off.query) == ("all", True, None)
    assert year_scope.resolve("focus").query == "focus"
    assert year_scope.resolve("2025").year == 2025 and year_scope.resolve("2025").query == "2025"
    assert year_scope.resolve("garbage").kind == "all"

    _focus_on(isolated_settings)
    on = year_scope.resolve("")
    assert (on.kind, on.is_default, on.query) == ("focus", True, None)
    assert year_scope.resolve("all").query == "all"
    # The API keeps 全部年份 as its default; ?year=default asks for the console's.
    api = year_scope.resolve(None, default=year_scope.ALL)
    assert (api.kind, api.query) == ("all", None)
    asked = year_scope.resolve("default", default=year_scope.ALL)
    assert (asked.kind, asked.query) == ("focus", "focus")


def test_the_trend_counts_the_same_records():
    daily_stats._write({"2026-09-25": {"at": "x", "companies": {"甲": {
        "2026": {"7": 2, "1": 1},
        "2025": {"7": 4, "2": 1, "3": 1},
        "2024": {"1": 5},
        "none": {"1": 1},
    }}}})
    (point,) = daily_stats.series({"甲"}, "focus", 30, today=date(2026, 9, 27))
    assert point == {"date": "2026-09-25", "total": 6, "success": 2, "pending": 1}


# --- dashboard ----------------------------------------------------------------

def test_dashboard_offers_focus_when_the_default_is_all(world, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    html = client.get("/").text
    assert _total(html) == 8
    assert 'href="/?year=focus"' in html and re.search(r'href="/" class="chip active">全部年份', html)

    focused = client.get("/?year=focus").text
    assert _total(focused) == 5
    assert f"关注范围（{Y} 年申请 + {Y - 1} 年在审）" in focused
    assert 'href="/records?status=7&amp;year=focus"' in focused        # a count opens the rows it counted
    assert 'href="/records?account=ua1&amp;year=focus"' in focused
    assert 'hx-get="/partials/dashboard?year=focus"' in focused        # the live refresh keeps it


def test_dashboard_opens_on_focus_when_the_default_is_on(world, web_client, login_as):
    _focus_on(world)
    client = login_as(web_client, *ADMIN)
    html = client.get("/").text
    assert _total(html) == 5
    assert re.search(r'href="/" class="chip active"\s+title="[^"]*">关注中', html)
    assert 'href="/?year=all"' in html
    assert 'href="/records?status=7"' in html                          # the records page has the same default
    assert _total(client.get("/?year=all").text) == 8
    assert _total(client.get(f"/?year={Y - 1}").text) == 3


# --- records page and CSV -----------------------------------------------------

def test_records_page_says_what_the_years_leave_out(world, web_client, login_as):
    _focus_on(world)
    client = login_as(web_client, *ADMIN)
    html = client.get("/records").text
    assert _reg_nos(html) == IN_FOCUS
    assert "另有 3 条符合条件的未显示" in html and 'href="/records?year=all"' in html
    assert 'name="year" value=""' in html                              # typing a search keeps the default

    # A search that only matches outside the focus does not come up silently empty.
    html = client.get("/records?q=DIP-O1").text
    assert "共 0 条记录" in html and "另有 1 条符合条件的未显示" in html
    assert 'href="/records?q=DIP-O1&amp;year=all"' in html

    everything = client.get("/records?year=all").text
    assert _reg_nos(everything) == IN_FOCUS | OUT_OF_FOCUS and "未显示" not in everything
    assert 'name="year" value="all"' in everything


def test_records_year_travels_with_filters_and_csv(world, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    resp = client.get("/partials/records-table?year=focus&status=1")
    assert resp.headers["hx-replace-url"] == "/records?status=1&year=focus"
    assert _reg_nos(resp.text) == {"DIP-N2", "DIP-X1"}                   # 2 years ago's 待审核 is out
    assert 'href="/records.csv?status=1&amp;year=focus"' in resp.text

    rows = client.get("/records.csv?year=focus").text.strip().splitlines()[1:]
    assert {r.split(",")[2] for r in rows} == IN_FOCUS
    assert len(client.get("/records.csv").text.strip().splitlines()) == 1 + 8


def test_links_to_one_record_look_in_every_year(world, web_client, login_as):
    _focus_on(world)
    client = login_as(web_client, *ADMIN)
    html = client.get("/records").text
    assert re.search(r'<form class="topbar-search"[^>]*>.*?name="year" value="all"', html, re.S)
    found = client.get("/records?q=DIP-O1&year=all").text
    assert _reg_nos(found) == {"DIP-O1"}


# --- /view --------------------------------------------------------------------

def test_view_board_defaults_to_this_year_or_to_focus(world, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    assert _total(client.get("/view/").text) == 2                      # this year's dated records, as before
    _focus_on(world)
    html = client.get("/view/").text
    assert _total(html) == 5 and 'hx-get="/view/partial?year=focus"' in html
    assert _total(client.get("/view/partial?year=all").text) == 8


# --- the mini program's API ---------------------------------------------------

def test_api_keeps_all_years_unless_asked(world, web_client):
    _focus_on(world)
    key = {"X-API-Key": API_KEY}
    plain = web_client.get("/api/status", headers=key).json()
    assert plain["total_records"] == 8 and plain["year"] == "all" and plain["focus_default"] is True

    default = web_client.get("/api/status?year=default", headers=key).json()
    assert default["total_records"] == 5 and default["year"] == "focus"
    assert default["focus_note"] == focus.describe(Y)
    assert web_client.get(f"/api/status?year={Y - 1}", headers=key).json()["year"] == Y - 1

    records = web_client.get("/api/records?year=focus&page=1", headers=key).json()
    assert {r["reg_no"] for r in records["records"]} == IN_FOCUS
    assert records["outside"] == 3 and records["year_label"].startswith("关注范围")
    assert "outside" not in web_client.get("/api/records", headers=key).json()


# --- the settings switch --------------------------------------------------------

def test_settings_switch(world, web_client, login_as, isolated_control_dir):
    admin = login_as(web_client, *ADMIN)
    assert "默认关注范围" in admin.get("/settings").text
    html = admin.post("/settings/focus", data={"on": "1"}).text
    assert world.read()["display"]["focus_default"] is True and "已开启" in html
    assert admin.post("/settings/focus", data={"on": "1"}).status_code == 200   # a second click changes nothing
    assert world.read()["display"]["focus_default"] is True
    admin.post("/settings/focus", data={"on": "0"})
    assert world.read()["display"]["focus_default"] is False


def test_only_an_admin_can_flip_it(world, web_client, login_as):
    operator = login_as(web_client, *OPERATOR_A)
    operator.post("/settings/focus", data={"on": "1"})
    assert "display" not in world.read()
