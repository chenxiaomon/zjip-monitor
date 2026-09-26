"""The scan stores what the console needs from a notice the moment it lands.

.sync.json → facts[filename] = pdftext.as_record(): deadline, dates, reasons.
Certificates get no entry; an older round keeps its facts under its dated
name; a document that comes back changed after a release is parsed too.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src import attachments, pdftext
from tests.test_archive_pdftext import CORRECTION, CORRECTION_FORM_ONLY

NOTICE = "《浙江省数据知识产权登记形式审查补正通知书》"
SYNCED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)
TEXTS = {b"round-1": CORRECTION_FORM_ONLY, b"round-2": CORRECTION}


class _DummySession:
    def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "_ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(attachments, "_build_session", lambda token: _DummySession())
    monkeypatch.setattr(attachments, "_load_settings", lambda: {"attachments": {"enabled": True}})
    monkeypatch.setattr(attachments, "_fetch_certificates", lambda *a: [])
    monkeypatch.setattr(attachments.time, "sleep", lambda s: None)
    monkeypatch.setattr(pdftext, "extract_text", lambda path: TEXTS.get(path.read_bytes(), ""))
    return tmp_path


def _serve(monkeypatch, items, bodies):
    def fake_download(url, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(bodies[url])
        return True

    monkeypatch.setattr(attachments, "_fetch_notifications", lambda *a: items)
    monkeypatch.setattr(attachments, "_download_file", fake_download)


def _manifest(root):
    return json.loads((root / "甲公司" / "R1" / attachments._MANIFEST_NAME).read_text(encoding="utf-8"))


def _touched(days=1):
    """A record the platform updated `days` from NOW — after any synced_at a
    previous sync in the same test wrote."""
    at = datetime.now(timezone.utc) + timedelta(days=days)
    return [{"dataRegNo": "R1", "dataRegAuditStatus": 2, "updateTime": int(at.timestamp() * 1000)}]


def test_first_download_stores_the_notice_facts(env, monkeypatch):
    _serve(monkeypatch, [{"type": NOTICE, "url": "https://x/n1.pdf", "id": 1}], {"https://x/n1.pdf": b"round-1"})

    attachments.sync_attachments("tok", "甲公司", _touched())
    facts = _manifest(env)["facts"][f"{NOTICE}.pdf"]
    assert facts["kind"] == "补正"
    assert facts["deadline"] == "2026-10-13"
    assert facts["issued_on"] == "2026-09-22"
    assert facts["reasons"].startswith("登记申请表填写不符合要求")
    assert set(facts) == {"kind", "submitted_on", "issued_on", "deadline", "valid_from", "valid_to", "reasons"}


def test_a_new_round_moves_the_old_facts_to_the_dated_name(env, monkeypatch):
    _serve(monkeypatch, [{"type": NOTICE, "url": "https://x/n1.pdf", "id": 1}], {"https://x/n1.pdf": b"round-1"})
    attachments.sync_attachments("tok", "甲公司", _touched(1))
    _serve(monkeypatch, [{"type": NOTICE, "url": "https://x/n2.pdf", "id": 2}], {"https://x/n2.pdf": b"round-2"})
    attachments.sync_attachments("tok", "甲公司", _touched(20))

    facts = _manifest(env)["facts"]
    current, (older,) = facts[f"{NOTICE}.pdf"], [k for k in facts if k != f"{NOTICE}.pdf"]
    assert older.startswith(f"{NOTICE}_") and facts[older]["deadline"] == "2026-10-13"
    assert current["deadline"] == "2026-10-14"


def test_certificates_and_textless_files_get_no_facts(env, monkeypatch):
    monkeypatch.setattr(attachments, "_fetch_certificates",
                        lambda *a: [{"type": "国家局登记证书", "url": "https://x/c.pdf"}])
    _serve(monkeypatch, [{"type": NOTICE, "url": "https://x/blank.pdf", "id": 3}],
           {"https://x/blank.pdf": b"no-text", "https://x/c.pdf": b"round-1"})

    attachments.sync_attachments("tok", "甲公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 7}])
    manifest = _manifest(env)
    assert "facts" not in manifest  # nothing parseable → key absent, not {}
    assert sorted(manifest["files"]) == [f"{NOTICE}.pdf", "国家局登记证书.pdf"]


def test_an_old_manifest_without_facts_is_completed_on_the_next_touch(env, monkeypatch):
    rec = env / "甲公司" / "R1"
    rec.mkdir(parents=True)
    (rec / f"{NOTICE}.pdf").write_bytes(b"round-1")
    (rec / attachments._MANIFEST_NAME).write_text(json.dumps({
        "status": 2, "synced_at": SYNCED_AT.isoformat(), "files": [f"{NOTICE}.pdf"],
        "sources": {f"{NOTICE}.pdf": "id:1"},
    }), encoding="utf-8")
    _serve(monkeypatch, [{"type": NOTICE, "url": "https://x/n1.pdf", "id": 1}], {"https://x/n1.pdf": b"round-1"})

    attachments.sync_attachments("tok", "甲公司", _touched(5))
    assert _manifest(env)["facts"][f"{NOTICE}.pdf"]["deadline"] == "2026-10-13"


def test_a_document_changed_after_release_is_parsed(env, monkeypatch):
    rec = env / "甲公司" / "R1"
    rec.mkdir(parents=True)
    (rec / attachments._MANIFEST_NAME).write_text(json.dumps({
        "status": 2, "synced_at": SYNCED_AT.isoformat(), "files": [],
        "archived": {f"{NOTICE}.pdf": {"sha256": "0" * 64, "size": 1, "archived_at": "2026-09-27T00:00:00+00:00"}},
    }), encoding="utf-8")
    _serve(monkeypatch, [{"type": NOTICE, "url": "https://x/n2.pdf", "id": 2}], {"https://x/n2.pdf": b"round-2"})

    attachments.sync_attachments("tok", "甲公司", _touched(30))
    assert _manifest(env)["facts"][f"{NOTICE}.pdf"]["deadline"] == "2026-10-14"


def test_load_manifest_cached_follows_rewrites(tmp_path):
    rec = tmp_path / "R"
    rec.mkdir()
    assert attachments.load_manifest_cached(rec) == {}
    (rec / attachments._MANIFEST_NAME).write_text('{"status": 2}', encoding="utf-8")
    assert attachments.load_manifest_cached(rec) == {"status": 2}
    (rec / attachments._MANIFEST_NAME).write_text('{"status": 7, "facts": {}}', encoding="utf-8")
    assert attachments.load_manifest_cached(rec) == {"status": 7, "facts": {}}
    (rec / attachments._MANIFEST_NAME).write_text("not json at all", encoding="utf-8")
    assert attachments.load_manifest_cached(rec) == {}
