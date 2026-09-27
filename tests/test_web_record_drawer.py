"""/partials/record: the detail drawer — one visible record, its notices, files and history."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from src import attachments
from tests.conftest import ADMIN, GROUP_B_USER
from web.services import attachments_svc
from web.services import events as ev

NOTICE = "《浙江省数据知识产权登记形式审查补正通知书》.pdf"
OLD_NOTICE = "《浙江省数据知识产权登记形式审查补正通知书》_2026-08-01.pdf"


@pytest.fixture
def record(fake_accounts, isolated_snapshots, monkeypatch, tmp_path):
    isolated_snapshots.write("甲 公司", [
        {"dataRegNo": "DIP-A1", "dataRegName": "甲的<数据>", "dataRegAuditStatus": 2,
         "dataRegApplyTime": 1767225600000, "dataRegIndustry": "制造业", "updateTime": 1790000000000},
        {"dataRegNo": "DIP-A2", "dataRegName": "甲的另一份", "dataRegAuditStatus": 7, "dataRegApplyTime": 1},
    ])
    isolated_snapshots.write("丙公司", [{"dataRegNo": "DIP-B1", "dataRegName": "丙的数据", "dataRegAuditStatus": 7}])
    # attachments and facts in one tree
    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", attachments._ATTACHMENTS_DIR)
    rec = attachments._ATTACHMENTS_DIR / attachments._safe_name("甲 公司") / "DIP-A1"
    rec.mkdir(parents=True)
    (rec / NOTICE).write_bytes(b"%PDF")
    (rec / ".sync.json").write_text(json.dumps({"status": 2, "facts": {
        NOTICE: {"kind": "补正", "deadline": "2099-10-12", "issued_on": "2099-09-21",
                 "reasons": "针对：风险分级区间边界存在空档\n备注：无"},
        OLD_NOTICE: {"kind": "补正", "deadline": "2099-08-20", "issued_on": "2099-08-01", "reasons": "第一轮：字段说明缺失"},
    }, "archived": {"国家局登记证书.pdf": {"archived_at": "2026-09-27T01:00:00+00:00"}}}, ensure_ascii=False), encoding="utf-8")
    # events: two changes for DIP-A1 (a year apart), one for DIP-A2, one for 丙
    now = datetime.now(timezone.utc)

    def change(account, reg_no, old, new, at):
        return {"account": account, "company": "x", "timestamp": at.isoformat(), "added": [], "removed": [],
                "changed": [{"reg_no": reg_no, "name": "n", "old_status_code": old, "new_status_code": new,
                             "old_status": {1: "待审核", 2: "待补正", 7: "登记成功"}[old],
                             "new_status": {1: "待审核", 2: "待补正", 7: "登记成功"}[new]}]}

    events = [
        change("ua1", "DIP-A1", 1, 2, now - timedelta(days=400)),
        change("ua1", "DIP-A2", 1, 7, now - timedelta(days=3)),
        change("ua1", "DIP-A1", 2, 1, now - timedelta(days=2)),
        change("ub1", "DIP-B1", 1, 7, now - timedelta(days=1)),
    ]
    monkeypatch.setattr(ev, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ev, "_EVENTS_FILE", tmp_path / "events.jsonl")
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    ev._cache.clear()


def test_admin_sees_the_whole_record(record, web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/partials/record?account=ua1&reg_no=DIP-A1").text
    assert 'class="drawer"' in html and "甲 公司" in html
    assert "甲的&lt;数据&gt;" in html                      # escaped, never raw
    assert "截止 <b>2099-10-12</b>" in html and "针对：风险分级区间边界存在空档" in html
    assert "第一轮：字段说明缺失" in html and "历史" in html  # the older round is listed too
    assert "/attachments/%E7%94%B2%20%E5%85%AC%E5%8F%B8/DIP-A1/" in html
    assert "国家局登记证书.pdf" in html and "已归档 · 2026-09-27" in html
    assert html.count('class="hist-time"') == 2               # only DIP-A1's rows, including the old one
    hist = html[html.index('class="hist"'):]
    pairs = re.findall(r's-pill-(\d+)">[^<]+</span>\s*<span class="arrow">→</span>\s*<span class="pill s-pill-(\d+)">', hist)
    assert pairs == [("2", "1"), ("1", "2")]                 # newest first
    assert "制造业" in html and "平台原始数据" in html


def test_other_groups_and_unknown_records_are_the_same_404(record, web_client, login_as):
    group_b = login_as(web_client, *GROUP_B_USER)
    for url in ("/partials/record?account=ua1&reg_no=DIP-A1",       # another group's record
                "/partials/record?account=ub1&reg_no=NOPE",          # no such record
                "/partials/record?account=ub1&reg_no=%3Cscript%3E",  # never echoed back
                "/partials/record"):
        resp = group_b.get(url)
        assert resp.status_code == 404 and resp.text == "记录不存在或无权查看"
    assert group_b.get("/partials/record?account=ub1&reg_no=DIP-B1").status_code == 200


def test_rows_todo_and_changes_open_the_drawer(record, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    table = client.get("/partials/records-table?q=DIP-A1").text
    assert 'hx-get="/partials/record?account=ua1&amp;reg_no=DIP-A1"' in table and 'hx-target="#drawer"' in table
    assert "点击行查看详情" in table
    todo = client.get("/partials/todo").text
    assert 'hx-get="/partials/record?account=ua1&amp;reg_no=DIP-A1"' in todo
    changes = client.get("/changes").text
    assert 'hx-get="/partials/record?account=ub1&amp;reg_no=DIP-B1"' in changes
    assert 'id="drawer"' in client.get("/records").text


def test_change_rows_can_be_filtered_to_one_record(record):
    since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    rows = ev.read_change_rows(account="ua1", since=since, reg_no="DIP-A1")
    assert [(r["old_status"], r["new_status"]) for r in rows] == [("待补正", "待审核"), ("待审核", "待补正")]
    assert len(ev.read_change_rows(since=since, reg_no="DIP-A1", limit=1)) == 1
    assert ev.read_change_rows(account="ua1", since=since, reg_no="NOPE") == []
    assert len(ev.read_change_rows(since=since)) == 4  # no filter: unchanged behaviour
