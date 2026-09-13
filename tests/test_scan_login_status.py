"""Login-failure bookkeeping + notification wiring in scan_account_and_notify."""

import pytest

from src import main, status_store
from src.config_loader import Account
from src.exceptions import CredentialsRejectedError, LoginError


@pytest.fixture
def account():
    return Account(company="测试公司", username="u1", password="p")


@pytest.fixture(autouse=True)
def _stub(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(main, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(main, "_FORCE_SAVE_DIR", tmp_path / "force_save")
    monkeypatch.setattr(main, "notify", lambda *a, **k: None)
    monkeypatch.setattr(main, "sync_attachments", lambda *a, **k: [])
    monkeypatch.setattr(main, "load_accounts", lambda: [])  # _current_password fallback
    monkeypatch.setattr(status_store, "_STATUS_FILE", tmp_path / "accounts_status.json")
    status_store._read_cache = None


@pytest.fixture
def notified(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main, "notify_login_failure",
        lambda company, username, error, paused=False: calls.append(
            {"company": company, "paused": paused}
        ),
    )
    return calls


def _raise_rejected(*a, **k):
    raise CredentialsRejectedError("[u1] Login rejected by platform (code=1001): 账号或密码错误")


def test_rejected_login_counts_and_notifies_once_per_streak(monkeypatch, account, notified):
    monkeypatch.setattr(main, "get_token", _raise_rejected)

    r1 = main.scan_account_and_notify(account)
    assert "login_failed" in r1["error"]
    assert len(notified) == 1 and notified[0]["paused"] is False  # streak start

    main.scan_account_and_notify(account, ignore_pause=True)
    assert len(notified) == 1  # mid-streak: silent

    main.scan_account_and_notify(account, ignore_pause=True)
    assert len(notified) == 2 and notified[1]["paused"] is True  # just paused

    status_store._read_cache = None
    assert status_store.is_login_paused("u1") is True

    # Scheduled run now skips without calling get_token
    calls = []
    monkeypatch.setattr(main, "get_token", lambda *a, **k: calls.append(1))
    r = main.scan_account_and_notify(account)
    assert "login_paused" in r["error"]
    assert calls == []


def test_transport_login_error_does_not_count(monkeypatch, account, notified):
    """A non-rejection LoginError (platform outage) must not feed the streak."""
    def _raise_unavailable(*a, **k):
        raise LoginError("[u1] HTTP login unavailable: HTTP 502")

    monkeypatch.setattr(main, "get_token", _raise_unavailable)
    for _ in range(5):
        r = main.scan_account_and_notify(account)
        assert "login_failed" in r["error"]

    status_store._read_cache = None
    entry = status_store.get_status("u1")
    assert entry["status"] == "login_failed"          # durable visibility
    assert entry["consecutive_login_failures"] == 0   # but no streak
    assert status_store.is_login_paused("u1") is False
    assert notified == []


def test_notification_failure_is_non_fatal(monkeypatch, account):
    monkeypatch.setattr(main, "get_token", _raise_rejected)
    monkeypatch.setattr(
        main, "notify_login_failure",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("webhook down")),
    )
    r = main.scan_account_and_notify(account)  # must not raise
    assert "login_failed" in r["error"]


def test_success_records_login_ok(monkeypatch, account):
    from src.scraper import FetchResult

    monkeypatch.setattr(main, "get_token", lambda *a, **k: "tok")
    monkeypatch.setattr(main, "load_latest_snapshot", lambda company: None)
    monkeypatch.setattr(
        main, "fetch_all_records",
        lambda tok, cancel=None: FetchResult(records=[], reported_total=0, pages_fetched=1),
    )
    r = main.scan_account_and_notify(account)
    assert r["error"] is None
    status_store._read_cache = None
    entry = status_store.get_status("u1")
    assert entry["last_login_ok_at"] is not None
    assert entry["status"] == "ok"


def test_fresh_login_uses_current_stored_password(monkeypatch, account):
    """A password edited in the console mid-run must win over the stale
    in-memory Account copy on every login attempt of the scan."""
    from src.scraper import FetchResult

    monkeypatch.setattr(
        main, "load_accounts",
        lambda: [Account(company="测试公司", username="u1", password="NEW-PW")],
    )
    used = []

    def fake_get_token(username, password, *, force_refresh=False):
        used.append(password)
        return "tok"

    monkeypatch.setattr(main, "get_token", fake_get_token)
    monkeypatch.setattr(main, "load_latest_snapshot", lambda company: None)
    monkeypatch.setattr(
        main, "fetch_all_records",
        lambda tok, cancel=None: FetchResult(records=[], reported_total=0, pages_fetched=1),
    )
    main.scan_account_and_notify(account)  # account object still carries "p"
    assert used == ["NEW-PW"]
