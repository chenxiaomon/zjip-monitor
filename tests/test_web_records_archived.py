"""/records: a file released to the local archive shows as a grey 已归档 marker, not a link."""

import json

import pytest

from tests.conftest import ADMIN
from web.routes import records
from web.services import attachments_svc


@pytest.fixture
def archived_record(tmp_path, monkeypatch, fake_accounts):
    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", tmp_path)
    rec = tmp_path / "丙公司" / "R9"
    rec.mkdir(parents=True)
    (rec / "《登记公告通知书》.pdf").write_bytes(b"pdf")
    (rec / ".sync.json").write_text(json.dumps({
        "status": 7,
        "archived": {"国家局登记证书.pdf": {"sha256": "x", "size": 3, "archived_at": "2026-09-27T01:00:00+00:00"}},
    }, ensure_ascii=False), encoding="utf-8")

    def snapshot(company):
        if company != "丙公司":
            return None
        return {"company": company, "records": [
            {"dataRegNo": "R9", "dataRegName": "某数据", "dataRegAuditStatus": 7, "dataRegApplyTime": 1},
        ]}

    monkeypatch.setattr(records, "get_latest_snapshot", snapshot)
    return rec


def test_archived_file_is_a_grey_marker_and_live_file_a_link(archived_record, web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/partials/records-table").text

    assert "/attachments/%E4%B8%99%E5%85%AC%E5%8F%B8/R9/%E3%80%8A%E7%99%BB%E8%AE%B0" in html  # live link
    assert "国家局登记证书" in html and "（已归档）" in html
    assert "已归档到本机 · 2026-09-27" in html
    assert "%E5%9B%BD%E5%AE%B6%E5%B1%80" not in html  # no link to the released certificate


def test_archived_attachments_tolerates_bad_manifests(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", tmp_path)
    rec = tmp_path / "甲" / "R1"
    rec.mkdir(parents=True)
    assert attachments_svc.archived_attachments("甲", "R1") == []  # no manifest
    (rec / ".sync.json").write_text("[1, 2]", encoding="utf-8")
    assert attachments_svc.archived_attachments("甲", "R1") == []
    (rec / ".sync.json").write_text('{"archived": {"a.pdf": "oops", "b.pdf": {}}}', encoding="utf-8")
    assert attachments_svc.archived_attachments("甲", "R1") == [{"name": "b.pdf", "archived_on": ""}]
