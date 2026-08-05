"""Snapshot shrink guard in scan_account_and_notify."""

import pytest

from src import main
from src.config_loader import Account


@pytest.fixture
def account():
    return Account(company="测试公司", username="u1", password="p")


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(main, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(main, "get_token", lambda *a, **k: "tok")
    monkeypatch.setattr(main, "notify", lambda *a, **k: None)
    monkeypatch.setattr(main, "sync_attachments", lambda *a, **k: [])


def _records(n):
    return [{"id": i, "dataRegNo": f"R{i}", "dataRegAuditStatus": 1} for i in range(n)]


def test_shrunk_fetch_refused(monkeypatch, account):
    monkeypatch.setattr(main, "load_latest_snapshot",
                        lambda company: {"records": _records(20)})
    monkeypatch.setattr(main, "fetch_all_records", lambda tok, cancel=None: _records(4))

    saved = []
    monkeypatch.setattr(main, "save_snapshot", lambda *a, **k: saved.append(a))

    result = main.scan_account_and_notify(account)
    assert "suspicious_shrink" in result["error"]
    assert saved == []  # snapshot must NOT be persisted


def test_normal_shrink_allowed(monkeypatch, account):
    """A small decrease (real withdrawal) still goes through."""
    monkeypatch.setattr(main, "load_latest_snapshot",
                        lambda company: {"records": _records(20)})
    monkeypatch.setattr(main, "fetch_all_records", lambda tok, cancel=None: _records(18))

    result = main.scan_account_and_notify(account)
    assert result["error"] is None
    assert len(result["records"]) == 18


def test_small_account_not_guarded(monkeypatch, account):
    """Guard only kicks in from 10 previous records — tiny accounts fluctuate."""
    monkeypatch.setattr(main, "load_latest_snapshot",
                        lambda company: {"records": _records(3)})
    monkeypatch.setattr(main, "fetch_all_records", lambda tok, cancel=None: _records(1))

    result = main.scan_account_and_notify(account)
    assert result["error"] is None
