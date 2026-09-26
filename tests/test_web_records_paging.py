"""/records: pages of 50, links that keep every filter, CSV export, row ids by position."""

import csv
import io
import re

import pytest

from tests.conftest import ADMIN, GROUP_B_USER


def _rec(reg_no, status=7, name="某数据", apply_ms=1):
    return {"dataRegNo": reg_no, "dataRegName": name, "dataRegAuditStatus": status, "dataRegApplyTime": apply_ms,
            "dataRegIndustry": "制造业"}


@pytest.fixture
def many_records(fake_accounts, isolated_snapshots):
    isolated_snapshots.write("甲 公司", [_rec(f"A{i:02d}", apply_ms=1_700_000_000_000 + i) for i in range(60)])
    isolated_snapshots.write("丙公司", [_rec(f"B{i}", status=2, name="丙的数据") for i in range(3)])


def _ids(html):
    return re.findall(r'id="json-(\d+)"', html)


def test_pages_of_fifty_with_position_ids(many_records, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    html = client.get("/records").text
    assert "共 63 条记录 · 第 1/2 页" in html
    assert _ids(html) == [str(i) for i in range(1, 51)]
    assert 'hx-get="/partials/records-table?page=2"' in html

    page2 = client.get("/partials/records-table?page=2").text
    assert len(_ids(page2)) == 13
    assert 'hx-get="/partials/records-table"' in page2  # back to page 1 (no page param)


def test_every_link_keeps_the_other_filters(many_records, web_client, login_as):
    html = login_as(web_client, *ADMIN).get(
        "/records?account=ua1&status=7&q=%E6%95%B0%E6%8D%AE&start_date=2020-01-01&end_date=2030-12-31&page=2"
    ).text
    # status chip keeps account/q/dates, resets the page
    assert 'href="/records?account=ua1&amp;status=2&amp;q=%E6%95%B0%E6%8D%AE&amp;start_date=2020-01-01&amp;end_date=2030-12-31"' in html
    # account chip keeps status/q/dates
    assert 'href="/records?account=ub1&amp;status=7&amp;q=%E6%95%B0%E6%8D%AE&amp;start_date=2020-01-01&amp;end_date=2030-12-31"' in html
    # 清除 drops q and dates but keeps account/status
    assert 'href="/records?account=ua1&amp;status=7"' in html
    # export carries the filters and never a page
    assert 'href="/records.csv?account=ua1&amp;status=7&amp;q=%E6%95%B0%E6%8D%AE&amp;start_date=2020-01-01&amp;end_date=2030-12-31"' in html
    assert "第 2/2 页" in html


def test_csv_export_respects_filters_and_visibility(many_records, web_client, login_as):
    resp = login_as(web_client, *ADMIN).get("/records.csv?account=ua1")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert re.match(r'attachment; filename="records-\d{8}\.csv"', resp.headers["content-disposition"])
    assert resp.text.startswith("﻿公司,账号,登记编号")
    rows = list(csv.reader(io.StringIO(resp.text.lstrip("﻿"))))
    assert rows[0] == ["公司", "账号", "登记编号", "数据名称", "行业", "申请时间", "状态", "补正截止", "证书到期", "附件数"]
    assert len(rows) == 61 and all(r[1] == "ua1" for r in rows[1:])
    assert rows[1][2] == "A59"  # newest application first, like the page

    mine = login_as(web_client, *GROUP_B_USER).get("/records.csv").text
    body = list(csv.reader(io.StringIO(mine.lstrip("﻿"))))
    assert len(body) == 4 and {r[0] for r in body[1:]} == {"丙公司"}
    assert body[1][6] == "待补正" and body[1][7] == ""  # no notice facts → empty deadline cell


def test_out_of_range_page_clamps(many_records, web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/records?page=7").text
    assert "第 2/2 页" in html and len(_ids(html)) == 13
