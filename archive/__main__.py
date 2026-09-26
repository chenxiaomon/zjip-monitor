"""python -m archive pull | index | release | restore — see archive/__init__.py."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import index, pull, release
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
    p_rel = sub.add_parser("release", help="把已归档的旧文件从服务器删除以腾空间（默认只预演）")
    p_rel.add_argument("--yes", action="store_true", help="正式释放（不加就只预演，服务器上什么都不删）")
    p_rel.add_argument("--days", type=int, default=release.DEFAULT_DAYS,
                       help=f"办结后多少天没有变动才释放（默认 {release.DEFAULT_DAYS}；服务器按自己的配置再核一遍）")
    p_rel.add_argument("--company", metavar="企业目录", help="只处理这一家（先小范围试）")
    p_res = sub.add_parser("restore", help="把某条申请已释放的文件送回服务器")
    p_res.add_argument("company_dir", metavar="企业目录")
    p_res.add_argument("reg_no", metavar="登记号")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.env) if args.env else None)
    if args.command == "pull":
        return pull.run(cfg, reports=args.reports)
    if args.command == "release":
        return release.run_release(cfg, yes=args.yes, days=args.days, company=args.company)
    if args.command == "restore":
        return release.run_restore(cfg, args.company_dir, args.reg_no)
    return index.run(cfg)


if __name__ == "__main__":
    sys.exit(main())
