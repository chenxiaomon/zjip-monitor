#!/usr/bin/env python3
"""Single-account login and scrape test.

Usage:
    cd zjip-monitor
    python scripts/test_login.py

Reads ZJIP_ACCOUNT and ZJIP_PASSWORD from .env (or prompts if missing).
Always performs a fresh browser login (force_refresh=True) so you can
verify the Playwright flow end-to-end.

Exit codes:
    0 — success
    1 — login or scrape failure
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

# Ensure project root is importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from loguru import logger

from src.exceptions import CaptchaRequiredError, LoginError, ScraperError, TokenExpiredError
from src.login import get_token
from src.scraper import decode_status, fetch_all_records

# ---------------------------------------------------------------------------
# Logger setup — pretty output to stderr, suppress noise from libraries
# ---------------------------------------------------------------------------

logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    level="DEBUG",
    colorize=True,
)


def main() -> int:
    load_dotenv(ROOT / ".env")

    account = os.getenv("ZJIP_ACCOUNT") or input("账号 (ZJIP_ACCOUNT): ").strip()
    password = os.getenv("ZJIP_PASSWORD") or getpass.getpass("密码 (ZJIP_PASSWORD): ")

    if not account or not password:
        logger.error("账号或密码为空，退出")
        return 1

    # ── Step 1: Login ──────────────────────────────────────────────────────
    logger.info(f"开始登录账号: {account}")
    try:
        token = get_token(account, password, force_refresh=True)
    except CaptchaRequiredError as exc:
        logger.error(f"需要人工过验证码: {exc}")
        logger.info(
            "解决办法: 将 config/settings.yaml 中 login.headless 改为 false，"
            "手动过一次验证码后 token 将被缓存，后续运行可无人工干预。"
        )
        return 1
    except LoginError as exc:
        logger.error(f"登录失败: {exc}")
        return 1

    # Print token summary
    logger.info(f"Token 获取成功 — 长度: {len(token)}，前缀: {token[:20]}…")

    from src.login import _session_path  # inspect cached metadata
    session_file = _session_path(account)
    if session_file.exists():
        meta = json.loads(session_file.read_text(encoding="utf-8"))
        logger.info(f"  fetched_at : {meta.get('fetched_at', 'unknown')}")
        logger.info(f"  expires_at : {meta.get('expires_at', 'unknown')}")

    # ── Step 2: Fetch records (first 2 pages max for the test) ─────────────
    logger.info("开始抓取登记列表（测试模式：最多 2 页，每页 10 条）…")
    try:
        records = fetch_all_records(token, page_size=10)
    except TokenExpiredError as exc:
        logger.error(f"Token 已过期: {exc}")
        return 1
    except ScraperError as exc:
        logger.error(f"数据抓取失败: {exc}")
        return 1

    total = len(records)
    logger.info(f"共抓取到 {total} 条记录")

    if records:
        logger.info("第 1 条记录:")
        print(json.dumps(records[0], ensure_ascii=False, indent=2))

        if total > 1:
            logger.info(f"（还有 {total - 1} 条记录，此处省略）")

        # Summary of audit statuses
        status_counter: dict[str, int] = {}
        for r in records:
            code = r.get("dataRegAuditStatus")
            label = decode_status(code)
            key = f"{label}({code})"
            status_counter[key] = status_counter.get(key, 0) + 1

        logger.info("审核状态分布:")
        for status, count in sorted(status_counter.items()):
            logger.info(f"  {status}: {count} 条")
    else:
        logger.warning("未找到任何登记记录（账号下可能暂无数据，或列表 API 返回了空）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
