"""src/attachments.py robustness: no orphan .part files, a size ceiling, an
atomic manifest, and one malformed record not wiping out the rest of the sync.

The disk these files land on has run at 96% full, and a corrupt manifest reads
back as "never synced" — i.e. the record re-requests its documents from the
platform on every run, forever.
"""

import json
from pathlib import Path

import pytest
import requests

from src import attachments


class _FakeResponse:
    def __init__(self, chunks, *, ok=True, status=200, headers=None, fail_after=None):
        self._chunks = chunks
        self.ok = ok
        self.status_code = status
        self.headers = headers or {}
        self._fail_after = fail_after

    def iter_content(self, chunk_size=65536):
        for i, c in enumerate(self._chunks):
            if self._fail_after is not None and i >= self._fail_after:
                raise requests.ConnectionError("stream died")
            yield c


def _serve(monkeypatch, response):
    monkeypatch.setattr(attachments.requests, "get", lambda url, **kw: response)


def test_successful_download_leaves_no_part_file(tmp_path, monkeypatch):
    _serve(monkeypatch, _FakeResponse([b"abc", b"def"]))
    dest = tmp_path / "a" / "通知书.pdf"

    assert attachments._download_file("https://x/f.pdf", dest) is True
    assert dest.read_bytes() == b"abcdef"
    assert list(tmp_path.rglob("*.part")) == []


def test_broken_stream_leaves_no_part_file(tmp_path, monkeypatch):
    """A mid-stream disconnect used to leave a half-written .part behind with
    nothing in the codebase that ever removed it."""
    _serve(monkeypatch, _FakeResponse([b"abc", b"def"], fail_after=1))
    dest = tmp_path / "a" / "通知书.pdf"

    assert attachments._download_file("https://x/f.pdf", dest) is False
    assert not dest.exists()
    assert list(tmp_path.rglob("*.part")) == []


def test_disk_full_is_one_failed_file_not_a_crash(tmp_path, monkeypatch):
    _serve(monkeypatch, _FakeResponse([b"abc"]))
    dest = tmp_path / "a" / "通知书.pdf"

    real_open = Path.open

    def boom(self, *a, **kw):
        if self.suffix == ".part":
            raise OSError(28, "No space left on device")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", boom)
    assert attachments._download_file("https://x/f.pdf", dest) is False


def test_oversized_declared_body_is_refused_without_downloading(tmp_path, monkeypatch):
    resp = _FakeResponse([b"x"], headers={"Content-Length": str(200 * 1024 * 1024)})
    _serve(monkeypatch, resp)
    dest = tmp_path / "big.pdf"

    assert attachments._download_file("https://x/f.pdf", dest) is False
    assert not dest.exists()


def test_oversized_undeclared_body_is_aborted_midstream(tmp_path, monkeypatch):
    _serve(monkeypatch, _FakeResponse([b"x" * 1024, b"y" * 1024]))
    dest = tmp_path / "big.pdf"

    assert attachments._download_file("https://x/f.pdf", dest, max_bytes=1500) is False
    assert not dest.exists()
    assert list(tmp_path.rglob("*.part")) == []


def test_http_error_returns_false(tmp_path, monkeypatch):
    _serve(monkeypatch, _FakeResponse([], ok=False, status=403))
    assert attachments._download_file("https://x/f.pdf", tmp_path / "f.pdf") is False


# ---------------------------------------------------------------------------
# sync_attachments
# ---------------------------------------------------------------------------

@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "_ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(attachments, "_build_session", lambda token: _DummySession())
    monkeypatch.setattr(attachments, "_load_settings", lambda: {"attachments": {"enabled": True}})
    monkeypatch.setattr(attachments.time, "sleep", lambda s: None)
    return tmp_path


class _DummySession:
    def close(self):
        pass


def test_one_malformed_status_does_not_skip_the_rest(sync_env, monkeypatch):
    """int() on a non-numeric status used to raise out of the whole loop, so a
    single bad record silently dropped every later attachment for the account."""
    seen: list[str] = []

    def fake_notifications(session, base_url, reg_no):
        seen.append(reg_no)
        return [{"type": "通知书", "url": f"https://x/{reg_no}.pdf"}]

    monkeypatch.setattr(attachments, "_fetch_notifications", fake_notifications)
    monkeypatch.setattr(attachments, "_fetch_certificates", lambda *a: [])
    monkeypatch.setattr(attachments, "_download_file", lambda url, dest, **kw: dest.parent.mkdir(parents=True, exist_ok=True) or dest.write_bytes(b"x") or True)

    records = [
        {"dataRegNo": "R1", "dataRegAuditStatus": 7},
        {"dataRegNo": "R2", "dataRegAuditStatus": "不是数字"},
        {"dataRegNo": "R3", "dataRegAuditStatus": 7},
    ]
    attachments.sync_attachments("tok", "甲公司", records)

    assert seen == ["R1", "R3"]  # R2 skipped, R3 still processed


def test_manifest_is_written_atomically(sync_env, monkeypatch):
    monkeypatch.setattr(
        attachments, "_fetch_notifications",
        lambda session, base_url, reg_no: [{"type": "通知书", "url": "https://x/a.pdf"}],
    )
    monkeypatch.setattr(attachments, "_fetch_certificates", lambda *a: [])
    monkeypatch.setattr(
        attachments, "_download_file",
        lambda url, dest, **kw: dest.parent.mkdir(parents=True, exist_ok=True) or dest.write_bytes(b"x") or True,
    )

    attachments.sync_attachments("tok", "甲公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 7}])

    manifest = sync_env / "甲公司" / "R1" / attachments._MANIFEST_NAME
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == 7
    assert list(sync_env.rglob("*.tmp")) == []
