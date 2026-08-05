"""End-to-end cancellation flow through run_all (fake accounts, no network)."""

import threading
import time

import pytest

from src import control, main
from src.config_loader import Account


def _fake_accounts(n=3):
    return [
        Account(company=f"公司{i}", username=f"user{i}", password="x", contact="")
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _setup(isolated_control_dir, monkeypatch):
    monkeypatch.setattr(main, "load_accounts", lambda: _fake_accounts(3))
    monkeypatch.setattr(main.random, "uniform", lambda a, b: 0.05)
    yield


def test_run_all_completes_without_stop(monkeypatch):
    scanned = []

    def fake_scan(account, cancel=None, *, force_login=False, notify_inline=True):
        scanned.append(account.company)
        return {"account": account.username, "company": account.company,
                "records": [], "snapshot_path": None, "error": None}

    monkeypatch.setattr(main, "scan_account_and_notify", fake_scan)
    monkeypatch.setattr(main, "generate_report", lambda *a, **k: None)

    output = main.run_all(trigger="test")
    assert len(output["results"]) == 3
    assert not output.get("cancelled")
    assert control.read_state()["status"] == "done"


def test_stop_flag_stops_between_accounts(monkeypatch):
    def slow_scan(account, cancel=None, *, force_login=False, notify_inline=True):
        if cancel:
            cancel.wait(0.4)  # cancellable work
        else:
            time.sleep(0.4)
        return {"account": account.username, "company": account.company,
                "records": [], "snapshot_path": None, "error": None}

    monkeypatch.setattr(main, "scan_account_and_notify", slow_scan)
    monkeypatch.setattr(main, "generate_report", lambda *a, **k: None)

    result_box = {}

    def runner():
        result_box["output"] = main.run_all(trigger="test")

    t = threading.Thread(target=runner)
    t.start()
    time.sleep(0.6)  # let account 1 finish, then stop mid-run
    control.request_stop()
    t.join(timeout=10)
    assert not t.is_alive()

    output = result_box["output"]
    assert output["cancelled"] is True
    assert 1 <= len(output["results"]) < 3
    assert output["report_path"] is None  # report skipped when cancelled
    assert control.read_state()["status"] == "cancelled"
    assert not control.is_stop_requested()  # flag cleared for the next run


def test_run_all_skips_when_lock_held():
    ctx = control.try_acquire(kind="run_all", trigger="other")
    output = main.run_all(trigger="test")
    assert output.get("skipped") == "already_running"
    assert output["results"] == []
    ctx.finish("done")


def test_run_single_skips_when_lock_held():
    ctx = control.try_acquire(kind="run_all", trigger="other")
    result = main.run_single(_fake_accounts(1)[0], trigger="test")
    assert result.get("skipped") == "already_running"
    ctx.finish("done")
