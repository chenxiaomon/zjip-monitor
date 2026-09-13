"""Disk guard inside attachments.sync_attachments.

Attachments are the one thing this tool writes that grows without bound
(407 MB and counting on a 9.6 GB root partition at 91%). When free space
drops under disk.min_free_mb the sync must stop downloading — before it
even opens a platform session at the start of an account, and between two
files when the floor is crossed mid-run — and must leave the interrupted
record un-manifested so the next run picks it up again.
"""

import json

import pytest
import requests
from loguru import logger

from src import attachments, diskstat


def _low(free_mb: float = 120.0) -> diskstat.DiskCheck:
    return diskstat.DiskCheck(low=True, free_mb=free_mb, min_free_mb=500.0)


def _ok(free_mb: float = 900.0) -> diskstat.DiskCheck:
    return diskstat.DiskCheck(low=False, free_mb=free_mb, min_free_mb=500.0)


class _DummySession:
    def close(self):
        pass


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "_ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(attachments, "_load_settings", lambda: {"attachments": {"enabled": True}})
    monkeypatch.setattr(attachments.time, "sleep", lambda s: None)
    return tmp_path


@pytest.fixture
def warnings():
    seen: list[str] = []
    sink = logger.add(
        lambda m: seen.append(m.record["message"]),
        level="WARNING",
        filter=lambda r: r["name"] == "src.attachments",
    )
    yield seen
    logger.remove(sink)


def _no_network(monkeypatch):
    def boom(self, request, **kw):
        raise AssertionError(f"HTTP escaped the disk guard: {request.method} {request.url}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", boom)
    monkeypatch.setattr(attachments.requests, "get", lambda *a, **k: pytest.fail("requests.get called"))


def test_low_disk_at_entry_skips_the_account_without_any_http(sync_env, monkeypatch, warnings):
    monkeypatch.setattr(attachments.diskstat, "check_free", lambda *a, **k: _low())
    monkeypatch.setattr(attachments, "_build_session", lambda token: pytest.fail("session built"))
    _no_network(monkeypatch)

    out = attachments.sync_attachments(
        "tok", "甲公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 7}]
    )

    assert out == []
    assert list(sync_env.rglob("*")) == []  # nothing written, no manifest
    assert len(warnings) == 1
    assert "[甲公司]" in warnings[0]
    assert "120MB" in warnings[0] and "disk.min_free_mb=500MB" in warnings[0]
    assert "跳过附件下载" in warnings[0]


def test_disabled_attachments_do_not_even_probe_the_disk(sync_env, monkeypatch):
    monkeypatch.setattr(attachments, "_load_settings", lambda: {"attachments": {"enabled": False}})
    monkeypatch.setattr(attachments.diskstat, "check_free", lambda *a, **k: pytest.fail("probed"))

    assert attachments.sync_attachments("tok", "甲公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 7}]) == []


def _sequence(monkeypatch, checks):
    """check_free returns the given results in order, then repeats the last."""
    it = iter(checks)
    state = {"last": None}

    def fake(*a, **k):
        try:
            state["last"] = next(it)
        except StopIteration:
            pass
        return state["last"]

    monkeypatch.setattr(attachments.diskstat, "check_free", fake)


def _wire_platform(monkeypatch, fetched: list[str], downloaded: list[str]):
    monkeypatch.setattr(attachments, "_build_session", lambda token: _DummySession())

    def notifications(session, base_url, reg_no):
        fetched.append(reg_no)
        return [{"type": "通知书", "url": f"https://oss/{reg_no}.pdf"}]

    def download(url, dest, **kw):
        downloaded.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return True

    monkeypatch.setattr(attachments, "_fetch_notifications", notifications)
    monkeypatch.setattr(attachments, "_fetch_certificates", lambda *a: [])
    monkeypatch.setattr(attachments, "_download_file", download)


def test_disk_going_low_mid_sync_stops_downloads_and_leaves_record_for_retry(
    sync_env, monkeypatch, warnings
):
    # entry probe ok → R1's file ok → R2's file low
    _sequence(monkeypatch, [_ok(), _ok(), _low(80)])
    fetched: list[str] = []
    downloaded: list[str] = []
    _wire_platform(monkeypatch, fetched, downloaded)

    records = [
        {"dataRegNo": "R1", "dataRegAuditStatus": 7},
        {"dataRegNo": "R2", "dataRegAuditStatus": 7},
        {"dataRegNo": "R3", "dataRegAuditStatus": 7},
    ]
    out = attachments.sync_attachments("tok", "甲公司", records)

    assert downloaded == ["https://oss/R1.pdf"]
    assert [p.name for p in out] == ["通知书.pdf"]
    r1_manifest = sync_env / "甲公司" / "R1" / attachments._MANIFEST_NAME
    assert json.loads(r1_manifest.read_text(encoding="utf-8"))["status"] == 7
    # R2 was interrupted: no file, no manifest → re-synced next run.
    assert not (sync_env / "甲公司" / "R2").exists()
    # R3 was never touched — not even its notification list was requested.
    assert fetched == ["R1", "R2"]
    assert len(warnings) == 1
    assert "80MB" in warnings[0]


def test_low_disk_between_two_files_of_one_record_keeps_the_first_and_no_manifest(
    sync_env, monkeypatch, warnings
):
    _sequence(monkeypatch, [_ok(), _ok(), _low()])
    monkeypatch.setattr(attachments, "_build_session", lambda token: _DummySession())
    monkeypatch.setattr(
        attachments, "_fetch_notifications",
        lambda s, b, reg_no: [
            {"type": "补正通知书", "url": "https://oss/a.pdf"},
            {"type": "受理通知书", "url": "https://oss/b.pdf"},
        ],
    )
    monkeypatch.setattr(attachments, "_fetch_certificates", lambda *a: [])
    downloaded: list[str] = []

    def download(url, dest, **kw):
        downloaded.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return True

    monkeypatch.setattr(attachments, "_download_file", download)

    out = attachments.sync_attachments(
        "tok", "甲公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 7}]
    )

    assert downloaded == ["https://oss/a.pdf"]
    assert [p.name for p in out] == ["补正通知书.pdf"]
    rec_dir = sync_env / "甲公司" / "R1"
    assert (rec_dir / "补正通知书.pdf").exists()          # what landed stays
    assert not (rec_dir / attachments._MANIFEST_NAME).exists()  # but the record is not "synced"
    assert len(warnings) == 1


def test_healthy_disk_changes_nothing(sync_env, monkeypatch, warnings):
    _sequence(monkeypatch, [_ok()])
    fetched: list[str] = []
    downloaded: list[str] = []
    _wire_platform(monkeypatch, fetched, downloaded)

    records = [
        {"dataRegNo": "R1", "dataRegAuditStatus": 7},
        {"dataRegNo": "R2", "dataRegAuditStatus": 7},
    ]
    out = attachments.sync_attachments("tok", "甲公司", records)

    assert len(out) == 2
    assert downloaded == ["https://oss/R1.pdf", "https://oss/R2.pdf"]
    for reg_no in ("R1", "R2"):
        assert (sync_env / "甲公司" / reg_no / attachments._MANIFEST_NAME).exists()
    assert warnings == []
