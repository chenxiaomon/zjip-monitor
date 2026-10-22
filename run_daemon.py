#!/usr/bin/env python3
"""Entry point: resident daemon, runs on a cron schedule.

Usage:
    python run_daemon.py               # uses schedule from settings.yaml
    python run_daemon.py --cron "0 9 * * *"  # override cron expression

Default schedule (settings.yaml → schedule.crons):
    09:00 and 18:00 every day (Asia/Shanghai)

The schedule is hot-reloaded: a job checks settings.yaml once a minute and
reschedules when schedule.crons really changed (not on every console save,
which rewrites the whole file). The schedule actually in force is published
to data/control/schedule.json for the settings page.

Press Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from src import control, ops_alerts
from src.logging_setup import setup_logging
from src.main import run_all

_SETTINGS = ROOT / "config" / "settings.yaml"
_DEFAULT_CRONS = ["0 9 * * *", "0 18 * * *"]
_TIMEZONE = "Asia/Shanghai"
_WATCH_SECONDS = 60
_JOB_PREFIX = "cron:"


class _Unreadable(Exception):
    pass


def _read_crons() -> list[str]:
    """schedule.crons as configured ([] when not set or the file is missing).
    Raises _Unreadable when the file exists but cannot be read or parsed."""
    try:
        text = _SETTINGS.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise _Unreadable(str(exc)) from exc
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise _Unreadable(str(exc)) from exc
    schedule = data.get("schedule") if isinstance(data, dict) else None
    crons = schedule.get("crons") if isinstance(schedule, dict) else None
    return [str(c) for c in crons] if isinstance(crons, list) else []


def _load_crons() -> list[str]:
    try:
        crons = _read_crons()
    except _Unreadable as exc:
        logger.error(f"读取 settings.yaml 失败（{exc}）— 使用默认计划 {_DEFAULT_CRONS}")
        return list(_DEFAULT_CRONS)
    return crons or list(_DEFAULT_CRONS)


def _settings_key() -> tuple[int, int] | None:
    try:
        st = _SETTINGS.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _valid(crons: list[str]) -> list[str]:
    ok = []
    for expr in crons:
        try:
            CronTrigger.from_crontab(expr, timezone=_TIMEZONE)
        except ValueError as exc:
            logger.error(f"非法 cron 表达式 {expr!r}（{exc}）— 已跳过")
            continue
        ok.append(expr)
    return ok


def _apply_schedule(scheduler, crons: list[str], source: str) -> list[str]:
    """Replace the scan jobs with `crons` (already validated) and publish the
    result. Other jobs (the wedge probe, the schedule watch) are untouched."""
    for job in scheduler.get_jobs():
        if job.id.startswith(_JOB_PREFIX):
            job.remove()
    for i, expr in enumerate(crons):
        scheduler.add_job(
            _job, CronTrigger.from_crontab(expr, timezone=_TIMEZONE),
            id=f"{_JOB_PREFIX}{i}", misfire_grace_time=300,
        )
        logger.info(f"Scheduled: {expr} ({_TIMEZONE})")
    control.write_schedule_state(crons, source)
    return list(crons)


class _ScheduleWatch:
    """Runs every minute: settings.yaml unchanged → nothing; changed but the
    crons are the same (a notify or login save) → nothing; an unreadable
    file or a list with no valid expression → keep the running schedule.
    Only a real, valid change reschedules."""

    def __init__(self, scheduler, current: list[str], key: tuple[int, int] | None):
        self.scheduler = scheduler
        self.current = list(current)
        self.key = key

    def __call__(self) -> None:
        key = _settings_key()
        if key == self.key:
            return
        self.key = key
        try:
            configured = _read_crons()
        except _Unreadable as exc:
            logger.warning(f"settings.yaml 暂时读不了（{exc}）— 保持现有巡检计划 {self.current}")
            return
        valid = _valid(configured or list(_DEFAULT_CRONS))
        if not valid:
            logger.error(f"新的巡检计划里没有可用的 cron 表达式 — 保持现有计划 {self.current}")
            return
        if valid == self.current:
            return
        logger.info(f"巡检计划已热加载：{self.current} → {valid}")
        self.current = _apply_schedule(self.scheduler, valid, "settings" if configured else "default")


def _exit_if_wedged_on_self() -> None:
    """Self-heal: if the lock holder is *this* process but its heartbeat is
    dead, an earlier run is wedged in a thread that will never finish (seen
    2026-09-04: a Playwright login hung, blocking every later scheduled run).
    Nothing inside the process can free it, so exit non-zero and let systemd
    (Restart=on-failure) bring up a clean daemon."""
    state = control.read_state() or {}
    if state.get("status") == "stale" and state.get("pid") == os.getpid():
        logger.critical(
            f"Run {state.get('run_id')} is wedged inside this process "
            f"(last heartbeat {state.get('heartbeat_at')}) — exiting so systemd restarts the daemon"
        )
        os._exit(1)


def _refresh_ops_alerts() -> None:
    """Re-evaluate the console banner's alerts once per round.

    Bookkeeping only: a failure here must never change what the scan reported,
    so every path swallows its own errors."""
    try:
        ops_alerts.refresh_and_log()
    except Exception as exc:
        logger.warning(f"运维告警刷新失败（非致命）: {exc!r}")


# A colleague's single-account scan holds the global run lock for a minute or
# two. Skipping the whole scheduled round because one company was being
# re-checked at 9:00 would leave every other company a round behind, so the
# round waits for such a scan instead — never for a full round or a wedged one.
_SHORT_RUN_WAIT_SECONDS = 600
_SHORT_RUN_POLL_SECONDS = 15


def _run_round() -> dict:
    deadline = time.monotonic() + _SHORT_RUN_WAIT_SECONDS
    while True:
        output = run_all(trigger="daemon")
        if not output.get("skipped"):
            return output
        holder = control.read_state() or {}
        if holder.get("kind") != "scan_one" or holder.get("status") != "running":
            return output
        if time.monotonic() >= deadline:
            return output
        logger.info("单账号巡检进行中 — 等它结束后开始本轮定时巡检")
        time.sleep(_SHORT_RUN_POLL_SECONDS)


def _job() -> None:
    if control.is_daemon_paused():
        logger.info("巡检已暂停（data/control/pause.flag 存在）— 跳过本轮")
        _refresh_ops_alerts()  # 暂停本身就是一条横幅告警，也让「已恢复」能被记下
        return
    logger.info("Scheduled job triggered — starting run…")
    try:
        output = _run_round()
    except Exception as exc:
        logger.exception(f"Scheduled run failed: {exc}")
        _refresh_ops_alerts()
        return
    if output.get("skipped"):
        # Refresh BEFORE the wedge probe: a skipped round usually means the
        # lock holder is stuck, _exit_if_wedged_on_self() may end the process
        # on the spot, and the banner is where that has to show up.
        _refresh_ops_alerts()
        _exit_if_wedged_on_self()
        logger.warning("另一巡检进行中（web/CLI 触发）— 本轮跳过")
        return
    if output.get("cancelled"):
        logger.info("Scheduled job stopped by user request")
    else:
        logger.info("Scheduled job done")
    _refresh_ops_alerts()


def main() -> int:
    setup_logging("daemon")

    parser = argparse.ArgumentParser(description="Resident daemon for zjip-monitor")
    parser.add_argument(
        "--cron",
        help='Override cron expression, e.g. "0 9 * * *"',
    )
    args = parser.parse_args()

    scheduler = BlockingScheduler(timezone=_TIMEZONE)
    key = _settings_key()  # before reading, so an edit in between is still noticed
    if args.cron:
        crons, source = [args.cron], "cli"
    else:
        crons = _load_crons()
        source = "default" if crons == _DEFAULT_CRONS else "settings"
    # One bad expression must not keep the daemon from starting
    valid = _valid(crons)
    if not valid:
        logger.error(f"没有可用的 cron 表达式 — 回退默认计划 {_DEFAULT_CRONS}")
        valid, source = list(_DEFAULT_CRONS), "default"
    applied = _apply_schedule(scheduler, valid, source)

    if not args.cron:
        # --cron pins the schedule for this process; otherwise follow settings.yaml.
        scheduler.add_job(_ScheduleWatch(scheduler, applied, key), "interval",
                          seconds=_WATCH_SECONDS, id="schedule-watch")

    # Self-heal probe: catches a run wedged inside this process within minutes
    # instead of waiting for the next cron slot (which APScheduler may even
    # skip with "maximum number of running instances reached").
    scheduler.add_job(_exit_if_wedged_on_self, "interval", minutes=10, id="wedge-probe")

    def _on_sigterm(signum, frame):  # noqa: ARG001
        logger.info("SIGTERM received — stopping scheduler, in-flight run will stop at next checkpoint…")
        control.request_local_stop()
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _on_sigterm)

    logger.info("Daemon started — press Ctrl-C to stop")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        control.request_local_stop()
        logger.info("Daemon stopping…")

    # Wait (bounded) for an in-flight run owned by this process to reach a
    # terminal state so Playwright cleanup runs and no chromium is orphaned.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        state = control.read_state()
        if (
            not state
            or state.get("pid") != os.getpid()
            or state.get("status") not in ("running", "stopping")
        ):
            break
        time.sleep(1)
    logger.info("Daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
