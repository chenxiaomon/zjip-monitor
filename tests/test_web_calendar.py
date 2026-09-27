"""/calendar: correction deadlines and certificate expiries on a month grid, scoped per group."""

import json
import re
from datetime import date

import pytest

from src import attachments, todo
from tests.conftest import ADMIN, GROUP_A_USER

NOTICE = "《浙江省数据知识产权登记形式审查补正通知书》.pdf"
GAZETTE = "《浙江省数据知识产权登记登记公告通知书》.pdf"


def _facts(company, reg_no, facts):
    rec = attachments._ATTACHMENTS_DIR / attachments._safe_name(company) / reg_no
    rec.mkdir(parents=True, exist_ok=True)
    (rec / ".sync.json").write_text(json.dumps({"facts": facts}, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def deadlines(fake_accounts, isolated_snapshots):
    isolated_snapshots.write("甲 公司", [
        {"dataRegNo": "A-CORR", "dataRegName": "甲的补正", "dataRegAuditStatus": 2},
        {"dataRegNo": "A-UNDATED", "dataRegName": "甲的无日期补正", "dataRegAuditStatus": 2},
        {"dataRegNo": "A-CERT", "dataRegName": "甲的证书", "dataRegAuditStatus": 7},
        {"dataRegNo": "A-LATE", "dataRegName": "甲的逾期补正", "dataRegAuditStatus": 2},
    ])
    isolated_snapshots.write("丙公司", [{"dataRegNo": "B-CORR", "dataRegName": "丙的补正", "dataRegAuditStatus": 2}])
    _facts("甲 公司", "A-CORR", {NOTICE: {"kind": "补正", "deadline": "2099-10-12", "issued_on": "2099-09-21"}})
    _facts("甲 公司", "A-UNDATED", {NOTICE: {"kind": "补正", "deadline": None}})
    _facts("甲 公司", "A-CERT", {GAZETTE: {"kind": "公告", "valid_from": "2096-10-20", "valid_to": "2099-10-20"}})
    _facts("甲 公司", "A-LATE", {NOTICE: {"kind": "补正", "deadline": "2020-01-05"}})
    _facts("丙公司", "B-CORR", {NOTICE: {"kind": "补正", "deadline": "2099-10-05"}})


def _cell(html, day):
    """The markup of the in-month cell for `day`."""
    cells = re.findall(r'<div class="cal-day[^"]*" role="gridcell"[^>]*>(.*?)</div>\s*(?=<div class="cal-day|</div>\s*</div>)', html, re.S)
    for c in cells:
        if re.search(rf'<div class="cal-num">{day}</div>', c):
            return c
    raise AssertionError(f"no cell for {day}")


def test_items_land_on_their_days_and_link_to_the_drawer(deadlines, web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/calendar?month=2099-10").text
    assert "2099 年 10 月" in html
    assert "补正 甲 公司" in _cell(html, 12) and "证书 甲 公司" in _cell(html, 20) and "补正 丙公司" in _cell(html, 5)
    assert 'hx-get="/partials/record?account=ua1&amp;reg_no=A-CORR"' in html
    assert "补正截止 2" in html and "证书到期 1" in html
    assert "10 月 12 日" in html and "甲 公司 · 甲的补正" in html        # the agenda
    assert "截止日期未知 1" in html and "甲的无日期补正" in html
    assert "已逾期 1" in html and "甲的逾期补正" in html                 # overdue shows in every month


def test_group_users_see_only_their_companies(deadlines, web_client, login_as):
    html = login_as(web_client, *GROUP_A_USER).get("/calendar?month=2099-10").text
    assert "丙公司" not in html and "甲的补正" in html


def test_month_navigation_and_bad_months(deadlines, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    dec = client.get("/calendar?month=2099-12").text
    assert 'href="/calendar?month=2099-11"' in dec and 'href="/calendar?month=2100-01"' in dec
    assert "回到本月" in dec
    today = todo.today_local()
    for bad in ("nonsense", "2099-13", "1800-01", ""):
        page = client.get(f"/calendar?month={bad}").text
        assert f"{today.year} 年 {today.month} 月" in page and "回到本月" not in page


def test_nav_has_the_calendar_for_everyone(deadlines, web_client, login_as):
    html = login_as(web_client, *GROUP_A_USER).get("/").text
    assert 'href="/calendar"' in html


def test_deadline_items_includes_certificates_beyond_the_renewal_window(deadlines, fake_accounts):
    data = todo.deadline_items(fake_accounts, today=date(2099, 1, 1))
    kinds = {(i["reg_no"], i["kind"], i["date"]) for i in data["items"]}
    assert ("A-CERT", "certificate", "2099-10-20") in kinds          # 292 days out: outside build()'s window
    assert [i["reg_no"] for i in data["undated"]] == ["A-UNDATED"]
    cert = next(i for i in data["items"] if i["reg_no"] == "A-CERT")
    assert cert["urgency"] == "unknown" and cert["days_left"] == 292
    assert [i["date"] for i in data["items"]] == sorted(i["date"] for i in data["items"])
    # the window default is unchanged for the dashboard's 可续展 list
    assert todo.build(fake_accounts, today=date(2099, 1, 1))["renewals"] == []


def test_certificate_urgency_steps():
    assert [todo.certificate_urgency(d) for d in (-1, 0, 30, 31, 180, 181)] == \
        ["overdue", "amber", "amber", "normal", "normal", "unknown"]
