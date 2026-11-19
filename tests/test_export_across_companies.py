"""勾选可以跨几家企业（勾选不分企业存，换企业还在）：

- 导出 CSV：一个文件，勾的几家都在里面；
- 导出台账：每家一份 xlsx（和单独导出那家的一样），几家时打成一个 zip；
- 交付包：一个 zip，每家一个文件夹，没有登记成功记录的那家不放；
- 勾选只看权限：上方选的是哪家不限制勾选，但看不到的企业的记录怎么也导不出来。
"""

import csv
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from src import attachments
from src.attachments import _safe_name
from tests.conftest import ADMIN, GROUP_B_USER, OPERATOR_A
from tests.test_records_ledger import _ms, events_log, read_xlsx  # noqa: F401 — events_log is a fixture
from web.services import attachments_svc, ledger

TODAY = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")


def _rec(reg_no, status, name):
    return {"dataRegNo": reg_no, "dataRegName": name, "dataRegAuditStatus": status,
            "dataRegApplyTime": _ms("2026-03-01"), "id": reg_no, "nationalCertificateNo": f"NO-{reg_no}"}


def _cert(company, reg_no):
    d = attachments._ATTACHMENTS_DIR / _safe_name(company) / reg_no
    d.mkdir(parents=True, exist_ok=True)
    (d / "国家局登记证书.pdf").write_bytes(f"{company}/{reg_no}".encode())
    (d / ".sync.json").write_text(json.dumps({"status": 7, "synced_at": "2026-11-14T00:00:00+00:00"}),
                                  encoding="utf-8")
    attachments._manifest_cache.clear()


@pytest.fixture
def world(fake_accounts, isolated_snapshots, events_log, tmp_path, monkeypatch):  # noqa: F811
    """甲、乙 in group A, 丙 in group B. Returns the download names the routes gave."""
    from web.routes import records as records_routes

    names = []
    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", attachments._ATTACHMENTS_DIR)
    monkeypatch.setattr(records_routes, "_HANDOVER_TMP", tmp_path / "handover-tmp")
    # TestClient cannot carry a UTF-8 header; the real one is tested in test_records_ledger
    monkeypatch.setattr(records_routes, "attachment_disposition", lambda name: names.append(name) or "attachment")
    isolated_snapshots.write("甲 公司", [_rec("A1", 7, "甲的证书"), _rec("A2", 2, "甲在补正"), _rec("A3", 7, "甲没勾的")])
    isolated_snapshots.write("乙·公司", [_rec("B1", 7, "乙的证书"), _rec("B2", 12, "乙在审")])
    isolated_snapshots.write("丙公司", [_rec("C1", 7, "丙的证书")])
    for company, reg in (("甲 公司", "A1"), ("甲 公司", "A3"), ("乙·公司", "B1"), ("丙公司", "C1")):
        _cert(company, reg)
    now = datetime.now(timezone.utc).isoformat()
    events_log([
        {"account": "ua1", "company": "甲 公司", "timestamp": now, "added": [], "removed": [],
         "changed": [{"reg_no": "A1", "name": "甲的证书", "old_status_code": 3, "new_status_code": 7},
                     {"reg_no": "A3", "name": "甲没勾的", "old_status_code": 3, "new_status_code": 7}]},
        {"account": "ua2", "company": "乙·公司", "timestamp": now, "added": [], "removed": [],
         "changed": [{"reg_no": "B1", "name": "乙的证书", "old_status_code": 3, "new_status_code": 7}]},
    ])
    yield names
    attachments._manifest_cache.clear()


def _audit(audit_dir, action):
    return [e for f in sorted(audit_dir.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()
            if (e := json.loads(ln))["action"] == action]


def _names(book: bytes, sheet: str, col: str) -> set[str]:
    known = {"甲的证书", "甲在补正", "甲没勾的", "乙的证书", "乙在审", "丙的证书"}
    return {v for ref, (v, _) in read_xlsx(book)[sheet].items() if ref.startswith(col) and v in known}


def test_csv_takes_ticks_from_several_companies(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *OPERATOR_A)
    # the chips show 甲; the ticks are in 甲 and 乙
    body = web_client.get("/records.csv?account=ua1&reg=A1&reg=B1").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(body)))[1:]
    assert sorted((r[0], r[2]) for r in rows) == [("乙·公司", "B1"), ("甲 公司", "A1")]
    (entry,) = _audit(_isolated_audit, "data.records_csv")
    assert entry["detail"] == {"account": "ua1", "picked": 2, "companies": 2, "rows": 2}


def test_ledger_of_ticks_in_two_companies_is_a_zip_with_one_each(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *OPERATOR_A)
    resp = web_client.get("/records/ledger.xlsx?account=ua1&reg=A1&reg=A2&reg=B1")
    assert resp.status_code == 200, resp.text[:200]
    assert resp.headers["content-type"] == "application/zip"
    assert world[-1] == f"数据知识产权登记进度台账_甲 公司等2家_{TODAY}.zip"
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    jia, yi = f"数据知识产权登记进度台账_甲 公司_{TODAY}.xlsx", f"数据知识产权登记进度台账_乙·公司_{TODAY}.xlsx"
    assert z.namelist() == [jia, yi]
    # each company's book holds its own ticked records, and only their changes
    assert _names(z.read(jia), "登记明细", "D") == {"甲的证书", "甲在补正"}
    assert _names(z.read(jia), "状态变更记录", "E") == {"甲的证书"}
    assert _names(z.read(yi), "登记明细", "D") == {"乙的证书"}
    assert _names(z.read(yi), "状态变更记录", "E") == {"乙的证书"}

    entries = _audit(_isolated_audit, "data.records_ledger")
    assert [(e["company"], e["detail"]) for e in entries] == [
        ("甲 公司", {"rows": 2, "changes": 1, "picked": 2, "companies": 2}),
        ("乙·公司", {"rows": 1, "changes": 1, "picked": 1, "companies": 2}),
    ]


def test_ticks_in_one_company_still_give_one_xlsx(world, web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    # ticked in 甲, then the chips moved to 乙: the ticks still decide
    resp = web_client.get("/records/ledger.xlsx?account=ua2&reg=A1")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == ledger.MEDIA_TYPE
    assert world[-1] == f"数据知识产权登记进度台账_甲 公司_{TODAY}.xlsx"
    assert _names(resp.content, "登记明细", "D") == {"甲的证书"}


def test_handover_of_ticks_in_two_companies_has_a_folder_each(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *OPERATOR_A)
    resp = web_client.get("/records/handover.zip?reg=A1&reg=B1&reg=B2")
    assert resp.status_code == 200, resp.text[:200]
    assert world[-1] == f"数据知识产权登记交付包_甲 公司等2家_{TODAY}.zip"
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    folders = sorted({n.split("/", 1)[0] for n in z.namelist()})
    assert len(folders) == 2
    jia = next(f for f in folders if "甲 公司" in f)
    yi = next(f for f in folders if "乙·公司" in f)
    # each folder is that company's own package: 乙's ticked B2 is still in review
    assert "截至" in yi and "截至" not in jia
    certs = {n for n in z.namelist() if n.endswith(".pdf")}
    assert certs == {f"{jia}/证书/甲的证书_A1_国家局登记证书.pdf", f"{yi}/证书/乙的证书_B1_国家局登记证书.pdf"}
    assert z.read(f"{jia}/证书/甲的证书_A1_国家局登记证书.pdf") == "甲 公司/A1".encode()  # unticked A3 is not in
    entries = _audit(_isolated_audit, "data.records_handover")
    assert [(e["company"], e["detail"]["rows"], e["detail"]["picked"], e["detail"]["companies"])
            for e in entries] == [("甲 公司", 1, 1, 2), ("乙·公司", 1, 2, 2)]


def test_a_company_with_nothing_registered_ticked_gets_no_folder(world, web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    resp = web_client.get("/records/handover.zip?reg=A2&reg=B1")  # 甲's tick is 待补正
    assert resp.status_code == 200
    assert world[-1].startswith("数据知识产权登记交付包_乙·公司_")  # one package: its own name
    assert {n.split("/", 1)[0] for n in zipfile.ZipFile(io.BytesIO(resp.content)).namelist()} == {
        world[-1].removesuffix(".zip")}


def test_ticks_never_reach_past_what_the_caller_may_see(world, web_client, login_as):
    login_as(web_client, *GROUP_B_USER)  # sees 丙 only
    body = web_client.get("/records.csv?reg=A1&reg=C1").content.decode("utf-8-sig")
    assert [r[2] for r in list(csv.reader(io.StringIO(body)))[1:]] == ["C1"]
    for path in ("/records/ledger.xlsx", "/records/handover.zip"):
        alone = web_client.get(f"{path}?account=ua1&reg=A1&reg=B1")
        assert alone.status_code == 404 and "甲" not in alone.text and "乙" not in alone.text
    mixed = web_client.get("/records/ledger.xlsx?reg=A1&reg=C1")
    assert mixed.status_code == 200 and mixed.headers["content-type"] == ledger.MEDIA_TYPE
    assert world[-1].startswith("数据知识产权登记进度台账_丙公司_")
    assert _names(mixed.content, "登记明细", "D") == {"丙的证书"}


def test_the_admin_ticks_across_groups(world, web_client, login_as):
    login_as(web_client, *ADMIN)
    resp = web_client.get("/records/ledger.xlsx?reg=A1&reg=C1")
    assert resp.headers["content-type"] == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(resp.content)).namelist() == [
        f"数据知识产权登记进度台账_甲 公司_{TODAY}.xlsx", f"数据知识产权登记进度台账_丙公司_{TODAY}.xlsx"]


def test_two_books_whose_names_collide_both_survive():
    data = ledger.zip_of([("台账_a.xlsx", b"one"), ("台账_a.xlsx", b"two")])
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.namelist() == ["台账_a.xlsx", "台账_a_2.xlsx"]
    assert [z.read(n) for n in z.namelist()] == [b"one", b"two"]
