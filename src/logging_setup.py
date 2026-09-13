"""One loguru configuration for every zjip-monitor process.

Why this exists: run_daemon.py and run_once.py each carried their own copy of
the sink set-up, and the web process (systemd runs `uvicorn web.app:app`
directly) had no file log at all — only journalctl. uvicorn and APScheduler
talk to the standard `logging` module, so their lines never reached loguru
either. Every entry point now calls setup_logging(role) once; web/app.py also
calls it at import time because that is the only hook the production
`uvicorn web.app:app` command line offers.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

from loguru import logger

from src.runtime_settings import retention_settings

_ROOT = Path(__file__).resolve().parent.parent

_STDERR_FORMAT = "<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}"

# role -> (file name pattern, key under `retention:` in settings.yaml, default days).
# daemon and cli share run_*.log on purpose: ops grep that name when a scan
# misbehaves, whichever way it was started.
_FILE_SINKS: dict[str, tuple[str, str]] = {
    "daemon": ("run_{time:YYYY-MM-DD}.log", "logs_keep_days"),
    "cli": ("run_{time:YYYY-MM-DD}.log", "logs_keep_days"),
    "web": ("web_{time:YYYY-MM-DD}.log", "web_logs_keep_days"),
}

# Loggers that arrive with their own StreamHandlers and propagate=False: the
# uvicorn CLI applies its dictConfig before importing the app module, so by
# the time setup_logging() runs they would print to stderr on their own and
# never reach the file sink. APScheduler is listed for the same treatment in
# case a future version ships handlers.
_TAKEOVER_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "apscheduler")

# Third-party loggers that emit DEBUG on a timer or per lock/parser step. With
# the root at level 0 they would flood the DEBUG file: sse_starlette logs
# every ping of every open dashboard (4 lines/min/client), filelock every
# acquire/release. The root partition is >90% full — not worth it.
_QUIET_DEBUG_LOGGERS = ("sse_starlette", "filelock", "python_multipart", "multipart")

_configured_role: str | None = None


class InterceptHandler(logging.Handler):
    """Forward standard `logging` records into loguru (pattern from loguru's docs)."""

    def emit(self, record: logging.LogRecord) -> None:
        # Same-named loguru level when there is one (uvicorn's TRACE=5 matches
        # loguru's), otherwise the raw number so nothing is dropped.
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Walk out of logging/__init__.py (and import machinery) so the file
        # log's {name}:{function}:{line} names the real caller.
        frame, depth = inspect.currentframe(), 0
        while frame:
            filename = frame.f_code.co_filename
            is_logging = filename == logging.__file__
            is_frozen = "importlib" in filename and "_bootstrap" in filename
            if depth > 0 and not (is_logging or is_frozen):
                break
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _keep_days(field: str) -> int:
    """retention.<field> in days, validated by src/runtime_settings.

    Deliberately not a second parser: runtime_settings owns the rules for
    every numeric key in settings.yaml (integers only, ≥ 1, no bools or
    numeric strings) and logs one warning per bad key. A private copy here
    once accepted "30" where every neighbouring key rejected it."""
    return int(getattr(retention_settings(), field))


def _intercept_std_logging() -> None:
    # force=True drops whatever is on the root already — including the
    # InterceptHandler of an earlier setup_logging() — so a record can never
    # be delivered twice. Level 0: loguru's sinks decide what to keep.
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in _TAKEOVER_LOGGERS:
        std_logger = logging.getLogger(name)
        for handler in list(std_logger.handlers):
            std_logger.removeHandler(handler)
        std_logger.propagate = True
    for name in _QUIET_DEBUG_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)


def setup_logging(role: str) -> None:
    """Configure loguru and standard logging for this process. Idempotent.

    role: "web" | "daemon" | "cli".

    The FIRST call in a process wins. A later call with the same role is a
    no-op; a later call with a different role is ignored (a DEBUG line says
    so). Rationale: the entry point that started the process decides where
    its logs go — web/app.py calls this at import time, and importing a module
    must never be able to redirect a CLI's log file.

    Sinks:
      * stderr, INFO, `HH:mm:ss | LEVEL | message`; coloured only when stderr
        is a TTY, so journald never sees escape codes.
      * file, DEBUG, rotated at 00:00, utf-8:
          daemon / cli -> <logs>/run_YYYY-MM-DD.log, kept retention.logs_keep_days (30)
          web          -> <logs>/web_YYYY-MM-DD.log, kept retention.web_logs_keep_days (14)
        Both keys are read from config/settings.yaml through
        runtime_settings (invalid values fall back to the default there). <logs> is the repository's logs/ unless
        ZJIP_LOG_DIR says otherwise; ZJIP_LOG_TO_FILE=0 disables the file sink
        entirely (tests). A file sink that cannot be opened is reported on
        stderr and skipped — a broken log directory must not take the console
        or the scheduler down.

    Standard logging is intercepted at the root (level 0) so uvicorn and
    APScheduler lines land in the same sinks; handlers uvicorn's CLI installed
    before this ran are removed and propagation restored. uvicorn.access keeps
    logging requests at INFO. A few third-party loggers that emit DEBUG on a
    timer are pinned to INFO (see _QUIET_DEBUG_LOGGERS).
    """
    global _configured_role
    if role not in _FILE_SINKS:
        raise ValueError(f"unknown logging role {role!r}; expected one of {sorted(_FILE_SINKS)}")
    if _configured_role is not None:
        if _configured_role != role:
            logger.debug(f"setup_logging({role!r}) ignored — process already logs as {_configured_role!r}")
        return
    _configured_role = role

    logger.remove()
    stderr = sys.stderr
    colorize = bool(stderr is not None and hasattr(stderr, "isatty") and stderr.isatty())
    logger.add(stderr, format=_STDERR_FORMAT, level="INFO", colorize=colorize)

    if os.getenv("ZJIP_LOG_TO_FILE", "1") != "0":
        pattern, retention_field = _FILE_SINKS[role]
        log_dir = Path(os.getenv("ZJIP_LOG_DIR") or _ROOT / "logs")
        try:
            logger.add(
                log_dir / pattern,
                rotation="00:00",
                retention=timedelta(days=_keep_days(retention_field)),
                level="DEBUG",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error(f"无法打开日志目录 {log_dir}（{exc}）— 本进程日志只输出到 stderr")

    _intercept_std_logging()
