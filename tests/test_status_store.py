"""Failure-streak / pause semantics and concurrency of src/status_store.py."""

import json
import threading

import pytest

from src import status_store


@pytest.fixture(autouse=True)
def _tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(status_store, "_STATUS_FILE", tmp_path / "accounts_status.json")
    status_store._read_cache = None
    yield tmp_path
    status_store._read_cache = None


def _fresh():
    status_store._read_cache = None


def test_streak_counting_and_pause_threshold():
    f1 = status_store.record_login("u", "公司", ok=False, error="e1")
    assert f1 == {"streak_started": True, "just_paused": False}

    f2 = status_store.record_login("u", "公司", ok=False, error="e2")
    assert f2 == {"streak_started": False, "just_paused": False}

    f3 = status_store.record_login("u", "公司", ok=False, error="e3")
    assert f3 == {"streak_started": False, "just_paused": True}
    _fresh()
    assert status_store.is_login_paused("u") is True

    # Already paused: no duplicate just_paused flag
    f4 = status_store.record_login("u", "公司", ok=False, error="e4")
    assert f4 == {"streak_started": False, "just_paused": False}
    _fresh()
    assert status_store.get_status("u")["consecutive_login_failures"] == 4


def test_success_resets_and_unpauses():
    for _ in range(3):
        status_store.record_login("u", "公司", ok=False, error="e")
    status_store.record_login("u", "公司", ok=True)
    _fresh()
    entry = status_store.get_status("u")
    assert entry["consecutive_login_failures"] == 0
    assert entry["login_paused"] is False
    assert entry["last_login_ok_at"] is not None


def test_password_change_resets_and_unpauses():
    for _ in range(3):
        status_store.record_login("u", "公司", ok=False, error="e")
    status_store.record_password_changed("u")
    _fresh()
    assert status_store.is_login_paused("u") is False
    assert status_store.get_status("u")["consecutive_login_failures"] == 0


def test_threshold_from_settings(monkeypatch):
    monkeypatch.setattr(
        status_store, "get_settings",
        lambda: {"login": {"max_consecutive_failures": 1}},
    )
    flags = status_store.record_login("u", "公司", ok=False, error="e")
    assert flags["just_paused"] is True


def test_threshold_zero_disables_pause(monkeypatch):
    monkeypatch.setattr(
        status_store, "get_settings",
        lambda: {"login": {"max_consecutive_failures": 0}},
    )
    for _ in range(10):
        flags = status_store.record_login("u", "公司", ok=False, error="e")
        assert flags["just_paused"] is False
    _fresh()
    assert status_store.is_login_paused("u") is False


def test_record_scan_success_and_failure(tmp_path):
    status_store.record_scan("u", "公司", ok=False, error="boom", kind="scrape_failed")
    _fresh()
    e = status_store.get_status("u")
    assert e["status"] == "scrape_failed"
    assert e["error"] == "boom"
    assert e["last_success_at"] is None

    meta = {"reported_total": 10, "fetched_count": 10, "complete": True}
    status_store.record_scan("u", "公司", ok=True, fetch_meta=meta)
    _fresh()
    e = status_store.get_status("u")
    assert e["status"] == "ok"
    assert e["error"] is None
    assert e["last_success_at"] is not None
    assert e["fetched"] == 10

    meta_partial = {"reported_total": 10, "fetched_count": 4, "complete": False}
    status_store.record_scan("u", "公司", ok=True, fetch_meta=meta_partial)
    _fresh()
    assert status_store.get_status("u")["status"] == "partial"


def test_error_capped_at_500_chars():
    status_store.record_scan("u", "公司", ok=False, error="x" * 2000, kind="login_failed")
    _fresh()
    assert len(status_store.get_status("u")["error"]) == 500


def test_forget_account(tmp_path):
    status_store.record_scan("u", "公司", ok=True)
    status_store.forget_account("u")
    _fresh()
    assert status_store.get_status("u") is None


def test_store_survives_corrupt_file(tmp_path):
    (tmp_path / "accounts_status.json").write_text("{not json", encoding="utf-8")
    _fresh()
    assert status_store.get_all() == {}
    status_store.record_scan("u", "公司", ok=True)  # must not raise
    _fresh()
    assert status_store.get_status("u")["status"] == "ok"


def test_concurrent_writers_lose_nothing(tmp_path):
    """Two threads writing different accounts must both land (filelock RMW)."""
    def worker(name):
        for i in range(20):
            status_store.record_scan(name, "公司", ok=True)

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    data = json.loads((tmp_path / "accounts_status.json").read_text(encoding="utf-8"))
    assert set(data["accounts"]) == {"a", "b"}
