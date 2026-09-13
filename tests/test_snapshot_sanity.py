"""Snapshot shrink guard + partial-fetch merge in scan_account_and_notify."""

import json

import pytest

from src import main, status_store
from src.config_loader import Account
from src.scraper import FetchResult


@pytest.fixture
def account():
    return Account(company="测试公司", username="u1", password="p")


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(main, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(main, "_FORCE_SAVE_DIR", tmp_path / "force_save")
    monkeypatch.setattr(main, "get_token", lambda *a, **k: "tok")
    monkeypatch.setattr(main, "notify", lambda *a, **k: None)
    monkeypatch.setattr(main, "sync_attachments", lambda *a, **k: [])
    monkeypatch.setattr(main, "load_accounts", lambda: [])  # _current_password fallback
    # Keep durable status bookkeeping inside tmp_path
    monkeypatch.setattr(status_store, "_STATUS_FILE", tmp_path / "accounts_status.json")
    status_store._read_cache = None


def _records(n):
    return [{"id": i, "dataRegNo": f"R{i}", "dataRegAuditStatus": 1} for i in range(n)]


def _fetch(records, *, total=None, complete=True, error=None, total_from_server=True):
    return FetchResult(
        records=records,
        reported_total=len(records) if total is None else total,
        pages_fetched=1,
        complete=complete,
        error=error,
        total_from_server=total_from_server,
    )


def test_shrunk_fetch_refused(monkeypatch, account, tmp_path):
    monkeypatch.setattr(main, "load_latest_snapshot",
                        lambda company: {"records": _records(20)})
    # reported_total disagrees with the shrunk count → guard must refuse
    monkeypatch.setattr(main, "fetch_all_records",
                        lambda tok, cancel=None: _fetch(_records(4), total=20))

    saved = []
    monkeypatch.setattr(main, "save_snapshot", lambda *a, **k: saved.append(a))

    result = main.scan_account_and_notify(account)
    assert "suspicious_shrink" in result["error"]
    assert saved == []  # snapshot must NOT be persisted
    # The refusal is durably visible, not log-only
    entry = json.loads((tmp_path / "accounts_status.json").read_text())["accounts"]["u1"]
    assert entry["status"] == "shrink_refused"


def test_shrink_matching_server_total_accepted(monkeypatch, account):
    """The server itself asserting the smaller total unfreezes a genuine shrink."""
    monkeypatch.setattr(main, "load_latest_snapshot",
                        lambda company: {"records": _records(20)})
    monkeypatch.setattr(main, "fetch_all_records",
                        lambda tok, cancel=None: _fetch(_records(4), total=4))

    result = main.scan_account_and_notify(account)
    assert result["error"] is None
    assert len(result["records"]) == 4


def test_synthesized_total_cannot_confirm_shrink(monkeypatch, account):
    """A total the scraper synthesized (no total field in the response) must
    NOT self-confirm a shrink — only a genuine server-reported total may."""
    monkeypatch.setattr(main, "load_latest_snapshot",
                        lambda company: {"records": _records(20)})
    monkeypatch.setattr(
        main, "fetch_all_records",
        lambda tok, cancel=None: _fetch(_records(4), total=4, total_from_server=False),
    )

    result = main.scan_account_and_notify(account)
    assert "suspicious_shrink" in result["error"]


def test_force_save_flag_accepts_shrink_once(monkeypatch, account, tmp_path):
    monkeypatch.setattr(main, "load_latest_snapshot",
                        lambda company: {"records": _records(20)})
    monkeypatch.setattr(main, "fetch_all_records",
                        lambda tok, cancel=None: _fetch(_records(4), total=20))

    flag_dir = tmp_path / "force_save"
    flag_dir.mkdir(parents=True)
    (flag_dir / "u1.flag").touch()

    result = main.scan_account_and_notify(account)
    assert result["error"] is None  # flag consumed → accepted
    assert not (flag_dir / "u1.flag").exists()

    result = main.scan_account_and_notify(account)
    assert "suspicious_shrink" in result["error"]  # one-shot only


def test_normal_shrink_allowed(monkeypatch, account):
    """A small decrease (real withdrawal) still goes through."""
    monkeypatch.setattr(main, "load_latest_snapshot",
                        lambda company: {"records": _records(20)})
    monkeypatch.setattr(main, "fetch_all_records",
                        lambda tok, cancel=None: _fetch(_records(18), total=20))

    result = main.scan_account_and_notify(account)
    assert result["error"] is None
    assert len(result["records"]) == 18


def test_small_account_not_guarded(monkeypatch, account):
    """Guard only kicks in from 10 previous records — tiny accounts fluctuate."""
    monkeypatch.setattr(main, "load_latest_snapshot",
                        lambda company: {"records": _records(3)})
    monkeypatch.setattr(main, "fetch_all_records",
                        lambda tok, cancel=None: _fetch(_records(1)))

    result = main.scan_account_and_notify(account)
    assert result["error"] is None


def test_partial_fetch_merges_with_previous(monkeypatch, account):
    """Incomplete fetch: fetched head overrides, unfetched tail survives,
    and the diff reports zero 消失."""
    prev = _records(20)
    fetched = [{"id": 0, "dataRegNo": "R0", "dataRegAuditStatus": 7}]  # R0 changed 1→7
    monkeypatch.setattr(main, "load_latest_snapshot", lambda company: {"records": prev})
    monkeypatch.setattr(
        main, "fetch_all_records",
        lambda tok, cancel=None: _fetch(fetched, total=20, complete=False, error="page 2: boom"),
    )

    result = main.scan_account_and_notify(account)
    assert result["error"] is None
    assert len(result["records"]) == 20  # nothing lost
    diff = result["diff"]
    assert diff.removed == []
    assert [c.reg_no for c in diff.changed] == ["R0"]
    assert result["fetch_meta"]["complete"] is False
    assert result["fetch_meta"]["merged_with_previous"] is True

    # The saved snapshot carries the fetch meta for the UI banner
    snap_files = sorted((main._SNAPSHOTS_DIR).glob("*.json"))
    payload = json.loads(snap_files[-1].read_text(encoding="utf-8"))
    assert payload["fetch"]["complete"] is False
    assert payload["record_count"] == 20


def test_partial_first_fetch_refuses_baseline(monkeypatch, account):
    """No previous snapshot to merge with: a partial fetch must not become the
    baseline (the next complete run would flood 新增 events)."""
    monkeypatch.setattr(main, "load_latest_snapshot", lambda company: None)
    monkeypatch.setattr(
        main, "fetch_all_records",
        lambda tok, cancel=None: _fetch(_records(5), total=50, complete=False, error="page 2: boom"),
    )
    saved = []
    monkeypatch.setattr(main, "save_snapshot", lambda *a, **k: saved.append(a))

    result = main.scan_account_and_notify(account)
    assert "partial_first_fetch" in result["error"]
    assert saved == []
    assert len(result["records"]) == 5  # still visible in the result


def test_paused_account_skipped_without_login(monkeypatch, account, tmp_path):
    """Lockout protection: a paused account is skipped before any platform call."""
    status_store.record_login("u1", "测试公司", ok=False, error="bad pw")
    status_store.record_login("u1", "测试公司", ok=False, error="bad pw")
    flags = status_store.record_login("u1", "测试公司", ok=False, error="bad pw")
    assert flags["just_paused"] is True

    calls = []
    monkeypatch.setattr(main, "get_token", lambda *a, **k: calls.append(1) or "tok")

    result = main.scan_account_and_notify(account)
    assert "login_paused" in result["error"]
    assert calls == []  # never touched the platform

    # ignore_pause (manual scan) retries, and a success un-pauses
    monkeypatch.setattr(main, "load_latest_snapshot", lambda company: None)
    monkeypatch.setattr(main, "fetch_all_records", lambda tok, cancel=None: _fetch(_records(2)))
    result = main.scan_account_and_notify(account, ignore_pause=True)
    assert result["error"] is None
    status_store._read_cache = None
    assert status_store.is_login_paused("u1") is False
