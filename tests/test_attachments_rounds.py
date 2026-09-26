"""A second 补正 round must reach the console.

Seen 2026-09: DIP2026000010599 went 待补正 → 待审核 → 待补正 three times, but the
manifest still said status 2 from the first round, so the sync never asked the
platform again — and a newer notice would have hit the same file name anyway.
The console kept serving the 08-31 notice (deadline 09-14) while the live one
said 10-12. getNotification lists only the current notice, with an id.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from src import attachments

NOTICE = "《浙江省数据知识产权登记形式审查补正通知书》"
FNAME = NOTICE + ".pdf"
SYNCED_AT = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
# 2026-09-01 09:00 in Beijing — the day the first-round notice was downloaded
FIRST_DOWNLOAD = datetime(2026, 9, 1, 9, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()


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
    return tmp_path


def _platform(monkeypatch, notices, bodies):
    """Serve *notices* from getNotification and *bodies* (url → bytes) as
    downloads; returns the lists of queried records and downloaded urls."""
    queried: list[str] = []
    fetched: list[str] = []

    def fake_notifications(session, base_url, reg_no):
        queried.append(reg_no)
        return notices

    def fake_download(url, dest, **kw):
        fetched.append(url)
        if url not in bodies:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(bodies[url])
        return True

    monkeypatch.setattr(attachments, "_fetch_notifications", fake_notifications)
    monkeypatch.setattr(attachments, "_download_file", fake_download)
    return queried, fetched


def _first_round_on_disk(root, *, sources=None):
    """What production looked like: the first notice plus a status-2 manifest."""
    rec = root / "易航海" / "R1"
    rec.mkdir(parents=True)
    (rec / FNAME).write_bytes(b"round-1")
    os.utime(rec / FNAME, (FIRST_DOWNLOAD, FIRST_DOWNLOAD))
    manifest = {"status": 2, "synced_at": SYNCED_AT.isoformat(), "files": [FNAME]}
    if sources is not None:
        manifest["sources"] = sources
    (rec / attachments._MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return rec


def _record(update: datetime | None, status=2):
    r = {"dataRegNo": "R1", "dataRegAuditStatus": status}
    if update is not None:
        r["updateTime"] = int(update.timestamp() * 1000)
    return r


def _manifest(rec):
    return json.loads((rec / attachments._MANIFEST_NAME).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# When to ask the platform again
# ---------------------------------------------------------------------------

def test_same_status_but_platform_touched_the_record_is_queried_again(env, monkeypatch):
    _first_round_on_disk(env)
    queried, _ = _platform(monkeypatch, [], {})

    attachments.sync_attachments("tok", "易航海", [_record(SYNCED_AT + timedelta(days=20))])

    assert queried == ["R1"]


def test_untouched_record_at_the_same_status_is_not_queried(env, monkeypatch):
    _first_round_on_disk(env)
    queried, _ = _platform(monkeypatch, [], {})

    attachments.sync_attachments("tok", "易航海", [_record(SYNCED_AT - timedelta(hours=2))])

    assert queried == []


def test_record_without_update_time_keeps_status_only_behaviour(env, monkeypatch):
    _first_round_on_disk(env)
    queried, _ = _platform(monkeypatch, [], {})

    attachments.sync_attachments("tok", "易航海", [_record(None)])

    assert queried == []


# ---------------------------------------------------------------------------
# What to do with a document of a type that is already on disk
# ---------------------------------------------------------------------------

def test_newer_round_takes_the_plain_name_and_the_old_one_is_archived(env, monkeypatch):
    rec = _first_round_on_disk(env)
    _platform(monkeypatch, [{"type": NOTICE, "url": "https://x/n3.pdf", "id": 152216}],
              {"https://x/n3.pdf": b"round-3"})

    got = attachments.sync_attachments("tok", "易航海", [_record(SYNCED_AT + timedelta(days=20))])

    archived = f"{NOTICE}_2026-09-01.pdf"
    assert (rec / FNAME).read_bytes() == b"round-3"
    assert (rec / archived).read_bytes() == b"round-1"
    assert got == [rec / FNAME]
    m = _manifest(rec)
    assert m["sources"][FNAME] == "id:152216"
    assert set(m["files"]) == {FNAME, archived}
    assert list(rec.rglob("*.part")) == []


def test_same_document_on_an_old_manifest_is_not_duplicated(env, monkeypatch):
    """Manifests written before identities were recorded: the platform serving
    the very same bytes must not produce an archived copy."""
    rec = _first_round_on_disk(env)
    _platform(monkeypatch, [{"type": NOTICE, "url": "https://x/n1.pdf", "id": 1}],
              {"https://x/n1.pdf": b"round-1"})

    got = attachments.sync_attachments("tok", "易航海", [_record(SYNCED_AT + timedelta(days=1))])

    assert got == []
    assert sorted(p.name for p in rec.iterdir()) == sorted([FNAME, attachments._MANIFEST_NAME])
    assert _manifest(rec)["sources"] == {FNAME: "id:1"}


def test_known_document_is_not_downloaded_again(env, monkeypatch):
    rec = _first_round_on_disk(env, sources={FNAME: "id:1"})
    _, fetched = _platform(monkeypatch, [{"type": NOTICE, "url": "https://x/n1.pdf", "id": 1}], {})

    attachments.sync_attachments("tok", "易航海", [_record(SYNCED_AT + timedelta(days=1))])

    assert fetched == []
    assert (rec / FNAME).read_bytes() == b"round-1"


def test_failed_download_of_a_newer_round_keeps_the_old_file_and_retries(env, monkeypatch):
    rec = _first_round_on_disk(env)
    before = _manifest(rec)
    _platform(monkeypatch, [{"type": NOTICE, "url": "https://x/n3.pdf", "id": 152216}], {})

    attachments.sync_attachments("tok", "易航海", [_record(SYNCED_AT + timedelta(days=20))])

    assert (rec / FNAME).read_bytes() == b"round-1"
    assert _manifest(rec) == before  # unwritten → the next run asks again
    assert list(rec.rglob("*.part")) == []


def test_archive_names_do_not_collide(env, monkeypatch):
    rec = _first_round_on_disk(env)
    (rec / f"{NOTICE}_2026-09-01.pdf").write_bytes(b"round-0")
    _platform(monkeypatch, [{"type": NOTICE, "url": "https://x/n3.pdf", "id": 3}],
              {"https://x/n3.pdf": b"round-3"})

    attachments.sync_attachments("tok", "易航海", [_record(SYNCED_AT + timedelta(days=20))])

    assert (rec / f"{NOTICE}_2026-09-01.pdf").read_bytes() == b"round-0"
    assert (rec / f"{NOTICE}_2026-09-01_2.pdf").read_bytes() == b"round-1"
    assert (rec / FNAME).read_bytes() == b"round-3"


def test_certificates_without_an_id_are_identified_by_url_path():
    a = attachments._identity({"url": "https://h/abc/证书.pdf?sig=1"})
    b = attachments._identity({"url": "https://h/abc/证书.pdf?sig=2"})
    assert a == b == "path:/abc/证书.pdf"
    assert attachments._identity({"url": "https://h/abc/证书.pdf", "id": 7}) == "id:7"
