#!/usr/bin/env python3
"""Entry point: resident daemon, runs on a cron schedule.

Usage:
    python run_daemon.py               # uses schedule from settings.yaml
    python run_daemon.py --cron "0 9 * * *"  # override cron expression

Default schedule (settings.yaml → schedule.crons):
    09:00 and 18:00 every day (Asia/Shanghai)

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


def _load_crons() -> list[str]:
    try:
        if _SETTINGS.exists():
            data = yaml.safe_load(_SETTINGS.read_text(encoding="utf-8")) or {}
            crons = data.get("schedule", {}).get("crons")
            if crons:
                return list(crons)
    except Exception as exc:
        logger.error(f"读取 settings.yaml 失败（{exc}）— 使用默认计划 {_DEFAULT_CRONS}")
    return _DEFAULT_CRONS


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


def _job() -> None:
    if control.is_daemon_paused():
        logger.info("巡检已暂停（data/control/pause.flag 存在）— 跳过本轮")
        _refresh_ops_alerts()  # 暂停本身就是一条横幅告警，也让「已恢复」能被记下
        return
    logger.info("Scheduled job triggered — starting run…")
    try:
        output = run_all(trigger="daemon")
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

    crons = [args.cron] if args.cron else _load_crons()

    scheduler = BlockingScheduler(timezone=_TIMEZONE)
    for expr in crons:
        # One bad expression must not keep the daemon from starting
        try:
            trigger = CronTrigger.from_crontab(expr, timezone=_TIMEZONE)
        except ValueError as exc:
            logger.error(f"非法 cron 表达式 {expr!r}（{exc}）— 已跳过")
            continue
        scheduler.add_job(_job, trigger, misfire_grace_time=300)
        logger.info(f"Scheduled: {expr} ({_TIMEZONE})")

    if not scheduler.get_jobs():
        logger.error(f"没有可用的 cron 表达式 — 回退默认计划 {_DEFAULT_CRONS}")
        for expr in _DEFAULT_CRONS:
            scheduler.add_job(
                _job, CronTrigger.from_crontab(expr, timezone=_TIMEZONE),
                misfire_grace_time=300,
            )

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
