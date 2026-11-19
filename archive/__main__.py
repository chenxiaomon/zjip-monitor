"""python -m archive pull | index | release | restore | bundle | auto | schedule … — see archive/__init__.py."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import index, pull, release, report
from .config import load_config
from .lock import ArchiveBusy, archive_lock

# Commands that write the archive (or the server from it) take the lock
# exclusively; bundle only reads, so it shares it — but never with a pull.
_EXCLUSIVE = {"pull", "index", "release", "restore", "push-facts"}
_SHARED = {"bundle"}


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
                       help=f"通知书：办结后多少天没有变动才释放（默认 {release.DEFAULT_DAYS}；服务器按自己的配置再核一遍）")
    p_rel.add_argument("--cert-days", type=int, default=release.DEFAULT_CERT_DAYS,
                       help=f"证书：办结后多少天没有变动才释放（默认 {release.DEFAULT_CERT_DAYS}；服务器同样再核一遍）")
    p_rel.add_argument("--company", metavar="企业目录", help="只处理这一家（先小范围试）")
    sub.add_parser("push-facts", help="把已释放通知书的解析结果（证书有效期等）送到服务器，供控制台显示")
    p_res = sub.add_parser("restore", help="把某条申请已释放的文件送回服务器；--certificates 送回全部已释放的证书")
    p_res.add_argument("company_dir", metavar="企业目录", nargs="?")
    p_res.add_argument("reg_no", metavar="登记号", nargs="?")
    p_res.add_argument("--certificates", action="store_true",
                       help="送回所有已释放的当前证书（交付包要用；默认只预演）")
    p_res.add_argument("--company", metavar="企业目录", help="配合 --certificates：只处理这一家")
    p_res.add_argument("--yes", action="store_true", help="配合 --certificates：正式送回")
    p_b = sub.add_parser("bundle", help="给一家企业打交付包（证书清单 + 证书），放到归档目录的「交付包」下")
    p_b.add_argument("company", metavar="企业", help="企业名称、企业目录名，或只对得上一家的一段名字")
    p_b.add_argument("--out", metavar="目录", help="放到别处（不能是归档自己的数据目录）")
    sub.add_parser("auto", help="定时任务跑的那一套：先拉通知书、再拉其余、再入库，有新通知书或失败时弹通知")
    p_sch = sub.add_parser("schedule", help="本机定时归档（launchd）：install / uninstall / status / test-notify")
    p_sch.add_argument("action", choices=("install", "uninstall", "status", "test-notify"))
    p_s = sub.add_parser("search", help="在全部通知书与证书正文里搜关键词")
    p_s.add_argument("term", metavar="关键词")
    p_s.add_argument("--kind", help="只看某一类：补正 / 公示 / 公告 / 不予登记 / 异议 / 国家局证书 / 省证书")
    p_s.add_argument("--limit", type=int, default=20)
    sub.add_parser("report", help="补正理由、补正轮数、登记用时、将到期证书的统计报告")
    args = parser.parse_args(argv)

    if args.command == "restore":
        if args.certificates and (args.company_dir or args.reg_no):
            parser.error("--certificates 不和 企业目录/登记号 一起用；只处理一家请用 --company")
        if not args.certificates and (not (args.company_dir and args.reg_no) or args.company or args.yes):
            parser.error("restore 需要 企业目录 和 登记号（或用 --certificates）")

    env_file = Path(args.env) if args.env else None
    cfg = load_config(env_file)
    if args.command in _EXCLUSIVE or args.command in _SHARED:
        try:
            with archive_lock(cfg.archive_dir, what=f"archive {args.command}", shared=args.command in _SHARED):
                return _run(args, cfg)
        except ArchiveBusy as exc:
            print(f"✗ {exc}，稍后再试")
            return 1
    if args.command == "auto":
        from . import schedule
        return schedule.run_scheduled(cfg)  # takes the lock itself, waiting for a manual run
    if args.command == "schedule":
        from . import schedule
        if args.action == "install":
            return schedule.install(cfg, env_file=env_file)
        if args.action == "uninstall":
            return schedule.uninstall()
        if args.action == "status":
            return schedule.status(cfg)
        return schedule.test_notify()
    return _run(args, cfg)


def _run(args, cfg) -> int:
    if args.command == "pull":
        return pull.run(cfg, reports=args.reports)
    if args.command == "release":
        return release.run_release(cfg, yes=args.yes, days=args.days, cert_days=args.cert_days,
                                   company=args.company)
    if args.command == "restore":
        if args.certificates:
            return release.run_restore_certificates(cfg, company=args.company, yes=args.yes)
        return release.run_restore(cfg, args.company_dir, args.reg_no)
    if args.command == "push-facts":
        return release.run_push_facts(cfg)
    if args.command == "bundle":
        from . import bundle  # imports the console's packaging code; only this command needs it
        return bundle.run_bundle(cfg, args.company, out=Path(args.out) if args.out else None)
    if args.command == "search":
        return report.run_search(cfg, args.term, kind=args.kind, limit=args.limit)
    if args.command == "report":
        return report.run_report(cfg)
    return index.run(cfg)


if __name__ == "__main__":
    sys.exit(main())
