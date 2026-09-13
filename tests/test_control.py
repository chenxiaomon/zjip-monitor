"""Tests for the cross-process run control hub (src/control.py)."""

import json
import multiprocessing
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from src import control
from src.exceptions import RunCancelled


@pytest.fixture(autouse=True)
def _isolated_control_dir(isolated_control_dir):
    yield isolated_control_dir


def test_acquire_writes_running_state():
    ctx = control.try_acquire(kind="run_all", trigger="test")
    assert ctx is not None
    state = control.read_state()
    assert state["status"] == "running"
    assert state["pid"] == os.getpid()
    assert state["trigger"] == "test"
    ctx.finish("done")
    assert control.read_state()["status"] == "done"


def test_second_acquire_rejected_while_held():
    ctx = control.try_acquire(kind="run_all", trigger="test")
    assert ctx is not None
    assert control.try_acquire(kind="run_all", trigger="test2") is None
    ctx.finish("done")
    ctx2 = control.try_acquire(kind="run_all", trigger="test3")
    assert ctx2 is not None
    ctx2.finish("done")


def test_stop_flag_cancels_at_checkpoint():
    ctx = control.try_acquire(kind="run_all", trigger="test")
    ctx.cancel.checkpoint(account="a")  # no stop yet — passes
    control.request_stop()
    with pytest.raises(RunCancelled):
        ctx.cancel.checkpoint(account="a")
    assert control.read_state()["status"] == "stopping"
    ctx.finish("cancelled")
    assert control.read_state()["status"] == "cancelled"
    # finish() clears the leftover stop flag
    assert not control.is_stop_requested()


def test_local_stop_cancels_wait_quickly():
    ctx = control.try_acquire(kind="run_all", trigger="test")
    control._local_stop.set()
    start = time.monotonic()
    with pytest.raises(RunCancelled):
        ctx.cancel.wait(30.0)
    assert time.monotonic() - start < 2.0
    ctx.finish("cancelled")


def test_acquire_clears_previous_runs_stop_flag():
    control.request_stop()
    ctx = control.try_acquire(kind="run_all", trigger="test")
    assert ctx is not None
    ctx.cancel.checkpoint()  # must NOT raise — old stop flag was cleared
    ctx.finish("done")


def test_dead_pid_reported_stale():
    ctx = control.try_acquire(kind="run_all", trigger="test")
    # Simulate a crashed holder: state says running under a non-existent pid
    state = dict(ctx.state)
    state["pid"] = 99999999
    control._write_state(state)
    assert control.read_state()["status"] == "stale"
    ctx.finish("done")


def test_old_heartbeat_reported_stale():
    ctx = control.try_acquire(kind="run_all", trigger="test")
    state = dict(ctx.state)
    old = datetime.now(timezone.utc) - timedelta(seconds=control.HEARTBEAT_STALE_SECONDS + 10)
    state["heartbeat_at"] = old.isoformat()
    control._write_state(state)
    assert control.read_state()["status"] == "stale"
    ctx.finish("done")


def test_context_manager_maps_outcomes():
    with control.try_acquire(kind="run_all", trigger="test"):
        pass
    assert control.read_state()["status"] == "done"

    with control.try_acquire(kind="run_all", trigger="test"):
        raise RunCancelled("stop")  # suppressed by __exit__
    assert control.read_state()["status"] == "cancelled"

    with pytest.raises(ValueError):
        with control.try_acquire(kind="run_all", trigger="test"):
            raise ValueError("boom")
    assert control.read_state()["status"] == "error"


def test_pause_flag_roundtrip():
    assert not control.is_daemon_paused()
    control.pause_daemon()
    assert control.is_daemon_paused()
    control.resume_daemon()
    assert not control.is_daemon_paused()


def _hold_lock_in_child(lock_path: str, acquired_evt, release_evt):
    from filelock import FileLock

    lock = FileLock(lock_path)
    lock.acquire()
    acquired_evt.set()
    release_evt.wait(timeout=30)
    lock.release()


def test_lock_excludes_other_process(_isolated_control_dir):
    """A different OS process holding run.lock blocks try_acquire."""
    acquired = multiprocessing.Event()
    release = multiprocessing.Event()
    child = multiprocessing.Process(
        target=_hold_lock_in_child,
        args=(str(_isolated_control_dir / "run.lock"), acquired, release),
    )
    child.start()
    try:
        assert acquired.wait(timeout=10)
        assert control.try_acquire(kind="run_all", trigger="test") is None
        release.set()
        child.join(timeout=10)
        ctx = control.try_acquire(kind="run_all", trigger="test")
        assert ctx is not None
        ctx.finish("done")
    finally:
        release.set()
        child.join(timeout=5)
        if child.is_alive():
            child.kill()


def test_corrupt_state_file_returns_none(_isolated_control_dir):
    (_isolated_control_dir / "state.json").write_text("{truncated", encoding="utf-8")
    assert control.read_state() is None


def test_live_pid_with_dead_heartbeat_reports_wedged_holder():
    """A holder that is alive but silent is a wedged process, not a crash."""
    ctx = control.try_acquire(kind="run_all", trigger="test")
    state = dict(ctx.state)
    old = datetime.now(timezone.utc) - timedelta(seconds=control.HEARTBEAT_STALE_SECONDS + 10)
    state["heartbeat_at"] = old.isoformat()
    control._write_state(state)
    seen = control.read_state()
    assert seen["status"] == "stale"
    assert seen["holder_alive"] is True
    ctx.finish("done")


def test_dead_pid_reports_holder_not_alive():
    ctx = control.try_acquire(kind="run_all", trigger="test")
    state = dict(ctx.state)
    state["pid"] = 99999999
    control._write_state(state)
    seen = control.read_state()
    assert seen["status"] == "stale"
    assert seen["holder_alive"] is False
    ctx.finish("done")


def test_heartbeat_swallows_write_errors(monkeypatch):
    """A failing state.json write must not abort the scan (heartbeat is bookkeeping)."""
    ctx = control.try_acquire(kind="run_all", trigger="test")

    def boom(state):
        raise FileNotFoundError(2, "No such file or directory")

    # Scoped context, NOT monkeypatch.undo(): undo() would also revert the
    # isolated_control_dir fixture and make ctx.finish() write the real
    # data/control/state.json.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(control, "_write_state", boom)
        ctx.cancel.heartbeat(account="x")   # must not raise
        ctx.cancel.checkpoint(account="y")  # must not raise either
    ctx.finish("done")


def test_concurrent_checkpoints_from_threads_do_not_raise():
    import threading
    ctx = control.try_acquire(kind="run_all", trigger="test")
    errors = []

    def worker(i):
        try:
            for _ in range(30):
                ctx.cancel.checkpoint(account=f"acc{i}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert errors == []
    assert control.read_state()["status"] == "running"
    ctx.finish("done")
