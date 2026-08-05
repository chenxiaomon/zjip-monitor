#!/usr/bin/env python3
"""Entry point: run one full cycle across all accounts.

Usage:
    python run_once.py              # normal run, reuse cached tokens
    python run_once.py --force-login  # force fresh browser login for every account
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import argparse
from loguru import logger
from src import control
from src.main import run_all

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one fetch cycle for all accounts")
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="Bypass token cache and do a fresh browser login for every account",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Ask the currently running scan (web/daemon/CLI) to stop, then exit",
    )
    args = parser.parse_args()

    if args.stop:
        state = control.read_state()
        if state and state.get("status") in ("running", "stopping"):
            control.request_stop()
            logger.info(
                f"已发送停止请求（当前运行：pid={state.get('pid')}, "
                f"trigger={state.get('trigger')}），最长约 1 分钟后停止"
            )
        else:
            logger.info("当前没有正在运行的巡检")
        return 0

    output = run_all(force_login=args.force_login, trigger="cli")
    if output.get("skipped"):
        holder = control.read_state() or {}
        logger.error(
            f"另一巡检进行中（pid={holder.get('pid')}, trigger={holder.get('trigger')}）"
            "— 本次退出。可用 --stop 请求其停止"
        )
        return 2

    results = output.get("results", [])
    report_path = output.get("report_path")

    if report_path:
        logger.info(f"📄 报表已生成：{report_path.resolve()}")
        logger.info("   用浏览器打开该文件即可查看")

    failed = [r for r in results if r.get("error")]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
