"""src/logging_setup.py — one loguru configuration for web / daemon / cli.

Standard-library records (uvicorn, APScheduler) must reach loguru's sinks with
their level and traceback intact, set-up must be idempotent, and the file sink
must obey ZJIP_LOG_TO_FILE / ZJIP_LOG_DIR so a test process never writes into
the repository's logs/.

Every test here starts from "fresh process" state and hands loguru back in a
stderr-only configuration, because web/app.py calls setup_logging("web") at
import time and later tests must not inherit a sink pointing at a tmp dir.
"""

import io
import logging
import logging.config
import re
import sys
from contextlib import contextmanager
from datetime import timedelta

import pytest
from loguru import logger

from src import logging_setup, runtime_settings
from src.logging_setup import InterceptHandler, setup_logging


@pytest.fixture
def fresh_process(monkeypatch, tmp_path):
    """Simulate a process that has not configured logging yet.

    The file sink is off unless a test turns it on, and would then land in the
    returned directory. Teardown re-runs a stderr-only setup so the rest of
    the suite is unaffected by whatever sinks a test created.
    """
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("ZJIP_LOG_TO_FILE", "0")
    monkeypatch.setenv("ZJIP_LOG_DIR", str(log_dir))
    monkeypatch.setattr(logging_setup, "_configured_role", None)
    yield log_dir
    monkeypatch.setenv("ZJIP_LOG_TO_FILE", "0")
    logging_setup._configured_role = None
    setup_logging("cli")


@contextmanager
def _captured():
    """Collect loguru messages on a sink that is removed afterwards.

    Must be entered AFTER setup_logging(): that call wipes every sink.
    """
    messages = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def _only_file(log_dir):
    files = list(log_dir.iterdir())
    assert len(files) == 1, files
    return files[0]


# ---------------------------------------------------------------------------
# stdlib → loguru bridge
# ---------------------------------------------------------------------------

def test_std_logging_reaches_loguru_sink(fresh_process):
    setup_logging("web")
    with _captured() as messages:
        logging.getLogger("uvicorn.error").warning("port %d in use", 8000)

    (msg,) = messages
    assert msg.record["level"].name == "WARNING"
    assert msg.record["message"] == "port 8000 in use"
    # depth handling: the record points at the caller, not at logging/__init__.py
    assert msg.record["function"] == "test_std_logging_reaches_loguru_sink"


def test_exception_info_is_preserved(fresh_process):
    setup_logging("daemon")
    with _captured() as messages:
        try:
            1 / 0
        except ZeroDivisionError:
            logging.getLogger("apscheduler.executors.default").exception("job failed")

    (msg,) = messages
    assert msg.record["level"].name == "ERROR"
    assert msg.record["exception"] is not None
    assert msg.record["exception"].type is ZeroDivisionError
    assert "ZeroDivisionError" in str(msg)  # traceback text is part of the formatted line


def test_unknown_std_level_falls_back_to_number(fresh_process):
    setup_logging("cli")
    with _captured() as messages:
        logging.getLogger("some.lib").log(35, "custom level")

    (msg,) = messages
    assert msg.record["level"].no == 35
    assert msg.record["message"] == "custom level"


def test_uvicorn_cli_handlers_are_taken_over(fresh_process):
    """Production runs `uvicorn web.app:app`: uvicorn's dictConfig is applied
    BEFORE our module is imported. Its own StreamHandlers must be dropped and
    propagation restored, otherwise access lines print twice / bypass the file."""
    import uvicorn.config

    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    assert logging.getLogger("uvicorn.access").handlers  # precondition: uvicorn installed its own
    assert logging.getLogger("uvicorn.access").propagate is False

    setup_logging("web")

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "apscheduler"):
        std = logging.getLogger(name)
        assert std.handlers == [], name
        assert std.propagate is True, name
    # access log keeps flowing at INFO
    with _captured() as messages:
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", "/", "1.1", 200)
    assert [m.record["message"] for m in messages] == ['1.2.3.4 - "GET / HTTP/1.1" 200']


def test_chatty_third_party_debug_is_quieted(fresh_process):
    setup_logging("web")
    with _captured() as messages:
        logging.getLogger("sse_starlette.sse").debug("ping: b'...'")
        logging.getLogger("filelock").debug("Attempting to acquire lock")
        logging.getLogger("sse_starlette.sse").info("kept")
        logging.getLogger("src.anything").debug("own debug still flows")
    assert [m.record["message"] for m in messages] == ["kept", "own debug still flows"]


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------

def test_repeated_setup_does_not_duplicate_sinks_or_handlers(fresh_process):
    with pytest.MonkeyPatch.context() as mp:
        err = io.StringIO()
        mp.setattr(sys, "stderr", err)  # the stderr sink binds to this object

        setup_logging("web")
        setup_logging("web")
        setup_logging("daemon")  # different role later in the same process: ignored

        assert sum(isinstance(h, InterceptHandler) for h in logging.getLogger().handlers) == 1
        with _captured() as messages:
            logging.getLogger("uvicorn.error").info("once")
            logger.info("twice")
        assert [m.record["message"] for m in messages] == ["once", "twice"]

        out = err.getvalue()
        assert out.count("once") == 1
        assert out.count("twice") == 1
        assert "\x1b[" not in out  # StringIO is not a TTY → no colour codes
        assert re.search(r"^\d{2}:\d{2}:\d{2} \| INFO     \| once$", out, re.M)


def test_first_role_wins(fresh_process, monkeypatch):
    monkeypatch.setenv("ZJIP_LOG_TO_FILE", "1")
    setup_logging("web")
    setup_logging("daemon")
    logger.info("hello")

    assert re.fullmatch(r"web_\d{4}-\d{2}-\d{2}\.log", _only_file(fresh_process).name)


def test_unknown_role_is_rejected(fresh_process):
    with pytest.raises(ValueError):
        setup_logging("worker")


# ---------------------------------------------------------------------------
# file sink
# ---------------------------------------------------------------------------

def test_log_to_file_zero_creates_nothing(fresh_process):
    setup_logging("daemon")
    logger.info("hello")
    logging.getLogger("apscheduler.scheduler").info("Scheduler started")
    assert not fresh_process.exists()


def test_web_role_writes_web_log(fresh_process, monkeypatch):
    monkeypatch.setenv("ZJIP_LOG_TO_FILE", "1")
    setup_logging("web")
    logging.getLogger("uvicorn.error").info("Started server process [1]")
    logger.debug("debug lands in the file only")

    f = _only_file(fresh_process)
    assert re.fullmatch(r"web_\d{4}-\d{2}-\d{2}\.log", f.name)
    text = f.read_text(encoding="utf-8")
    assert "Started server process [1]" in text
    assert "debug lands in the file only" in text
    assert "\x1b[" not in text


@pytest.mark.parametrize("role", ["daemon", "cli"])
def test_daemon_and_cli_keep_run_log_name(fresh_process, monkeypatch, role):
    """Ops grep logs/run_YYYY-MM-DD.log — the name must not change."""
    monkeypatch.setenv("ZJIP_LOG_TO_FILE", "1")
    setup_logging(role)
    logger.info("scan done")

    f = _only_file(fresh_process)
    assert re.fullmatch(r"run_\d{4}-\d{2}-\d{2}\.log", f.name)
    assert "scan done" in f.read_text(encoding="utf-8")


def test_unwritable_log_dir_does_not_crash_the_process(fresh_process, monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("ZJIP_LOG_TO_FILE", "1")
    monkeypatch.setenv("ZJIP_LOG_DIR", str(blocker / "logs"))

    with pytest.MonkeyPatch.context() as mp:
        err = io.StringIO()
        mp.setattr(sys, "stderr", err)
        setup_logging("web")  # must not raise
        logger.info("still alive")

    out = err.getvalue()
    assert "still alive" in out
    assert "ERROR" in out and "日志" in out


# ---------------------------------------------------------------------------
# retention from settings.yaml
# ---------------------------------------------------------------------------

def _file_sink_kwargs(monkeypatch):
    """Record the kwargs of the logger.add() call that installs the file sink."""
    calls = []
    real_add = logger.add

    def spy(sink, *args, **kwargs):
        if not callable(sink) and not hasattr(sink, "write"):
            calls.append(kwargs)
        return real_add(sink, *args, **kwargs)

    monkeypatch.setattr(logging_setup.logger, "add", spy)
    return calls


@pytest.mark.parametrize(
    "role, settings, expected_days",
    [
        ("daemon", {}, 30),
        ("cli", {}, 30),
        ("web", {}, 14),
        ("daemon", {"retention": {"logs_keep_days": 7}}, 7),
        ("web", {"retention": {"web_logs_keep_days": 3}}, 3),
        # A quoted number is a typo everywhere else in settings.yaml; the
        # log retention used to be the one key that silently accepted it.
        ("web", {"retention": {"web_logs_keep_days": "5"}}, 14),
        ("daemon", {"retention": {"logs_keep_days": 0}}, 30),
        ("daemon", {"retention": {"logs_keep_days": -2}}, 30),
        ("daemon", {"retention": {"logs_keep_days": "abc"}}, 30),
        ("daemon", {"retention": {"logs_keep_days": True}}, 30),
        ("daemon", {"retention": {"logs_keep_days": None}}, 30),
        ("web", {"retention": "junk"}, 14),
        ("daemon", {"retention": {"web_logs_keep_days": 1}}, 30),  # other role's key is not mine
    ],
)
def test_retention_comes_from_settings_with_defaults(fresh_process, monkeypatch, role, settings, expected_days):
    monkeypatch.setenv("ZJIP_LOG_TO_FILE", "1")
    # Patch at the source settings_cache, not inside logging_setup: the point
    # of the change under test is that log retention goes through the same
    # runtime_settings validation as every other numeric key.
    monkeypatch.setattr(runtime_settings, "get_settings", lambda: settings)
    runtime_settings.reset_warnings()
    calls = _file_sink_kwargs(monkeypatch)

    setup_logging(role)

    (kwargs,) = calls
    assert kwargs["retention"] == timedelta(days=expected_days)
    assert kwargs["rotation"] == "00:00"
    assert kwargs["level"] == "DEBUG"
    assert kwargs["encoding"] == "utf-8"
    assert "enqueue" not in kwargs or kwargs["enqueue"] is False
