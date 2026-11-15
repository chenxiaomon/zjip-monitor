"""导出台账、交付包、导出 CSV 跟着登记记录页的选择走：

- 不勾选：按当前筛选（状态、关键字、申请时间、年份），和表格一样；
- 勾选了（?reg=）：只要勾选的，别的筛选不再起作用（可能是在别的筛选下勾的）；
- 什么都没选：这家企业的全部，和以前一样（台账的变更记录也是全部）。
"""

import json
import re
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import pytest

from src import attachments
from src.attachments import _safe_name
from tests.conftest import OPERATOR_A
from tests.test_records_ledger import _change, _ms, events_log, read_xlsx  # noqa: F401 — events_log is a fixture
from web.services import attachments_svc, ledger

NAMES = {"R1": "甲的老证书", "R2": "甲在补正", "R3": "甲在审", "R4": "甲的新证书"}


def _rec(reg_no, status, apply, rid):
    return {"dataRegNo": reg_no, "dataRegName": NAMES[reg_no], "dataRegAuditStatus": status,
            "dataRegApplyTime": _ms(apply), "id": rid, "nationalCertificateNo": f"NO-{reg_no}"}


def _put(company, reg_no):
    d = attachments._ATTACHMENTS_DIR / _safe_name(company) / reg_no
    d.mkdir(parents=True, exist_ok=True)
    (d / "国家局登记证书.pdf").write_bytes(f"{company}/{reg_no}".encode())
    (d / ".sync.json").write_text(json.dumps({"status": 7, "synced_at": "2026-11-14T00:00:00+00:00"}),
                                  encoding="utf-8")
    attachments._manifest_cache.clear()


@pytest.fixture
def world(fake_accounts, isolated_snapshots, events_log, tmp_path, monkeypatch):  # noqa: F811
    from web.routes import records as records_routes

    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", attachments._ATTACHMENTS_DIR)
    monkeypatch.setattr(records_routes, "_HANDOVER_TMP", tmp_path / "handover-tmp")
    monkeypatch.setattr(records_routes, "attachment_disposition", lambda name: "attachment")
    isolated_snapshots.write("甲 公司", [
        _rec("R1", 7, "2025-03-01", 1), _rec("R2", 2, "2026-03-01", 2),
        _rec("R3", 12, "2026-04-01", 3), _rec("R4", 7, "2026-05-01", 4),
    ])
    _put("甲 公司", "R1")
    _put("甲 公司", "R4")
    now = datetime.now(timezone.utc).isoformat()
    events_log([{
        "account": "ua1", "company": "甲 公司", "timestamp": now, "added": [], "removed": [],
        "changed": [
            {"reg_no": "R1", "name": NAMES["R1"], "old_status_code": 3, "new_status_code": 7},
            {"reg_no": "R2", "name": NAMES["R2"], "old_status_code": 1, "new_status_code": 2},
            {"reg_no": "id:3", "name": NAMES["R3"], "old_status_code": 1, "new_status_code": 12},
            {"reg_no": "RX", "name": "已经不在的", "old_status_code": 1, "new_status_code": 2},
        ],
    }])
    yield
    attachments._manifest_cache.clear()


def _ledger(web_client, query):
    resp = web_client.get("/records/ledger.xlsx?account=ua1" + query)
    assert resp.status_code == 200, resp.text[:200]
    book = read_xlsx(resp.content)

    def names(sheet, col):
        known = set(NAMES.values()) | {"已经不在的"}
        return sorted(v for ref, (v, _) in book[sheet].items() if ref.startswith(col) and v in known)

    return names("登记明细", "D"), names("状态变更记录", "E")


def _entries(audit_dir, action):
    return [e for f in sorted(audit_dir.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()
            if (e := json.loads(ln))["action"] == action]


def test_changes_of_matches_numbers_platform_ids_and_bare_names():
    chosen = [{"dataRegNo": "R2", "id": 2, "dataRegName": "乙"}, {"dataRegNo": "", "id": 7, "dataRegName": "无号"}]
    ts = "2026-11-01T00:00:00+00:00"
    rows = [_change(ts, "R1", "甲", 1, 12), _change(ts, "R2", "乙", 1, 12), _change(ts, "id:2", "乙", 1, 2),
            _change(ts, "id:7", "无号", 1, 2), _change(ts, "(无编号)", "无号", 1, 2),
            _change(ts, "(无编号)", "别的", 1, 2)]
    kept = ledger.changes_of(chosen, rows)
    assert [(r["reg_no"], r["name"]) for r in kept] == [
        ("R2", "乙"), ("id:2", "乙"), ("id:7", "无号"), ("(无编号)", "无号")]


def test_no_choice_is_the_whole_company_as_before(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *OPERATOR_A)
    records, changes = _ledger(web_client, "")
    assert records == sorted(NAMES.values())
    assert "已经不在的" in changes  # 整家企业：变更记录照旧全部
    (entry,) = _entries(_isolated_audit, "data.records_ledger")
    assert entry["detail"] == {"rows": 4, "changes": 4}


def test_the_ledger_follows_the_filters(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *OPERATOR_A)
    both = sorted([NAMES["R2"], NAMES["R3"]])
    assert _ledger(web_client, "&status=2,12") == (both, both)
    assert _ledger(web_client, "&year=2025") == ([NAMES["R1"]], [NAMES["R1"]])
    assert _ledger(web_client, "&q=新证书") == ([NAMES["R4"]], [])
    assert _ledger(web_client, "&start_date=2026-03-15&end_date=2026-04-30") == ([NAMES["R3"]], [NAMES["R3"]])
    first = _entries(_isolated_audit, "data.records_ledger")[0]
    assert first["detail"] == {"rows": 2, "changes": 2, "statuses": [2, 12]}


def test_ticked_records_win_over_the_filters(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *OPERATOR_A)
    # 勾的时候可能是别的筛选：现在的 status=2 不再起作用
    both = sorted([NAMES["R1"], NAMES["R3"]])
    assert _ledger(web_client, "&status=2&reg=R1&reg=R3") == (both, both)
    (entry,) = _entries(_isolated_audit, "data.records_ledger")
    assert entry["detail"] == {"rows": 2, "changes": 2, "picked": 2}


def test_nothing_left_is_a_plain_404(world, web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    for query in ("&reg=GONE", "&q=没有这个"):
        resp = web_client.get("/records/ledger.xlsx?account=ua1" + query)
        assert resp.status_code == 404 and "没有符合当前筛选或勾选的记录" in resp.text


def _package(web_client, query):
    resp = web_client.get("/records/handover.zip?account=ua1" + query)
    assert resp.status_code == 200, resp.text[:200]
    return sorted(n.rsplit("/", 1)[-1] for n in zipfile.ZipFile(BytesIO(resp.content)).namelist())


def test_the_package_follows_the_filters_and_the_ticks(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *OPERATOR_A)
    whole = _package(web_client, "")
    assert [n for n in whole if n.endswith(".pdf")] == sorted([
        "甲的新证书_R4_国家局登记证书.pdf", "甲的老证书_R1_国家局登记证书.pdf"])
    assert "数据知识产权登记证书清单_甲 公司_截至20260101.xlsx" in whole  # R2、R3 在审

    this_year = _package(web_client, "&year=2026")
    assert [n for n in this_year if n.endswith(".pdf")] == ["甲的新证书_R4_国家局登记证书.pdf"]

    ticked = _package(web_client, "&year=2026&reg=R1")  # 勾选的优先，年份不管
    assert ticked == sorted(["数据知识产权登记证书清单_甲 公司_20260101.xlsx",  # 勾的都已登记：不写「截至」
                             "甲的老证书_R1_国家局登记证书.pdf"])

    details = [e["detail"] for e in _entries(_isolated_audit, "data.records_handover")]
    assert [(d["rows"], d["open"], d.get("year"), d.get("picked")) for d in details] == [
        (2, 2, None, None), (1, 2, "2026", None), (1, 0, None, 1)]


def test_a_choice_without_registered_records_says_so(world, web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    resp = web_client.get("/records/handover.zip?account=ua1&status=2")
    assert resp.status_code == 404 and resp.text == "所选的记录里没有登记成功的"


def test_csv_ticks_win_over_the_filters(world, web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    body = web_client.get("/records.csv?account=ua1&status=2&reg=R1&reg=R4").content.decode("utf-8-sig")
    assert sorted(line.split(",")[2] for line in body.splitlines()[1:]) == ["R1", "R4"]
    filtered = web_client.get("/records.csv?account=ua1&status=2").content.decode("utf-8-sig")
    assert [line.split(",")[2] for line in filtered.splitlines()[1:]] == ["R2"]


def test_page_links_carry_the_filters_and_rows_carry_tick_boxes(world, web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    page = web_client.get("/records?account=ua1&status=2,7&year=2026&q=甲").text
    query = "account=ua1&amp;status=2,7&amp;q=%E7%94%B2&amp;year=2026"  # 不带页码、排序
    assert f'href="/records/ledger.xlsx?{query}"' in page
    assert f'href="/records/handover.zip?{query}"' in page
    assert 'data-registered="1" data-open="1"' in page and "交付包（尚有 1 条在审）" in page
    assert 'data-pick-key="pick:ua1"' in page and 'id="pick-page"' in page
    assert re.search(r'class="rec-pick" value="R4"[^>]*data-reg="1"\s+data-open="0"', page)
    assert re.search(r'class="rec-pick" value="R2"[^>]*data-reg="0"\s+data-open="1"', page)
    assert 'value="R1"' not in page  # 筛掉的不出现

    # 筛选里没有登记成功的：交付包按钮在、但藏着，勾了登记成功的记录时页面脚本再显示
    in_review = web_client.get("/records?account=ua1&status=12").text
    assert 'data-registered="0" data-open="1" hidden' in in_review

    everyone = web_client.get("/records").text
    assert "rec-pick" not in everyone and "pick-page" not in everyone and "data-pick-key" not in everyone
