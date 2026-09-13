"""Cross-process run control: global run lock, state file, stop/pause flags.

data/control/ is the single coordination point shared by the web console,
run_daemon.py and run_once.py:

    run.lock    flock-based mutex (released by the kernel if the holder dies)
    state.json  current/last run: status, pid, heartbeat, progress
    stop.flag   present = the current run should stop at its next checkpoint
    pause.flag  present = the daemon skips scheduled runs
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from loguru import logger

from .exceptions import RunCancelled
from .fsutil import atomic_write_text

_ROOT = Path(__file__).parent.parent
_CONTROL_DIR = _ROOT / "data" / "control"
_LOCK_FILE = _CONTROL_DIR / "run.lock"
_STATE_FILE = _CONTROL_DIR / "state.json"
_STOP_FLAG = _CONTROL_DIR / "stop.flag"
_PAUSE_FLAG = _CONTROL_DIR / "pause.flag"

# A run whose heartbeat is older than this is considered dead even if its
# pid still exists (3× the longest checkpoint-free stretch, the browser login).
HEARTBEAT_STALE_SECONDS = 180

# Minimum interval between state.json heartbeat writes from checkpoints.
_HEARTBEAT_WRITE_INTERVAL = 2.0

# Process-internal stop channel, set by SIGTERM handlers / lifespan shutdown
# so in-flight runs in this process stop without touching stop.flag.
_local_stop = threading.Event()


def request_local_stop() -> None:
    _local_stop.set()


def request_stop() -> None:
    """Ask the currently running scan (any process) to stop."""
    _CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    _STOP_FLAG.touch()


def clear_stop() -> None:
    _STOP_FLAG.unlink(missing_ok=True)


def is_stop_requested() -> bool:
    return _local_stop.is_set() or _STOP_FLAG.exists()


def pause_daemon() -> None:
    _CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    _PAUSE_FLAG.touch()


def resume_daemon() -> None:
    _PAUSE_FLAG.unlink(missing_ok=True)


def is_daemon_paused() -> bool:
    return _PAUSE_FLAG.exists()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def read_state() -> dict[str, Any] | None:
    """Read state.json, downgrading a dead holder's 'running' to 'stale'."""
    try:
        state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning(f"Could not read control state: {exc}")
        return None

    if state.get("status") in ("running", "stopping"):
        alive = _pid_alive(state.get("pid"))
        stale = not alive
        if not stale:
            try:
                hb = datetime.fromisoformat(state["heartbeat_at"])
                stale = (datetime.now(timezone.utc) - hb).total_seconds() > HEARTBEAT_STALE_SECONDS
            except Exception:
                stale = True
        if stale:
            state["status"] = "stale"
            # A live holder with a dead heartbeat is a wedged process, not a
            # crash: it still owns run.lock, so a new run cannot start until
            # that process is restarted. The UI and the daemon act on this.
            state["holder_alive"] = alive
    return state


# Serializes state.json writes from the concurrent scan threads so the JSON
# snapshot is taken and written by one thread at a time.
_STATE_WRITE_LOCK = threading.Lock()


def _write_state(state: dict[str, Any]) -> None:
    with _STATE_WRITE_LOCK:
        atomic_write_text(_STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))


class CancelToken:
    """Cooperative cancellation + heartbeat, threaded through the scan pipeline.

    Call sites treat a None token as "no cancellation" so library code stays
    usable standalone (e.g. scripts/test_login.py).
    """

    def __init__(self, state: dict[str, Any]):
        self._state = state
        self._last_write = 0.0

    def should_stop(self) -> bool:
        return is_stop_requested()

    def heartbeat(self, **progress: Any) -> None:
        """Update heartbeat/progress without raising."""
        now = time.monotonic()
        if progress:
            self._state["progress"] = {**self._state.get("progress", {}), **progress}
        elif now - self._last_write < _HEARTBEAT_WRITE_INTERVAL:
            return
        self._state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        self._last_write = now
        try:
            _write_state(self._state)
        except OSError as exc:
            # A heartbeat is bookkeeping; it must never fail the scan itself.
            logger.warning(f"Heartbeat write failed (ignored): {exc!r}")

    def checkpoint(self, **progress: Any) -> None:
        """Update heartbeat; raise RunCancelled if a stop was requested."""
        if self.should_stop():
            self._state["status"] = "stopping"
            self.heartbeat(**progress)
            raise RunCancelled("stop requested")
        self.heartbeat(**progress)

    def wait(self, seconds: float) -> None:
        """Cancellable replacement for time.sleep()."""
        deadline = time.monotonic() + seconds
        while True:
            self.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if _local_stop.wait(min(remaining, 0.5)):
                self.checkpoint()  # raises


class RunContext:
    """Holder of the global run lock; writes state.json transitions."""

    def __init__(self, lock: FileLock, state: dict[str, Any]):
        self._lock = lock
        self.state = state
        self.cancel = CancelToken(state)

    def finish(self, status: str, error: str | None = None) -> None:
        """Publish the final state and ALWAYS release the global run lock.

        The state write can fail for real (ENOSPC on a 96%-full disk is the
        case this guards). Before the try/finally that left the lock held by a
        still-living process, so every later scan — daemon and console alike —
        returned skipped="already_running" until the wedge probe restarted the
        daemon. Losing the final state.json is survivable; losing the lock is
        not, so the write is best-effort and the release is not."""
        self.state["status"] = status
        self.state["error"] = error
        now = datetime.now(timezone.utc).isoformat()
        self.state["heartbeat_at"] = now
        self.state["finished_at"] = now
        try:
            _write_state(self.state)
        except OSError as exc:
            logger.error(f"Could not write final run state ({exc!r}) — releasing the lock anyway")
            # Leave no stale "running" behind: read_state() returning None is
            # honest ("no run"), a stale running entry is not. Unlinking needs
            # no free space, unlike the atomic rewrite above.
            try:
                _STATE_FILE.unlink(missing_ok=True)
            except OSError:
                pass
        finally:
            try:
                clear_stop()
            finally:
                self._lock.release()

    def __enter__(self) -> "RunContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.finish("done")
        elif issubclass(exc_type, RunCancelled):
            self.finish("cancelled")
            return True  # cancellation is a normal outcome
        else:
            self.finish("error", error=f"{exc_type.__name__}: {exc}"[:200])
        return False


def try_acquire(kind: str, trigger: str) -> RunContext | None:
    """Take the global run lock without blocking.

    Returns None if another run holds it (its details are in read_state()).
    Being able to acquire the lock proves any previous holder is gone, so a
    leftover 'running' state is simply overwritten.
    """
    _CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    # thread_local=False so filelock's own __del__ safety net can force-release
    # from any thread; RunContext is the sole owner, no other thread touches it.
    lock = FileLock(_LOCK_FILE, thread_local=False)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return None

    clear_stop()  # a stop meant for a previous run must not kill this one
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "run_id": f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}",
        "kind": kind,
        "trigger": trigger,
        "pid": os.getpid(),
        "started_at": now,
        "heartbeat_at": now,
        "status": "running",
        "progress": {},
        "error": None,
        "finished_at": None,
    }
    try:
        _write_state(state)
    except OSError:
        # Never hold the lock for a run that could not even be announced.
        lock.release()
        raise
    return RunContext(lock, state)
