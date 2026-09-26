"""The sync must not undo a release: a file the laptop archived stays off the server.

scripts/archive_release.py deletes a file and records it under "archived" in
.sync.json. When the platform touches the record again, the sync asks for its
documents — and must neither re-download the same file nor lose a genuinely
new version.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from src import attachments

CERT = "国家局登记证书"
SYNCED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


class _DummySession:
    def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "_ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(attachments, "_build_session", lambda token: _DummySession())
    monkeypatch.setattr(attachments, "_load_settings", lambda: {"attachments": {"enabled": True}})
    monkeypatch.setattr(attachments, "_fetch_notifications", lambda *a: [])
    monkeypatch.setattr(attachments.time, "sleep", lambda s: None)
    return tmp_path


def _released(root, *, sources=None, body=b"cert-v1"):
    rec = root / "甲公司" / "R1"
    rec.mkdir(parents=True)
    manifest = {
        "status": 7, "synced_at": SYNCED_AT.isoformat(), "files": [],
        "archived": {f"{CERT}.pdf": {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body),
                                     "archived_at": "2026-09-27T01:00:00+00:00"}},
    }
    if sources is not None:
        manifest["sources"] = sources
    (rec / attachments._MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return rec


def _platform(monkeypatch, body):
    fetched = []

    def fake_download(url, dest, **kw):
        fetched.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return True

    monkeypatch.setattr(attachments, "_fetch_certificates",
                        lambda *a: [{"type": CERT, "url": "https://x/abc/cert.pdf?sig=1"}])
    monkeypatch.setattr(attachments, "_download_file", fake_download)
    return fetched


def _touched():
    return [{"dataRegNo": "R1", "dataRegAuditStatus": 7,
             "updateTime": int((SYNCED_AT + timedelta(days=30)).timestamp() * 1000)}]


def _manifest(rec):
    return json.loads((rec / attachments._MANIFEST_NAME).read_text(encoding="utf-8"))


def test_known_identity_is_not_even_downloaded(env, monkeypatch):
    rec = _released(env, sources={f"{CERT}.pdf": "path:/abc/cert.pdf"})
    fetched = _platform(monkeypatch, b"cert-v1")

    assert attachments.sync_attachments("tok", "甲公司", _touched()) == []
    assert fetched == []
    assert not (rec / f"{CERT}.pdf").exists()
    assert f"{CERT}.pdf" in _manifest(rec)["archived"]


def test_same_document_without_identity_is_fetched_once_and_dropped(env, monkeypatch):
    rec = _released(env)
    fetched = _platform(monkeypatch, b"cert-v1")

    assert attachments.sync_attachments("tok", "甲公司", _touched()) == []
    assert len(fetched) == 1
    assert sorted(p.name for p in rec.iterdir()) == [attachments._MANIFEST_NAME]  # no leftovers
    m = _manifest(rec)
    assert f"{CERT}.pdf" in m["archived"]
    assert m["sources"] == {f"{CERT}.pdf": "path:/abc/cert.pdf"}  # next time: no download at all


def test_a_changed_document_comes_back_as_the_current_one(env, monkeypatch):
    rec = _released(env)
    _platform(monkeypatch, b"cert-v2")

    got = attachments.sync_attachments("tok", "甲公司", _touched())
    assert got == [rec / f"{CERT}.pdf"]
    assert (rec / f"{CERT}.pdf").read_bytes() == b"cert-v2"
    m = _manifest(rec)
    assert "archived" not in m  # the old version lives on in the local archive
    assert m["files"] == [f"{CERT}.pdf"]


def test_a_new_round_next_to_an_archived_file_keeps_the_archive_marks(env, monkeypatch):
    """Regression: the new-round branch once rebound the manifest's `archived`
    map to the moved file's Path, so the manifest could not even be written."""
    rec = _released(env, sources={f"{CERT}.pdf": "path:/abc/cert.pdf"})
    (rec / "通知书.pdf").write_bytes(b"round-1")
    _platform(monkeypatch, b"round-2")
    monkeypatch.setattr(attachments, "_fetch_notifications",
                        lambda *a: [{"type": "通知书", "url": "https://x/n2.pdf", "id": 6}])

    attachments.sync_attachments("tok", "甲公司", _touched())
    assert (rec / "通知书.pdf").read_bytes() == b"round-2"
    assert any(p.name.startswith("通知书_") for p in rec.iterdir())  # round 1 kept under a dated name
    m = _manifest(rec)
    assert f"{CERT}.pdf" in m["archived"]
    assert m["status"] == 7


def test_other_documents_keep_syncing_and_the_marks_survive(env, monkeypatch):
    rec = _released(env, sources={f"{CERT}.pdf": "path:/abc/cert.pdf"})
    _platform(monkeypatch, b"new-notice")
    monkeypatch.setattr(attachments, "_fetch_notifications",
                        lambda *a: [{"type": "通知书", "url": "https://x/n.pdf", "id": 5}])

    attachments.sync_attachments("tok", "甲公司", _touched())
    assert (rec / "通知书.pdf").read_bytes() == b"new-notice"
    m = _manifest(rec)
    assert f"{CERT}.pdf" in m["archived"]
    assert m["sources"]["通知书.pdf"] == "id:5"
