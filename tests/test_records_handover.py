"""交付包下载（/records/handover.zip）和登记记录页上的按钮。

可见性照「导出台账」：看不到的企业、不存在的企业、不带企业，回答一样。
"""

import json
import zipfile
from io import BytesIO

import pytest

from src import attachments, diskstat
from src.attachments import _safe_name
from tests.conftest import GROUP_A_USER, GROUP_B_USER, OPERATOR_A
from tests.test_records_ledger import _ms
from web.services import attachments_svc, handover


def _rec(reg_no, status, name):
    return {"dataRegNo": reg_no, "dataRegName": name, "dataRegAuditStatus": status,
            "dataRegApplyTime": _ms("2026-03-01"), "nationalCertificateNo": f"NO-{reg_no}"}


def _put(company, reg_no, files, status=7):
    d = attachments._ATTACHMENTS_DIR / _safe_name(company) / reg_no
    d.mkdir(parents=True, exist_ok=True)
    for name in files:
        (d / name).write_bytes(f"{company}/{reg_no}/{name}".encode())
    (d / ".sync.json").write_text(json.dumps({"status": status, "synced_at": "2026-11-14T00:00:00+00:00"}),
                                  encoding="utf-8")
    attachments._manifest_cache.clear()


@pytest.fixture
def world(fake_accounts, isolated_snapshots, tmp_path, monkeypatch):
    from web.routes import records as records_routes

    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", attachments._ATTACHMENTS_DIR)
    monkeypatch.setattr(records_routes, "_HANDOVER_TMP", tmp_path / "handover-tmp")
    isolated_snapshots.write("甲 公司", [_rec("R1", 7, "甲的数据"), _rec("R2", 2, "甲在补正")])
    isolated_snapshots.write("乙·公司", [_rec("W1", 1, "乙在审")])
    isolated_snapshots.write("丙公司", [_rec("RB", 7, "丙的数据")])
    _put("甲 公司", "R1", ["国家局登记证书.pdf"])
    _put("丙公司", "RB", ["国家局登记证书.pdf"])
    yield tmp_path / "handover-tmp"
    attachments._manifest_cache.clear()


@pytest.fixture
def names(monkeypatch):
    # TestClient trips over raw UTF-8 header bytes (uvicorn sends them as they
    # are; test_records_ledger checks the header itself): record the name only.
    from web.routes import records as records_routes

    seen = []
    monkeypatch.setattr(records_routes, "attachment_disposition", lambda name: seen.append(name) or "attachment")
    return seen


def _audit(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("user", [OPERATOR_A, GROUP_A_USER])  # 只读用户也能下自己分组的
def test_downloads_a_visible_companys_package(world, web_client, login_as, user, names, _isolated_audit):
    login_as(web_client, *user)
    resp = web_client.get("/records/handover.zip?account=ua1")
    assert resp.status_code == 200, resp.text[:200]
    assert resp.headers["content-type"] == handover.MEDIA_TYPE
    assert resp.headers["cache-control"] == "no-store"
    assert names == ["数据知识产权登记交付包_甲 公司_截至20260101.zip"]

    z = zipfile.ZipFile(BytesIO(resp.content))
    top = "数据知识产权登记交付包_甲 公司_截至20260101/"
    assert z.namelist() == [top + "数据知识产权登记证书清单_甲 公司_截至20260101.xlsx",
                            top + "证书/甲的数据_R1_国家局登记证书.pdf"]
    assert z.read(top + "证书/甲的数据_R1_国家局登记证书.pdf") == "甲 公司/R1/国家局登记证书.pdf".encode()
    assert "丙" not in json.dumps(z.namelist(), ensure_ascii=False)

    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] == "data.records_handover"]
    assert entry["company"] == "甲 公司"
    assert {k: entry["detail"][k] for k in ("rows", "certificates", "missing", "open")} == {
        "rows": 1, "certificates": 1, "missing": 0, "open": 1}
    assert not list(world.glob("handover-*"))  # 下载完就删


def test_another_groups_company_is_the_same_404_as_none(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *GROUP_B_USER)
    theirs = web_client.get("/records/handover.zip?account=ua1")
    missing = web_client.get("/records/handover.zip?account=no-such")
    bare = web_client.get("/records/handover.zip")
    assert theirs.status_code == missing.status_code == bare.status_code == 404
    assert theirs.text == missing.text == bare.text
    assert not [e for e in _audit(_isolated_audit) if e["action"] == "data.records_handover"]


def test_a_company_with_nothing_registered_gets_404(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *OPERATOR_A)
    resp = web_client.get("/records/handover.zip?account=ua2")
    assert resp.status_code == 404 and "还没有登记成功" in resp.text
    assert not [e for e in _audit(_isolated_audit) if e["action"] == "data.records_handover"]


def test_no_package_when_the_disk_is_short(world, web_client, login_as, monkeypatch):
    monkeypatch.setattr(diskstat, "check_free",
                        lambda *a, **k: diskstat.DiskCheck(low=True, free_mb=100.0, min_free_mb=500.0))
    login_as(web_client, *OPERATOR_A)
    resp = web_client.get("/records/handover.zip?account=ua1")
    assert resp.status_code == 503 and "磁盘空间不足" in resp.text
    assert not list(world.glob("handover-*"))


def test_old_leftovers_are_swept(world, web_client, login_as, names):
    import os
    import time

    world.mkdir(parents=True, exist_ok=True)
    stale, fresh = world / "handover-old.zip", world / "handover-new.zip"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    login_as(web_client, *OPERATOR_A)
    assert web_client.get("/records/handover.zip?account=ua1").status_code == 200
    assert not stale.exists() and fresh.exists()  # 别人正在下的不动


def test_records_page_offers_the_package_once_a_company_is_picked(world, web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    picked = web_client.get("/records?account=ua1").text
    assert 'href="/records/handover.zip?account=ua1"' in picked
    assert "交付包（尚有 1 条在审）" in picked
    nothing_registered = web_client.get("/records?account=ua2").text
    assert "/records/handover.zip" not in nothing_registered
    everyone = web_client.get("/records").text
    assert "/records/handover.zip" not in everyone


def test_a_finished_company_says_just_package(fake_accounts, isolated_snapshots, web_client, login_as):
    isolated_snapshots.write("甲 公司", [_rec("R1", 7, "甲的数据"), _rec("R9", 6, "甲不予")])
    login_as(web_client, *OPERATOR_A)
    page = web_client.get("/records?account=ua1").text
    assert "交付包</a>" in page and "尚有" not in page.split("/records/handover.zip")[1][:300]
