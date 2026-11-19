"""数据多了也不重复干活、不越用越占内存（按全部企业合计 1 万条记录设计）：

- 快照缓存每家只留最新一份。每轮巡检都写一个新文件名，按文件名缓存时，每一轮
  的解析结果都留在内存里，直到攒满几百上千份（控制台、巡检进程都是）。
- 登记记录页只给显示的 50 条查附件和期限；导出 CSV 只给导出的行查；小程序
  /api/records 只给这一页的行算截止日期。
- 附件信息缓存装得下一整遍：上限低于记录数时，每扫一遍就被清空一次。
- 仪表盘一次刷新只算一遍待办，横幅和待办卡片共用。
"""

import csv
import io
import json
import os
import re
from datetime import timedelta

import pytest

from src import attachments, ops_alerts, todo
from src.attachments import _safe_name
from tests.conftest import ADMIN
from web.routes import records as records_routes
from web.services import attachments_svc
from web.services import snapshot as snap_svc


def _payload(company, n):
    return {"company": company, "snapshot_time": "2026-01-01T01:00:00+00:00", "record_count": n,
            "records": [{"dataRegNo": f"R{i}", "dataRegName": "某数据", "dataRegAuditStatus": 7}
                        for i in range(n)]}


def _write_round(directory, company, minute, n):
    """One scan's snapshot — a new file name each round, as src/main.save_snapshot writes."""
    path = directory / f"{snap_svc._safe_name(company)}_2026-01-01-09{minute:02d}.json"
    path.write_text(json.dumps(_payload(company, n), ensure_ascii=False), encoding="utf-8")
    return path


# --- snapshot caches -------------------------------------------------------------

@pytest.mark.parametrize("read, cache", [
    (snap_svc.get_latest_snapshot, lambda: snap_svc._cache),   # the console
    (todo._latest_snapshot, lambda: todo._json_cache),        # 待办, in the console and the daemon
])
def test_snapshot_cache_keeps_one_file_per_company(isolated_snapshots, read, cache):
    for rnd in range(30):
        for company in ("甲 公司", "丙公司"):
            _write_round(isolated_snapshots.dir, company, rnd, rnd + 1)
            assert read(company)["record_count"] == rnd + 1
    assert len(cache()) == 2


def test_a_rewritten_snapshot_is_read_again(isolated_snapshots):
    path = _write_round(isolated_snapshots.dir, "甲 公司", 0, 1)
    assert snap_svc.get_latest_snapshot("甲 公司")["record_count"] == 1
    assert todo._latest_snapshot("甲 公司")["record_count"] == 1
    path.write_text(json.dumps(_payload("甲 公司", 5), ensure_ascii=False), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert snap_svc.get_latest_snapshot("甲 公司")["record_count"] == 5
    assert todo._latest_snapshot("甲 公司")["record_count"] == 5


# --- per-row lookups only for the rows that are shown or exported ----------------

@pytest.fixture
def big_company(fake_accounts, isolated_snapshots, isolated_settings, monkeypatch):
    """120 records of one company; returns the registration numbers whose
    attachments were looked up."""
    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", attachments._ATTACHMENTS_DIR)
    isolated_snapshots.write("甲 公司", [
        {"dataRegNo": f"A{i:03d}", "dataRegName": "某数据", "dataRegAuditStatus": 2 if i % 2 else 7,
         "dataRegApplyTime": 1_700_000_000_000 + i}
        for i in range(120)
    ])
    # The newest one has a stored certificate: the lookups must still be real.
    d = attachments._ATTACHMENTS_DIR / _safe_name("甲 公司") / "A119"
    d.mkdir(parents=True)
    (d / "国家局登记证书.pdf").write_bytes(b"%PDF")
    looked_up = []
    real = records_routes.list_attachments
    monkeypatch.setattr(records_routes, "list_attachments",
                        lambda company, reg_no: looked_up.append(reg_no) or real(company, reg_no))
    return looked_up


def test_records_page_looks_up_only_the_rows_it_shows(big_company, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    html = client.get("/records?account=ua1").text
    assert "共 120 条记录 · 第 1/3 页" in html
    assert len(big_company) == 50
    assert "/attachments/" in html and "国家局登记证书" in html  # A119, newest first

    big_company.clear()
    page3 = client.get("/partials/records-table?account=ua1&page=3").text
    assert len(big_company) == 20
    assert "国家局登记证书" not in page3


def test_csv_of_ticked_rows_looks_up_only_those(big_company, web_client, login_as):
    resp = login_as(web_client, *ADMIN).get("/records.csv?account=ua1&reg=A119,A001,A002")
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.text.lstrip("﻿"))))[1:]
    assert sorted(r[2] for r in rows) == ["A001", "A002", "A119"]
    assert sorted(big_company) == ["A001", "A002", "A119"]
    assert {r[2]: r[-1] for r in rows} == {"A119": "1", "A001": "0", "A002": "0"}  # 附件数


def test_api_records_reads_deadlines_for_the_rows_it_sends(big_company, web_client, monkeypatch):
    token = web_client.post("/api/login", json={"username": ADMIN[0], "password": ADMIN[1]}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    asked = []
    real = todo.correction_for
    monkeypatch.setattr(todo, "correction_for",
                        lambda acc, r, today=None: asked.append(r["dataRegNo"]) or real(acc, r, today))

    page = web_client.get("/api/records?page=1", headers=headers).json()
    assert page["total"] == 120 and len(page["records"]) == 50
    assert sorted(asked) == sorted(r["reg_no"] for r in page["records"])
    assert all("due" in r and "cert" in r for r in page["records"])

    asked.clear()
    everything = web_client.get("/api/records", headers=headers).json()  # old builds: every row
    assert len(everything["records"]) == 120 and len(asked) == 120
    assert all("due" in r and "cert" in r for r in everything["records"])


# --- manifest cache ------------------------------------------------------------

def test_manifest_cache_holds_a_whole_pass(monkeypatch):
    """More records than the old 4096 cap: a second walk over all of them —
    what 待办 and the banner do on every dashboard refresh — reads no file."""
    dirs = []
    for i in range(4500):
        d = attachments._ATTACHMENTS_DIR / "某公司" / f"R{i}"
        d.mkdir(parents=True)
        (d / ".sync.json").write_text('{"status": 7}', encoding="utf-8")
        dirs.append(d)
    for d in dirs:
        attachments.load_manifest_cached(d)

    reads = []
    real = attachments._load_manifest
    monkeypatch.setattr(attachments, "_load_manifest", lambda d: reads.append(d) or real(d))
    assert all(attachments.load_manifest_cached(d) == {"status": 7} for d in dirs)
    assert reads == []


# --- dashboard -------------------------------------------------------------------

def test_dashboard_builds_todo_once_and_the_banner_still_warns(
    fake_accounts, isolated_snapshots, isolated_settings, web_client, login_as, monkeypatch,
):
    monkeypatch.setattr(ops_alerts, "load_accounts", lambda: list(fake_accounts))
    isolated_snapshots.write("甲 公司", [{"dataRegNo": "R1", "dataRegName": "某数据", "dataRegAuditStatus": 2}])
    d = attachments._ATTACHMENTS_DIR / _safe_name("甲 公司") / "R1"
    d.mkdir(parents=True)
    tomorrow = (todo.today_local() + timedelta(days=1)).isoformat()
    (d / ".sync.json").write_text(json.dumps({
        "status": 2, "facts": {"补正通知书.pdf": {"kind": "补正", "deadline": tomorrow}},
    }), encoding="utf-8")
    builds = []
    real = todo.build
    monkeypatch.setattr(todo, "build", lambda accounts, today=None: builds.append(accounts) or real(accounts, today))

    html = login_as(web_client, *ADMIN).get("/partials/dashboard").text
    assert len(builds) == 1
    assert re.search(r"1 条补正 \d+ 天内到期", html)  # the banner, from the shared build
