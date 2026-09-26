"""python -m archive pull | index — see archive/__init__.py."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import index, pull
from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m archive",
        description="zjip-monitor 本机归档：把服务器数据拉到本地（只增不减）并建成 SQLite 库",
    )
    parser.add_argument("--env", help="配置文件（默认 archive/archive.env）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_pull = sub.add_parser("pull", help="rsync 拉取附件、快照、事件日志到归档目录（本地只增不减）")
    p_pull.add_argument("--reports", action="store_true", help="连 HTML 报表一起拉")
    sub.add_parser("index", help="增量更新 archive.sqlite（登记申请、状态历史、文件与通知书正文）")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.env) if args.env else None)
    if args.command == "pull":
        return pull.run(cfg, reports=args.reports)
    return index.run(cfg)


if __name__ == "__main__":
    sys.exit(main())
