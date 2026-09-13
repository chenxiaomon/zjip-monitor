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
def _setup(isolated_control_dir, isolated_settings, monkeypatch):
    # isolated_settings: run.* (threshold, workers, timeout) come from the
    # temp settings.yaml, never from a developer's real config.
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


def test_concurrent_path_abandons_stuck_account(monkeypatch, isolated_settings):
    """One account that never returns must not block the whole run (2026-09-04 incident)."""
    monkeypatch.setattr(main, "load_accounts", lambda: _fake_accounts(12))  # >10 → concurrent
    # The per-account ceiling is settings.yaml → run.account_timeout_seconds.
    isolated_settings.write({"run": {"account_timeout_seconds": 0.3}})
    monkeypatch.setattr(main, "generate_report", lambda *a, **k: None)
    monkeypatch.setattr(main, "notify_batch", lambda *a, **k: None)
    monkeypatch.setattr(main, "_prune_data", lambda: None)

    release = threading.Event()

    def scan(account, cancel=None, *, force_login=False, notify_inline=True):
        if account.username == "user5":
            release.wait(10)  # simulates a wedged Playwright call
        return {"account": account.username, "company": account.company,
                "records": [], "snapshot_path": None, "error": None}

    monkeypatch.setattr(main, "scan_account_and_notify", scan)
    try:
        start = time.monotonic()
        output = main.run_all(trigger="test")
        elapsed = time.monotonic() - start
    finally:
        release.set()

    assert elapsed < 3.0
    assert len(output["results"]) == 12
    stuck = [r for r in output["results"] if r["account"] == "user5"]
    assert stuck and "timeout" in stuck[0]["error"]
    assert sum(1 for r in output["results"] if r["error"]) == 1
    assert control.read_state()["status"] == "done"  # lock released despite the stuck thread


def test_queued_accounts_do_not_burn_their_timeout_while_waiting(
    monkeypatch, isolated_settings
):
    """The per-account ceiling must start when the account starts.

    gather() creates one coroutine per account immediately, but only
    run.max_workers of them can hold a thread. Timing the budget from
    creation made every queued account report "timeout" on a box tuned down
    to a single worker, with nothing actually wedged."""
    monkeypatch.setattr(main, "load_accounts", lambda: _fake_accounts(4))
    isolated_settings.write({
        # serial_max_accounts=0 → always take the concurrent path
        "run": {"serial_max_accounts": 0, "max_workers": 1,
                "account_timeout_seconds": 0.5},
    })
    monkeypatch.setattr(main, "generate_report", lambda *a, **k: None)
    monkeypatch.setattr(main, "notify_batch", lambda *a, **k: None)
    monkeypatch.setattr(main, "_prune_data", lambda: None)

    def scan(account, cancel=None, *, force_login=False, notify_inline=True):
        time.sleep(0.2)  # 4 × 0.2s = 0.8s总时长 > 单账号 0.5s 预算
        return {"account": account.username, "company": account.company,
                "records": [], "snapshot_path": None, "error": None}

    monkeypatch.setattr(main, "scan_account_and_notify", scan)
    output = main.run_all(trigger="test")

    assert len(output["results"]) == 4
    assert [r["error"] for r in output["results"]] == [None] * 4


def test_lock_is_released_when_reading_settings_raises(monkeypatch):
    """Anything that raises between taking the lock and the final finish used
    to leave it held by a live process: every later run then answered
    "already_running" until the daemon was restarted."""
    monkeypatch.setattr(main, "load_accounts", lambda: _fake_accounts(2))
    monkeypatch.setattr(main, "_prune_data", lambda: None)

    def boom():
        raise RuntimeError("settings.yaml exploded")

    monkeypatch.setattr(main, "run_settings", boom)

    with pytest.raises(RuntimeError, match="settings.yaml exploded"):
        main.run_all(trigger="test")

    assert control.read_state()["status"] == "error"
    # The proof the lock is free: another run can take it.
    ctx = control.try_acquire(kind="probe", trigger="test")
    assert ctx is not None
    ctx.finish("done")


# ---------------------------------------------------------------------------
# Pruning runs on EVERY way out of a run, while the global lock is still held
# ---------------------------------------------------------------------------

def _ok(account):
    return {"account": account.username, "company": account.company,
            "records": [], "snapshot_path": None, "error": None}


@pytest.fixture
def prune_calls(monkeypatch):
    """Counts _prune_data() calls and records whether the run lock was held
    at each one (another try_acquire must fail while we prune — otherwise a
    concurrent run could be writing snapshots as we delete them)."""
    calls: list[bool] = []

    def probe():
        probe_ctx = control.try_acquire(kind="probe", trigger="test")
        if probe_ctx is not None:  # lock was free → bug; tidy up so the test can report it
            probe_ctx.finish("done")
        calls.append(probe_ctx is None)

    monkeypatch.setattr(main, "_prune_data", probe)
    monkeypatch.setattr(main, "generate_report", lambda *a, **k: None)
    monkeypatch.setattr(main, "notify_batch", lambda *a, **k: None)
    return calls


def test_run_all_normal_path_prunes_exactly_once_under_lock(prune_calls, monkeypatch):
    monkeypatch.setattr(main, "scan_account_and_notify", lambda account, cancel=None, **kw: _ok(account))

    main.run_all(trigger="test")

    assert prune_calls == [True]
    assert control.read_state()["status"] == "done"


def test_run_all_cancelled_path_prunes(prune_calls, monkeypatch):
    from src.exceptions import RunCancelled

    def scan(account, cancel=None, **kw):
        raise RunCancelled("stop requested")

    monkeypatch.setattr(main, "scan_account_and_notify", scan)

    output = main.run_all(trigger="test")

    assert output["cancelled"] is True
    assert output["report_path"] is None  # still exits fast: no report
    assert prune_calls == [True]
    assert control.read_state()["status"] == "cancelled"


def test_run_all_error_path_prunes_before_releasing_the_lock(prune_calls, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "_run_serial", explode)

    with pytest.raises(RuntimeError, match="boom"):
        main.run_all(trigger="test")

    assert prune_calls == [True]
    state = control.read_state()
    assert state["status"] == "error"
    assert "RuntimeError: boom" in state["error"]
    ctx = control.try_acquire(kind="probe", trigger="test")
    assert ctx is not None  # lock released afterwards
    ctx.finish("done")


def test_run_all_config_error_path_prunes(prune_calls, monkeypatch):
    from src.config_loader import ConfigError

    def bad_accounts():
        raise ConfigError("accounts file unreadable")

    monkeypatch.setattr(main, "load_accounts", bad_accounts)

    output = main.run_all(trigger="test")

    assert output == {"results": [], "report_path": None}
    assert prune_calls == [True]
    assert control.read_state()["status"] == "error"


def test_run_single_done_path_prunes(prune_calls, monkeypatch):
    monkeypatch.setattr(main, "scan_account_and_notify", lambda account, cancel=None, **kw: _ok(account))

    result = main.run_single(_fake_accounts(1)[0], trigger="test")

    assert result["error"] is None
    assert prune_calls == [True]
    assert control.read_state()["status"] == "done"


def test_run_single_cancelled_path_prunes(prune_calls, monkeypatch):
    from src.exceptions import RunCancelled

    def scan(account, cancel=None, **kw):
        raise RunCancelled("stop requested")

    monkeypatch.setattr(main, "scan_account_and_notify", scan)

    result = main.run_single(_fake_accounts(1)[0], trigger="test")

    assert result["error"] == "cancelled"
    assert prune_calls == [True]
    assert control.read_state()["status"] == "cancelled"


def test_run_single_error_path_prunes_and_still_raises(prune_calls, monkeypatch):
    def scan(account, cancel=None, **kw):
        raise ValueError("bad thing")

    monkeypatch.setattr(main, "scan_account_and_notify", scan)

    with pytest.raises(ValueError, match="bad thing"):
        main.run_single(_fake_accounts(1)[0], trigger="test")

    assert prune_calls == [True]
    state = control.read_state()
    assert state["status"] == "error"
    assert "ValueError: bad thing" in state["error"]


def test_a_prune_failure_never_blocks_the_lock_release(monkeypatch):
    def broken_prune():
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(main, "_prune_data", broken_prune)
    monkeypatch.setattr(main, "generate_report", lambda *a, **k: None)
    monkeypatch.setattr(main, "notify_batch", lambda *a, **k: None)
    monkeypatch.setattr(main, "scan_account_and_notify", lambda account, cancel=None, **kw: _ok(account))

    output = main.run_all(trigger="test")

    assert len(output["results"]) == 3
    assert control.read_state()["status"] == "done"
    ctx = control.try_acquire(kind="probe", trigger="test")
    assert ctx is not None
    ctx.finish("done")
