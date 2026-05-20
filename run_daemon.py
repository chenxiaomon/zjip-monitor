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
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from src.main import run_all

_SETTINGS = ROOT / "config" / "settings.yaml"
_DEFAULT_CRONS = ["0 9 * * *", "0 18 * * *"]
_TIMEZONE = "Asia/Shanghai"


def _load_crons() -> list[str]:
    if _SETTINGS.exists():
        data = yaml.safe_load(_SETTINGS.read_text(encoding="utf-8")) or {}
        crons = data.get("schedule", {}).get("crons")
        if crons:
            return list(crons)
    return _DEFAULT_CRONS


def _setup_logger() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        level="INFO",
        colorize=True,
    )
    logger.add(
        ROOT / "logs" / "run_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        level="DEBUG",
        encoding="utf-8",
    )


def _job() -> None:
    logger.info("Scheduled job triggered — starting run…")
    run_all()
    logger.info("Scheduled job done")


def main() -> int:
    _setup_logger()

    parser = argparse.ArgumentParser(description="Resident daemon for zjip-monitor")
    parser.add_argument(
        "--cron",
        help='Override cron expression, e.g. "0 9 * * *"',
    )
    args = parser.parse_args()

    crons = [args.cron] if args.cron else _load_crons()

    scheduler = BlockingScheduler(timezone=_TIMEZONE)
    for expr in crons:
        trigger = CronTrigger.from_crontab(expr, timezone=_TIMEZONE)
        scheduler.add_job(_job, trigger, misfire_grace_time=300)
        logger.info(f"Scheduled: {expr} ({_TIMEZONE})")

    logger.info("Daemon started — press Ctrl-C to stop")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
