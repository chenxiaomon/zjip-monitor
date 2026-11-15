"""交付包（web/services/handover.py）：登记成功的证书清单 + 证书文件，打成 zip。

服务器和本机各打一次，得是同一个包——所以这里的一切只取决于快照和附件目录。
"""

import json
import zipfile
from datetime import datetime
from io import BytesIO

import pytest

from src import attachments
from src.attachments import _safe_name
from tests.test_records_ledger import _day, _ms, _val, read_xlsx
from web.services import handover

COMPANY = "示例 公司"
SNAP_TIME = "2026-11-14T02:30:00+00:00"  # 北京时间 10:30


def _rec(reg_no, status, name=None, apply="2026-03-01", **extra):
    return {"dataRegNo": reg_no, "dataRegName": name or f"{reg_no}的数据", "dataRegAuditStatus": status,
            "dataRegApplyTime": _ms(apply), "dataRegDataType": "企业数据",
            "nationalRegNo": f"SZ-{reg_no}", "nationalCertificateNo": f"NO-{reg_no}", **extra}


def _snapshot(records, **extra):
    return {"company": COMPANY, "snapshot_time": SNAP_TIME, "records": records, **extra}


def _write(root, reg_no, files=(), manifest=None):
    d = root / _safe_name(COMPANY) / _safe_name(reg_no)
    d.mkdir(parents=True, exist_ok=True)
    for name in files:
        (d / name).write_bytes(f"{reg_no}/{name}".encode())
    if manifest is not None:
        (d / ".sync.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    attachments._manifest_cache.clear()
    return d


def _synced(status, facts=None, **extra):
    return {"status": status, "synced_at": "2026-11-14T00:00:00+00:00", "files": [], "facts": facts or {}, **extra}


def _notice(valid_from, valid_to, issued="2026-01-01"):
    return {"kind": "公告", "issued_on": issued, "valid_from": valid_from, "valid_to": valid_to}


@pytest.fixture
def root(tmp_path):
    attachments._manifest_cache.clear()
    yield tmp_path / "attachments"
    attachments._manifest_cache.clear()


def _cells(pkg):
    return read_xlsx(handover.build_xlsx(pkg))[handover.SHEET]


# --- what is in it -------------------------------------------------------------

def test_only_registered_and_renewed_records_in_apply_order(root):
    records = [_rec("R3", 7, apply="2026-05-01"), _rec("R1", 7, apply="2026-01-01"), _rec("R2", 13, apply="2026-02-01"),
               _rec("P", 3), _rec("W", 1), _rec("N", 6), _rec("X", 5)]
    pkg = handover.plan(COMPANY, _snapshot(records), root)
    assert [r.reg_no for r in pkg.rows] == ["R1", "R2", "R3"]
    assert [r.status for r in pkg.rows] == ["登记成功", "续展登记", "登记成功"]
    assert pkg.open_count == 2  # 公示中、待审核；不予登记、撤回已办结
    assert handover.counts(records) == {"registered": 3, "open": 2}


def test_validity_comes_from_the_current_announcement(root):
    _write(root, "R1", ["国家局登记证书.pdf"], _synced(7, {
        "《公告》.pdf": _notice("2026-08-12", "2029-08-12"),
        "《公告》_2025-01-01.pdf": _notice("2023-01-01", "2026-01-01", issued="2023-01-01"),
    }))
    _write(root, "R2", ["国家局登记证书.pdf"], _synced(7, {"《公告》.pdf": _notice("2029-01-01", "2026-01-01")}))
    _write(root, "R3", ["国家局登记证书.pdf"], _synced(7))
    pkg = handover.plan(COMPANY, _snapshot([_rec("R1", 7), _rec("R2", 7), _rec("R3", 7)]), root)
    by = {r.reg_no: r for r in pkg.rows}
    assert by["R1"].valid_to == "2029-08-12" and by["R1"].note == ""
    assert by["R2"].valid_to is None  # 公告自相矛盾：宁可空着
    assert by["R3"].valid_to is None and "有效期未知" in by["R3"].note


def test_a_renewed_certificate_shows_no_made_up_expiry(root):
    _write(root, "R1", ["国家局登记证书.pdf"], _synced(13, {"《公告》.pdf": _notice("2023-09-01", "2026-09-01")}))
    _write(root, "R2", ["国家局登记证书.pdf"], _synced(13))
    pkg = handover.plan(COMPANY, _snapshot([_rec("R1", 13), _rec("R2", 13)]), root)
    by = {r.reg_no: r for r in pkg.rows}
    assert by["R1"].valid_to is None and by["R1"].note == "已续展；原证书有效期至 2026-09-01"
    assert by["R2"].valid_to is None and by["R2"].note == "已续展"


def test_every_certificate_state(root):
    _write(root, "HERE", ["国家局登记证书.pdf", "浙江省登记证书.pdf", "《公告》.pdf"], _synced(7))
    _write(root, "GONE", ["浙江省登记证书.pdf"], _synced(7, archived={
        "国家局登记证书.pdf": {"sha256": "0" * 64, "size": 1, "archived_at": "2026-09-26T00:00:00+00:00"}}))
    _write(root, "NONE", [], _synced(7))              # 平台没给
    _write(root, "STALE", [], _synced(3))             # 同步时还在公示，之后没再取
    _write(root, "PART", [".国家局登记证书.pdf.incoming.part"])  # 下载中，没有清单
    records = [_rec(n, 7) for n in ("HERE", "GONE", "NONE", "STALE", "PART")]
    pkg = handover.plan(COMPANY, _snapshot(records), root)
    cells = {r.reg_no: [c.cell for c in r.certs] for r in pkg.rows}
    assert cells == {
        "HERE": ["国家局登记证书", "浙江省登记证书"],
        "GONE": ["国家局登记证书（缺：已归档到本机）", "浙江省登记证书"],
        "NONE": ["国家局登记证书（缺：平台未提供）"],     # 省证书没有是常态，不算缺
        "STALE": ["国家局登记证书（缺：未下载）"],
        "PART": ["国家局登记证书（缺：未下载）"],
    }
    assert [c.arcname for c in pkg.present] == [  # 行按申请日期、登记号排
        "证书/GONE的数据_GONE_浙江省登记证书.pdf",
        "证书/HERE的数据_HERE_国家局登记证书.pdf", "证书/HERE的数据_HERE_浙江省登记证书.pdf",
    ]
    assert len(pkg.missing) == 4


def test_an_archived_provincial_certificate_is_reported(root):
    _write(root, "R1", ["国家局登记证书.pdf"], _synced(7, archived={"浙江省登记证书.pdf": {}}))
    (row,) = handover.plan(COMPANY, _snapshot([_rec("R1", 7)]), root).rows
    assert [c.cell for c in row.certs] == ["国家局登记证书", "浙江省登记证书（缺：已归档到本机）"]


def test_a_certificate_with_another_extension_is_still_packed(root):
    _write(root, "R1", ["国家局登记证书.png", "国家局登记证书_2026-07-19.pdf"], _synced(7))
    (row,) = handover.plan(COMPANY, _snapshot([_rec("R1", 7)]), root).rows
    assert [c.arcname for c in row.certs] == ["证书/R1的数据_R1_国家局登记证书.png"]  # 旧轮不进包


def test_file_names_stay_unique_short_and_legal(root):
    long = "很长的数据名称" * 20
    for reg in ("R1", "R2", "R3"):
        _write(root, reg, ["国家局登记证书.pdf"], _synced(7))
    records = [_rec("R1", 7, name="同名数据"), _rec("R2", 7, name="同名数据"), _rec("R3", 7, name='a/b:c*"d"? ' + long)]
    names = [c.arcname for c in handover.plan(COMPANY, _snapshot(records), root).present]
    assert len(set(names)) == 3
    assert names[0] == "证书/同名数据_R1_国家局登记证书.pdf" and names[1] == "证书/同名数据_R2_国家局登记证书.pdf"
    assert names[2].startswith("证书/a_b_c_d_") and not any(ch in names[2][3:] for ch in ':*"?<>|\\ ')
    assert len(names[2]) < len("证书/") + handover.NAME_CAP + len("_R3_国家局登记证书.pdf") + 1


def test_package_names_carry_the_snapshot_day_and_say_when_unfinished(root):
    done = handover.plan('甲/乙', _snapshot([_rec("R1", 7)]), root)
    assert done.zip_name == "数据知识产权登记交付包_甲_乙_20261114.zip"
    assert done.xlsx_name == "数据知识产权登记证书清单_甲_乙_20261114.xlsx"
    unfinished = handover.plan(COMPANY, _snapshot([_rec("R1", 7), _rec("R2", 2)]), root)
    assert unfinished.zip_name == f"数据知识产权登记交付包_{COMPANY}_截至20261114.zip"


# --- the workbook --------------------------------------------------------------

def test_the_sheet(root):
    _write(root, "R1", ["国家局登记证书.pdf", "浙江省登记证书.pdf"], _synced(7, {"《公告》.pdf": _notice("2026-08-12", "2029-08-12")}))
    _write(root, "R2", [], _synced(13))
    records = [_rec("R1", 7, name="=HYPERLINK(1)", apply="2026-01-05"), _rec("R2", 13, apply="2026-02-05"), _rec("W", 1)]
    c = _cells(handover.plan(COMPANY, _snapshot(records), root))

    # 总体情况：登记成功、续展登记、合计（公式带缓存值），附证书，缺国家局证书
    assert [_val(c, f"A{i}") for i in range(3, 8)] == ["登记成功", "续展登记", "合计", "附证书", "缺国家局证书"]
    assert c["B3"] == ("1", "COUNTIF($G$10:$G$1009,A3)") and _val(c, "B4") == "1"
    assert c["B5"] == ("2", "COUNTA($D$10:$D$1009)")
    assert _val(c, "C5") == "数据截至 2026-11-14 10:30（最近一次巡检）；尚有 1 条在审，未列入"
    assert (_val(c, "B6"), _val(c, "B7")) == ("2", "1")
    assert "国家局登记证书 1 张、浙江省登记证书 1 张" in _val(c, "C6")

    assert [_val(c, f"{col}9") for col in "ABCDEFGHIJKL"] == [
        "序号", "公司", "登记编号", "数据名称", "业务类别", "申请日期", "状态", "国家登记号", "证书编号",
        "有效期至", "附证书", "备注"]
    assert [_val(c, f"{col}10") for col in "BCDEGHIK"] == [
        COMPANY, "R1", "=HYPERLINK(1)", "企业数据", "登记成功", "SZ-R1", "NO-R1", "国家局登记证书、浙江省登记证书"]
    assert c["D10"][1] is None  # 像公式的名字仍是文字
    assert _day(_val(c, "F10")) == "2026-01-05 00:00" and _day(_val(c, "J10")) == "2029-08-12 00:00"
    assert _val(c, "K11") == "国家局登记证书（缺：平台未提供）" and _val(c, "L11") == "已续展"
    assert "W的数据" not in json.dumps(c, ensure_ascii=False)


def test_an_incomplete_scan_is_said(root):
    c = _cells(handover.plan(COMPANY, _snapshot([_rec("R1", 7)], fetch={"complete": False}), root))
    assert "该次巡检没有取全" in _val(c, "C4")


# --- the zip -------------------------------------------------------------------

def test_the_zip(root, tmp_path):
    _write(root, "R1", ["国家局登记证书.pdf", "浙江省登记证书.pdf"], _synced(7))
    _write(root, "R2", ["国家局登记证书.pdf"], _synced(7))
    pkg = handover.plan(COMPANY, _snapshot([_rec("R2", 7), _rec("R1", 7)]), root)
    dest = tmp_path / "x.zip"
    size = handover.write_zip(pkg, dest)
    assert size == dest.stat().st_size

    z = zipfile.ZipFile(dest)
    top = f"数据知识产权登记交付包_{COMPANY}_20261114/"
    assert z.namelist() == [
        top + f"数据知识产权登记证书清单_{COMPANY}_20261114.xlsx",
        top + "证书/R1的数据_R1_国家局登记证书.pdf",
        top + "证书/R1的数据_R1_浙江省登记证书.pdf",
        top + "证书/R2的数据_R2_国家局登记证书.pdf",
    ]
    for info in z.infolist():
        assert info.compress_type == zipfile.ZIP_STORED
        assert info.flag_bits & 0x800  # UTF-8 文件名
        assert info.date_time == (2026, 11, 14, 10, 30, 0)
    assert z.read(top + "证书/R2的数据_R2_国家局登记证书.pdf") == "R2/国家局登记证书.pdf".encode()
    assert read_xlsx(z.read(z.namelist()[0]))[handover.SHEET]


def test_the_same_inputs_give_the_same_bytes(root, tmp_path, monkeypatch):
    _write(root, "R1", ["国家局登记证书.pdf"], _synced(7))
    pkg = handover.plan(COMPANY, _snapshot([_rec("R1", 7)]), root)
    handover.write_zip(pkg, tmp_path / "a.zip")
    # 「现在」不能漏进包里（xlsxwriter 不给 created 时取 core.datetime.now）
    import xlsxwriter.core as core

    class _Clock(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, datetime)

    class Later(metaclass=_Clock):
        @staticmethod
        def now(tz=None):
            return datetime(2030, 1, 1, tzinfo=tz)
    monkeypatch.setattr(core, "datetime", Later)
    handover.write_zip(pkg, tmp_path / "b.zip")
    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()


def test_no_registered_record_gives_an_empty_plan(root):
    pkg = handover.plan(COMPANY, _snapshot([_rec("W", 1)]), root)
    assert pkg.rows == () and pkg.present == [] and pkg.total_bytes == 0
    assert BytesIO(handover.build_xlsx(pkg)).getvalue()  # still a valid (empty) sheet
