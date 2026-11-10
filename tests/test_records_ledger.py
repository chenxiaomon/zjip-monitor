"""进度台账（/records/ledger.xlsx）：一家企业的登记明细 + 状态变更记录。

xlsx 用标准库读回（zipfile + ElementTree），不为测试多装 openpyxl。
"""

import json
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import quote

import pytest

from tests.conftest import GROUP_A_USER, GROUP_B_USER, OPERATOR_A
from web.services import ledger

_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_EXCEL_EPOCH = datetime(1899, 12, 30)


def read_xlsx(data: bytes) -> dict[str, dict[str, tuple[str | None, str | None]]]:
    """{sheet name: {"A1": (value, formula)}} — value is what a viewer that
    does not recalculate shows (shared string, number, or a formula's cached
    result)."""
    z = zipfile.ZipFile(BytesIO(data))
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(_M + "si"):
            strings.append("".join(t.text or "" for t in si.iter(_M + "t")))
    names = [s.get("name") for s in ET.fromstring(z.read("xl/workbook.xml")).iter(_M + "sheet")]
    out = {}
    for i, name in enumerate(names, 1):
        cells = {}
        for c in ET.fromstring(z.read(f"xl/worksheets/sheet{i}.xml")).iter(_M + "c"):
            v, f = c.find(_M + "v"), c.find(_M + "f")
            value = None
            if v is not None:
                value = strings[int(v.text)] if c.get("t") == "s" else v.text
            if value is not None or f is not None:
                cells[c.get("r")] = (value, f.text if f is not None else None)
        out[name] = cells
    return out


def _val(cells, ref):
    return cells.get(ref, (None, None))[0]


def _day(serial) -> str:
    return (_EXCEL_EPOCH + timedelta(days=float(serial))).strftime("%Y-%m-%d %H:%M")


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day + "T10:00:00+08:00").timestamp() * 1000)


def _change(ts, reg_no, name, old, new, kind="changed"):
    from src.scraper import decode_status
    return {"type": kind, "timestamp": ts, "reg_no": reg_no, "name": name,
            "old_status_code": old, "new_status_code": new,
            "old_status": decode_status(old) if old else "", "new_status": decode_status(new) if new else ""}


RECORDS = [
    {"dataRegNo": "R003", "dataRegName": "数据丙", "dataRegAuditStatus": 3,
     "dataRegApplyTime": _ms("2026-09-20"), "dataRegDataType": "视觉检测", "id": 3},
    {"dataRegNo": "R001", "dataRegName": "数据甲", "dataRegAuditStatus": 7,
     "dataRegApplyTime": _ms("2026-09-01"), "dataRegDataType": "注塑工艺", "id": 1},
    {"dataRegNo": "R002", "dataRegName": "数据乙", "dataRegAuditStatus": 12,
     "dataRegApplyTime": _ms("2026-09-05"), "dataRegDataType": "生产管理", "id": 2},
    {"dataRegNo": "R004", "dataRegName": '=HYPERLINK("http://x","点我")', "dataRegAuditStatus": 17,
     "dataRegApplyTime": _ms("2026-09-21"), "id": 4},
    {"dataRegNo": "R005", "dataRegName": "数据戊", "dataRegAuditStatus": 2,
     "dataRegApplyTime": _ms("2026-10-02"), "id": 5},
]
CHANGES = [  # read_change_rows order: newest first
    _change("2026-11-08T12:21:00+00:00", "R002", "数据乙", 1, 12),
    _change("2026-11-07T01:00:00+00:00", "R002", "数据乙", 2, 1),
    _change("2026-11-01T01:00:00+00:00", "R004", RECORDS[3]["dataRegName"], 3, 17),
    {"type": "removed", "timestamp": "2026-10-30T01:00:00+00:00", "reg_no": "R009",
     "name": "抓取异常消失的", "old_status_code": 1, "new_status_code": None},
    _change("2026-10-20T01:00:00+00:00", "R002", "数据乙", 1, 2),
    _change("2026-10-15T01:00:00+00:00", "id:1", "数据甲", 3, 7),  # before it had a number
    _change("2026-10-10T01:00:00+00:00", "R005", "数据戊", None, 1, kind="added"),
]


@pytest.fixture(scope="module")
def book():
    data = ledger.build_ledger("示例公司", RECORDS, CHANGES, corrections_noticed={"R005": 2},
                               as_of="2026-11-09T01:00:32+00:00")
    return read_xlsx(data)


def test_two_sheets_in_the_hand_made_order(book):
    assert list(book) == ["登记明细", "状态变更记录"]


def test_overview_counts_each_status_with_formula_and_cached_result(book):
    s = book["登记明细"]
    rows = {_val(s, f"A{r}"): s[f"B{r}"] for r in range(3, 10)}
    # The four fixed rows, then a status this company has that they do not cover.
    assert [_val(s, f"A{r}") for r in range(3, 8)] == ["登记成功", "公示中", "待审核", "待补正", "待发证"]
    assert {k: v[0] for k, v in rows.items()} == {
        "登记成功": "1", "公示中": "1", "待审核": "1", "待补正": "1", "待发证": "1",
        "合计": "5", "已通过审核（含公示中）": "3",
    }
    assert rows["待审核"][1].startswith("COUNTIF($G$12:$G$")  # recomputes when a status is edited
    assert _val(s, "C8") == "数据截至 2026-11-09 09:00（最近一次巡检）"
    assert _val(s, "C9") == "公示中、登记成功、待发证的申请均已通过审核"


def test_detail_rows_by_application_date_with_corrections_and_latest_change(book):
    s = book["登记明细"]
    assert [_val(s, f"{c}11") for c in "ABCDEFGHIJK"] == [
        "序号", "公司", "登记编号", "数据名称", "业务类别", "申请日期", "状态", "补正次数",
        "最近变更时间", "最近变化", "备注"]
    assert [_val(s, f"C{r}") for r in range(12, 17)] == ["R001", "R002", "R003", "R004", "R005"]
    r001, r002, r003, r004, r005 = range(12, 17)
    assert _val(s, f"E{r001}") == "注塑工艺"  # 业务类别 = 平台的「数据类型」
    assert _day(_val(s, f"F{r001}")) == "2026-09-01 00:00"
    # 1 → 12: both are 待审核 — the platform moved it along inside review.
    assert (_val(s, f"G{r002}"), _val(s, f"J{r002}")) == ("待审核", "审核环节更新")
    assert _day(_val(s, f"I{r002}")) == "2026-11-08 20:21"
    assert _val(s, f"H{r002}") == "1"  # went into 待补正 once
    # A change logged before the record got its number still belongs to it.
    assert _val(s, f"J{r001}") == "登记成功"
    assert _val(s, f"I{r003}") is None and _val(s, f"H{r003}") == "0"
    assert _val(s, f"J{r004}") == "公示中→待发证"
    # Two correction notices on file beat the single entry in the change log.
    assert (_val(s, f"H{r005}"), _val(s, f"J{r005}")) == ("2", "新增申请")


def test_a_name_that_looks_like_a_formula_stays_text(book):
    s = book["登记明细"]
    value, formula = s["D15"]
    assert value == RECORDS[3]["dataRegName"] and formula is None


def test_change_sheet_newest_first_without_vanished_records(book):
    s = book["状态变更记录"]
    header = next(r for r in range(1, 30) if _val(s, f"A{r}") == "序号")
    rows = [[_val(s, f"{c}{r}") for c in "CDEFGH"] for r in range(header + 1, header + 7)]
    assert rows == [
        ["示例公司", "R002", "数据乙", "待审核", "待审核", "审核环节更新"],
        ["示例公司", "R002", "数据乙", "待补正", "待审核", "补正已提交"],
        ["示例公司", "R004", RECORDS[3]["dataRegName"], "公示中", "待发证", "公示中→待发证"],
        ["示例公司", "R002", "数据乙", "待审核", "待补正", "需补正"],
        ["示例公司", "R001", "数据甲", "公示中", "登记成功", "登记成功"],  # id:1 → its number now
        ["示例公司", "R005", "数据戊", "", "待审核", "新增申请"],
    ]
    assert _val(s, f"A{header + 7}") is None  # 记录消失 is a scan anomaly, not progress
    assert "抓取异常消失的" not in json.dumps(s, ensure_ascii=False)


def test_change_overview_counts_and_span(book):
    s = book["状态变更记录"]
    summary = {_val(s, f"A{r}"): (_val(s, f"B{r}"), _val(s, f"C{r}")) for r in range(3, 11)}
    assert summary == {
        "进入公示": ("0", "0"), "登记成功": ("1", "1"), "需补正": ("1", "1"), "补正已提交": ("1", "1"),
        "审核环节更新": ("1", "1"), "新增申请": ("1", "1"), "公示中→待发证": ("1", "1"),
        "合计": ("6", "4"),
    }
    assert _val(s, "D10") == "记录区间：2026-10-10 09:00 至 2026-11-08 20:21，按时间倒序排列"


def test_no_changes_yet():
    s = read_xlsx(ledger.build_ledger("示例公司", RECORDS[:1], []))["状态变更记录"]
    assert _val(s, "B8") == "0"
    assert _val(s, "D8") == "开始监控以来暂无状态变化"
    assert _val(s, "A11") == "（开始监控以来暂无状态变化）"


@pytest.mark.parametrize("before,after,added,note", [
    ("待审核", "待审核", False, "审核环节更新"),
    ("待审核", "待补正", False, "需补正"),
    ("待补正", "待审核", False, "补正已提交"),
    ("待审核", "公示中", False, "进入公示"),
    ("待发证", "登记成功", False, "登记成功"),
    ("公示中", "异议", False, "公示中→异议"),
    ("", "待审核", True, "新增申请"),
])
def test_describe_change_follows_the_hand_made_formula(before, after, added, note):
    assert ledger.describe_change(before, after, added=added) == note


def test_filename_drops_characters_files_cannot_hold():
    assert ledger.filename('甲/乙:"丙"', date(2026, 11, 9)) == "数据知识产权登记进度台账_甲_乙__丙__20261109.xlsx"


# --- route ---------------------------------------------------------------------

@pytest.fixture
def events_log(tmp_path, monkeypatch):
    from web.services import events as ev

    def write(events):
        (tmp_path / "events.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
        ev._cache.clear()

    monkeypatch.setattr(ev, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ev, "_EVENTS_FILE", tmp_path / "events.jsonl")
    write([])
    yield write
    ev._cache.clear()


@pytest.fixture
def world(fake_accounts, isolated_snapshots, events_log):
    isolated_snapshots.write("甲 公司", [{"dataRegNo": "R1", "dataRegName": "甲的数据", "id": 1,
                                          "dataRegAuditStatus": 12, "dataRegApplyTime": _ms("2026-09-01")}])
    isolated_snapshots.write("丙公司", [{"dataRegNo": "RB", "dataRegName": "丙的数据", "id": 9,
                                         "dataRegAuditStatus": 7, "dataRegApplyTime": _ms("2026-09-01")}])
    now = datetime.now(timezone.utc).isoformat()
    events_log([
        {"account": "ua1", "company": "甲 公司", "timestamp": now, "added": [], "removed": [],
         "changed": [{"reg_no": "R1", "name": "甲的数据", "old_status_code": 1, "new_status_code": 12}]},
        {"account": "ub1", "company": "丙公司", "timestamp": now, "added": [], "removed": [],
         "changed": [{"reg_no": "RB", "name": "丙的数据", "old_status_code": 3, "new_status_code": 7}]},
    ])


def _audit(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


def test_download_name_header_carries_the_chinese_name_twice():
    from web.deps import attachment_disposition

    name = "数据知识产权登记进度台账_甲 公司_20261109.xlsx"
    header = attachment_disposition(name)
    raw = header.encode("latin-1")  # what Starlette puts on the wire
    assert f'filename="{name}"'.encode("utf-8") in raw
    assert f"filename*=utf-8''{quote(name)}" in header


@pytest.mark.parametrize("user", [OPERATOR_A, GROUP_A_USER])  # 只读用户也能导出自己分组的
def test_exports_a_visible_company(world, web_client, login_as, user, _isolated_audit, monkeypatch):
    # Starlette's TestClient decodes raw UTF-8 header bytes and then trips
    # over its own ASCII check (uvicorn sends them as they are), so the
    # header itself is tested above and here only the name the route picks.
    from web.routes import records as records_routes

    names = []
    monkeypatch.setattr(records_routes, "attachment_disposition",
                        lambda name: names.append(name) or "attachment")
    login_as(web_client, *user)
    resp = web_client.get("/records/ledger.xlsx?account=ua1")
    assert resp.status_code == 200, resp.text[:200]
    assert resp.headers["content-type"] == ledger.MEDIA_TYPE
    assert resp.headers["cache-control"] == "no-store"
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    assert names == [f"数据知识产权登记进度台账_甲 公司_{today}.xlsx"]

    book = read_xlsx(resp.content)
    assert _val(book["登记明细"], "D11") == "甲的数据"
    assert _val(book["状态变更记录"], "H11") == "审核环节更新"
    assert "丙" not in json.dumps(book, ensure_ascii=False)

    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] == "data.records_ledger"]
    assert entry["company"] == "甲 公司" and entry["detail"] == {"rows": 1, "changes": 1}


def test_another_groups_company_is_the_same_404_as_none(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *GROUP_B_USER)
    theirs = web_client.get("/records/ledger.xlsx?account=ua1")
    missing = web_client.get("/records/ledger.xlsx?account=no-such")
    bare = web_client.get("/records/ledger.xlsx")
    assert theirs.status_code == missing.status_code == bare.status_code == 404
    assert theirs.text == missing.text == bare.text
    assert not [e for e in _audit(_isolated_audit) if e["action"] == "data.records_ledger"]


def test_records_page_offers_the_ledger_once_a_company_is_picked(world, web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    picked = web_client.get("/records?account=ua1").text
    assert 'href="/records/ledger.xlsx?account=ua1"' in picked
    everyone = web_client.get("/records").text
    assert "/records/ledger.xlsx" not in everyone and "选一家企业可导出台账" in everyone
