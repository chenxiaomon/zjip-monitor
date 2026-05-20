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
    args = parser.parse_args()

    output = run_all(force_login=args.force_login)
    results = output.get("results", [])
    report_path = output.get("report_path")

    if report_path:
        logger.info(f"📄 报表已生成：{report_path.resolve()}")
        logger.info("   用浏览器打开该文件即可查看")

    failed = [r for r in results if r.get("error")]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
