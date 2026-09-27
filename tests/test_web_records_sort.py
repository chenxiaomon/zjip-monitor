"""/records: server-side sorting, status multi-select, chips that never go stale."""

import csv
import io
import re

import pytest

from tests.conftest import ADMIN
from web.routes import records


def _rec(reg_no, status, name, apply_ms):
    return {"dataRegNo": reg_no, "dataRegName": name, "dataRegAuditStatus": status, "dataRegApplyTime": apply_ms}


@pytest.fixture
def mixed(fake_accounts, isolated_snapshots):
    isolated_snapshots.write("甲 公司", [
        _rec("A-OK", 7, "b数据", 3000),
        _rec("A-FIX", 2, "a数据", 1000),
        _rec("A-WAIT", 1, "c数据", 2000),
        _rec("A-STR", "12", "d数据", "4000"),   # string status and time from the platform
    ])
    isolated_snapshots.write("丙公司", [
        _rec("B-GONE", 5, "e数据", 5000),
        _rec("B-FIX", 2, "f数据", 500),
    ])


def _order(html):
    return re.findall(r'reg_no=([A-Z]-[A-Z]+)"', html)


def test_default_order_is_newest_application_first(mixed, web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/partials/records-table").text
    assert _order(html) == ["B-GONE", "A-STR", "A-OK", "A-WAIT", "A-FIX", "B-FIX"]
    assert 'aria-sort="descending"' in html and html.count("aria-sort=") == 1


@pytest.mark.parametrize("sort, expected", [
    ("apply_asc", ["B-FIX", "A-FIX", "A-WAIT", "A-OK", "A-STR", "B-GONE"]),
    # 需处理 → 审核中 → … → 撤回/终止; newest first inside a group
    ("status", ["A-FIX", "B-FIX", "A-STR", "A-WAIT", "A-OK", "B-GONE"]),
    ("company", ["B-GONE", "B-FIX", "A-STR", "A-OK", "A-WAIT", "A-FIX"]),
    ("name", ["A-FIX", "A-OK", "A-WAIT", "A-STR", "B-GONE", "B-FIX"]),
    ("bogus", ["B-GONE", "A-STR", "A-OK", "A-WAIT", "A-FIX", "B-FIX"]),
])
def test_each_sort(mixed, web_client, login_as, sort, expected):
    html = login_as(web_client, *ADMIN).get(f"/partials/records-table?sort={sort}").text
    assert _order(html) == expected


def test_sort_headers_link_and_flip(mixed, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    html = client.get("/records?q=%E6%95%B0%E6%8D%AE").text
    assert 'href="/records?q=%E6%95%B0%E6%8D%AE&amp;sort=apply_asc"' in html   # 申请时间 flips to oldest first
    assert 'href="/records?q=%E6%95%B0%E6%8D%AE&amp;sort=status"' in html
    by_status = client.get("/records?sort=status").text
    assert 'aria-sort="ascending"' in by_status
    assert 'href="/records"' in by_status                                      # second click: back to default
    assert '<input type="hidden" name="sort" value="status">' in by_status     # typing keeps the sort


def test_status_multi_select_parsing(mixed, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    assert set(_order(client.get("/partials/records-table?status=2,7").text)) == {"A-FIX", "B-FIX", "A-OK"}
    assert set(_order(client.get("/partials/records-table?status=2&status=7").text)) == {"A-FIX", "B-FIX", "A-OK"}
    assert set(_order(client.get("/partials/records-table?status=12").text)) == {"A-STR"}   # "12" from the platform
    assert len(_order(client.get("/partials/records-table?status=").text)) == 6             # the form's blank input
    assert len(_order(client.get("/partials/records-table?status=x,,").text)) == 6


def test_chips_toggle_and_carry_the_keyword(mixed, web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/partials/records-table?status=2,7&q=%E6%95%B0%E6%8D%AE").text
    assert 'href="/records?status=2&amp;q=%E6%95%B0%E6%8D%AE"' in html          # 7 off
    assert 'href="/records?status=1,2,7&amp;q=%E6%95%B0%E6%8D%AE"' in html      # 1 on
    assert 'href="/records?q=%E6%95%B0%E6%8D%AE"' in html                       # 全部状态
    assert 'href="/records?account=ub1&amp;status=2,7&amp;q=%E6%95%B0%E6%8D%AE"' in html  # account chip in the swap unit
    assert html.count('aria-pressed="true"') == 2 and "已选 2 个状态" in html


def test_partial_responses_keep_the_address_bar_in_step(mixed, web_client, login_as):
    resp = login_as(web_client, *ADMIN).get("/partials/records-table?q=x&status=2,7&sort=name&page=1")
    assert resp.headers["hx-replace-url"] == "/records?status=2,7&q=x&sort=name"


def test_csv_follows_the_sort_and_the_statuses(mixed, web_client, login_as):
    text = login_as(web_client, *ADMIN).get("/records.csv?status=2,7&sort=status").text
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))[1:]
    assert [r[2] for r in rows] == ["A-FIX", "B-FIX", "A-OK"]


def test_build_records_accepts_one_code_or_a_set(monkeypatch):
    from types import SimpleNamespace

    acc = SimpleNamespace(company="甲", username="u")
    snap = {"records": [_rec("R1", 7, "n", 1), _rec("R2", 2, "n", 2)]}
    monkeypatch.setattr(records, "get_latest_snapshot", lambda company: snap)
    assert [r["reg_no"] for r in records._build_records([acc], None, 7)[0]] == ["R1"]
    assert [r["reg_no"] for r in records._build_records([acc], None, {2, 7})[0]] == ["R2", "R1"]
    assert len(records._build_records([acc], None, None)[0]) == 2
