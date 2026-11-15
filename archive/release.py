"""python -m archive release / restore — free server space, and get files back.

release: pick candidates from archive.sqlite with src.archive_policy, re-hash
the local copies (a missing or changed local copy is never offered), and send
the list to scripts/archive_release.py on the server, which re-checks all of
it — lock, path, hash, and the policy against its own snapshot — before it
deletes anything. A dry run unless --yes: the server then only verifies.

restore: copy one record's released files back to the server and clear their
archived marks, e.g. when a client asks for an old certificate.

restore --certificates: the same for every released current certificate at
once. Certificates stay on the server since the console's 交付包 hands them
out (src/archive_policy), so the ones an earlier release took are sent back:
one rsync of a hard-linked staging tree, one ssh call to clear the marks.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from src.archive_policy import is_certificate, releasable
from src.runtime_settings import ArchiveSettings

from .config import ArchiveConfig
from .index import _sha256

DEFAULT_DAYS = ArchiveSettings().release_after_terminal_days


def select(db_path: Path, *, days: int, company: str | None = None,
           now: datetime | None = None) -> list[dict]:
    """Files still on the server that the policy says may go."""
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "SELECT d.path, d.company_dir, d.reg_no, d.filename, d.doc_kind, d.sha256, d.size, "
            "r.status_code, r.platform_updated_at "
            "FROM documents d LEFT JOIN records r ON r.reg_no = d.reg_no "
            "WHERE d.released_at IS NULL AND d.path LIKE 'attachments/%' ORDER BY d.path"
        ).fetchall()
    finally:
        db.close()
    picked = []
    for path, company_dir, reg_no, filename, kind, sha, size, status, updated in rows:
        if company and company_dir != company:
            continue
        updated_at = datetime.fromisoformat(updated) if updated else None
        if releasable(filename, status, updated_at, days=days, now=now):
            picked.append({"path": path, "company_dir": company_dir, "reg_no": reg_no,
                           "filename": filename, "kind": kind, "sha256": sha, "size": size})
    return picked


def _ssh(cfg: ArchiveConfig, remote_command: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    argv = shlex.split(cfg.ssh_cmd) + [cfg.server, f"cd {shlex.quote(cfg.remote)} && {remote_command}"]
    return subprocess.run(argv, input=stdin, capture_output=True, text=True)


def _server_json(proc: subprocess.CompletedProcess) -> dict:
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        data = {"error": f"服务器没有返回结果（退出码 {proc.returncode}）：{proc.stderr.strip()[-300:]}"}
    return data if isinstance(data, dict) else {"error": f"服务器返回了意外内容：{data!r:.200}"}


def run_release(cfg: ArchiveConfig, *, yes: bool = False, days: int = DEFAULT_DAYS,
                company: str | None = None) -> int:
    if not cfg.db_path.exists():
        print("✗ 还没有 archive.sqlite，先运行 python -m archive pull 和 index")
        return 1
    candidates = select(cfg.db_path, days=days, company=company)
    verified = []
    for c in candidates:
        local = cfg.archive_dir / c["path"]
        if local.is_file() and _sha256(local) == c["sha256"]:
            verified.append(c)
        else:
            print(f"! 本机副本缺失或已变，不释放：{c['path']}")
    if not verified:
        print("没有需要释放的文件")
        return 0

    mb = sum(c["size"] for c in verified) / 1048576
    kinds = Counter(c["kind"] for c in verified)
    print(f"候选 {len(verified)} 个文件，共 {mb:.1f}MB（办结满 {days} 天，或已被新一轮替换）：")
    print("  " + "、".join(f"{k} {n}" for k, n in kinds.most_common()))

    items = [{"path": f"{c['company_dir']}/{c['reg_no']}/{c['filename']}", "sha256": c["sha256"]}
             for c in verified]
    command = ".venv/bin/python scripts/archive_release.py" + ("" if yes else " --dry-run")
    result = _server_json(_ssh(cfg, command, json.dumps(items, ensure_ascii=False)))
    if "error" in result:
        print(f"✗ {result['error']}")
        return 1

    released = result.get("released") or []
    skipped = result.get("skipped") or []
    verb = "已释放" if yes else "服务器核对通过、可释放"
    print(f"✓ {verb} {len(released)} 个文件，共 {(result.get('bytes') or 0) / 1048576:.1f}MB")
    for reason, n in Counter(s.get("reason") for s in skipped).most_common():
        print(f"  跳过 {n} 个：{reason}")
    if not yes:
        print("这是预演，服务器上什么都没删。确认无误后加 --yes 正式释放。")
        return 0

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = sqlite3.connect(cfg.db_path)
    try:
        with db:
            for rel in released:
                db.execute(
                    "UPDATE documents SET released_at = ? WHERE path = ?", (stamp, f"attachments/{rel}")
                )
    finally:
        db.close()
    return 0


def facts_payload(db_path: Path) -> dict[str, dict[str, dict]]:
    """Facts of the notices this laptop holds but the server released:
    {"企业目录/登记号": {"文件名": {kind, deadline, ...}}}."""
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "SELECT company_dir, reg_no, filename, doc_kind, submitted_on, issued_on, deadline, "
            "valid_from, valid_to, reasons FROM documents WHERE released_at IS NOT NULL "
            "AND path LIKE 'attachments/%' AND doc_kind IN ('补正', '公示', '公告', '不予登记', '异议') "
            "ORDER BY company_dir, reg_no, filename"
        ).fetchall()
    finally:
        db.close()
    payload: dict[str, dict[str, dict]] = {}
    for company_dir, reg_no, filename, kind, submitted, issued, deadline, vfrom, vto, reasons in rows:
        payload.setdefault(f"{company_dir}/{reg_no}", {})[filename] = {
            "kind": kind, "submitted_on": submitted, "issued_on": issued, "deadline": deadline,
            "valid_from": vfrom, "valid_to": vto, "reasons": (reasons or "")[:400] or None,
        }
    return payload


def run_push_facts(cfg: ArchiveConfig) -> int:
    """Send the released notices' facts to the server so its console can show
    certificate expiry for records whose 公告 lives only here."""
    if not cfg.db_path.exists():
        print("✗ 还没有 archive.sqlite，先运行 python -m archive pull 和 index")
        return 1
    payload = facts_payload(cfg.db_path)
    n = sum(len(v) for v in payload.values())
    result = _server_json(_ssh(
        cfg, ".venv/bin/python scripts/backfill_notice_facts.py --from-stdin",
        json.dumps(payload, ensure_ascii=False),
    ))
    if "error" in result:
        print(f"✗ {result['error']}")
        return 1
    print(
        f"✓ 送出 {n} 份已释放通知书的解析结果；服务器新解析 {result.get('parsed', 0)} 份、"
        f"合并 {result.get('merged', 0)} 份、更新 {result.get('records', 0)} 条记录"
        + (f"、{result['unreadable']} 份无法解析" if result.get("unreadable") else "")
    )
    return 0


def run_restore(cfg: ArchiveConfig, company_dir: str, reg_no: str) -> int:
    rec = cfg.archive_dir / "attachments" / company_dir / reg_no
    if not cfg.db_path.exists() or not rec.is_dir():
        print(f"✗ 本机归档里没有 {company_dir}/{reg_no}")
        return 1
    db = sqlite3.connect(cfg.db_path)
    try:
        names = [n for (n,) in db.execute(
            "SELECT filename FROM documents WHERE company_dir = ? AND reg_no = ? "
            "AND released_at IS NOT NULL AND path LIKE 'attachments/%'",
            (company_dir, reg_no),
        )]
    finally:
        db.close()
    files = [str(rec / n) for n in names if (rec / n).is_file()]
    if not files:
        print("这条记录没有已释放的文件")
        return 0

    target = f"{cfg.server}:{cfg.remote}/data/attachments/{company_dir}/{reg_no}/"
    # -rt, not -a: keep the times, but let the server own the files (root),
    # not the laptop user's uid
    proc = subprocess.run(["rsync", "-rt", "--ignore-existing", "-e", cfg.ssh_cmd, *files, target],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"✗ 送回服务器失败（rsync 退出码 {proc.returncode}）：{proc.stderr.strip()}")
        return 1
    result = _server_json(_ssh(
        cfg, ".venv/bin/python scripts/archive_release.py --unarchive "
        + shlex.quote(f"{company_dir}/{reg_no}")
    ))
    if "error" in result:
        print(f"✗ 文件已送回，但清除归档标记失败：{result['error']}")
        return 1

    restored = result.get("restored") or []
    db = sqlite3.connect(cfg.db_path)
    try:
        with db:
            for name in restored:
                db.execute(
                    "UPDATE documents SET released_at = NULL WHERE path = ?",
                    (f"attachments/{company_dir}/{reg_no}/{name}",),
                )
    finally:
        db.close()
    print(f"✓ 已送回 {len(restored)} 个文件：{'、'.join(restored)}")
    return 0


# --- restore --certificates ----------------------------------------------------

# Free space a restore must leave on the server beyond its own floor
RESTORE_MARGIN_MB = 200


def certificates_to_restore(db_path: Path, archive_dir: Path, *,
                            company: str | None = None) -> tuple[list[dict], list[str]]:
    """Current certificates an earlier release took off the server, each
    re-hashed here: (verified, problems). A local copy that is missing or no
    longer matches its recorded hash is a problem, never sent."""
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "SELECT path, company_dir, reg_no, filename, sha256, size FROM documents "
            "WHERE released_at IS NOT NULL AND superseded = 0 AND path LIKE 'attachments/%' "
            "ORDER BY path"
        ).fetchall()
    finally:
        db.close()
    verified: list[dict] = []
    problems: list[str] = []
    for path, company_dir, reg_no, filename, sha, size in rows:
        if not is_certificate(filename) or (company and company_dir != company):
            continue
        local = archive_dir / path
        if local.is_file() and _sha256(local) == sha:
            verified.append({"path": path, "company_dir": company_dir, "reg_no": reg_no,
                             "filename": filename, "size": size or 0})
        else:
            problems.append(path)
    return verified, problems


def _stage(items: list[dict], archive_dir: Path, staging: Path) -> None:
    """<企业目录>/<登记号>/<文件> under staging, hard-linked to the archive's
    copies (a copy where the file system refuses links), so a plain recursive
    rsync sends exactly these files and nothing else."""
    for c in items:
        dest = staging / c["company_dir"] / c["reg_no"] / c["filename"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = archive_dir / c["path"]
        try:
            os.link(src, dest)
        except OSError:
            shutil.copy2(src, dest)


def _space_line(label: str, free_mb: float, att_bytes: int, max_bytes: int) -> str:
    return (f"  {label}：剩余 {free_mb:.0f}MB，附件 {att_bytes / 1048576:.0f}MB"
            f"（告警线 {max_bytes / 1048576:.0f}MB）")


def run_restore_certificates(cfg: ArchiveConfig, *, company: str | None = None, yes: bool = False) -> int:
    if not cfg.db_path.exists():
        print("✗ 还没有 archive.sqlite，先运行 python -m archive pull 和 index")
        return 1
    items, problems = certificates_to_restore(cfg.db_path, cfg.archive_dir, company=company)
    for path in problems:
        print(f"! 本机副本缺失或已变，不送回：{path}")
    if not items:
        print("没有需要送回的证书")
        return 0

    total = sum(c["size"] for c in items)
    records = sorted({f"{c['company_dir']}/{c['reg_no']}" for c in items})
    print(f"待送回证书 {len(items)} 张，共 {total / 1048576:.1f}MB，涉及 {len(records)} 条登记")

    space = _server_json(_ssh(cfg, ".venv/bin/python scripts/archive_release.py --space"))
    if "error" in space:
        print(f"✗ {space['error']}")
        return 1
    try:
        free_mb = float(space["free_mb"])
        min_free = float(space["min_free_mb"])
        att_bytes = int(space["attachments_bytes"])
        max_bytes = int(space["max_total_bytes"])
    except (KeyError, TypeError, ValueError):
        print(f"✗ 服务器返回的空间信息不完整：{space!r:.200}")
        return 1
    after_free = free_mb - total / 1048576
    print(_space_line("送回前", free_mb, att_bytes, max_bytes))
    print(_space_line("送回后", after_free, att_bytes + total, max_bytes))
    if after_free < min_free + RESTORE_MARGIN_MB:
        print(f"✗ 送回后服务器只剩 {after_free:.0f}MB，低于下限 {min_free:.0f}MB + 余量 {RESTORE_MARGIN_MB}MB，不送")
        return 1
    if att_bytes + total > max_bytes:
        print("! 送回后附件会超过告警线，控制台会出现提示（设置 → attachments.max_total_mb 可调）")

    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=cfg.archive_dir))
    try:
        _stage(items, cfg.archive_dir, staging)
        # -t keeps the files' times; -O leaves the server's directory times
        # alone; no -a: the server owns the files (root), not the laptop's uid.
        argv = ["rsync", "-rtO", "--ignore-existing", "--stats", "-e", cfg.ssh_cmd]
        if not yes:
            argv.append("-n")
        argv += [f"{staging}/", f"{cfg.server}:{cfg.remote}/data/attachments/"]
        proc = subprocess.run(argv, capture_output=True, text=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if proc.returncode != 0:
        print(f"✗ 送回服务器失败（rsync 退出码 {proc.returncode}）：{proc.stderr.strip()[-500:]}")
        return 1
    if not yes:
        print("这是预演，服务器上什么都没写。确认无误后加 --yes 正式送回（请避开北京时间 09:00、18:00 的巡检）。")
        return 0

    result = _server_json(_ssh(
        cfg, ".venv/bin/python scripts/archive_release.py --unarchive-stdin",
        json.dumps(records, ensure_ascii=False),
    ))
    if "error" in result:
        print(f"✗ 文件已送回，但清除归档标记失败：{result['error']}（稍后重跑本命令即可，已送的不会重复传）")
        return 1
    restored = [
        f"attachments/{rel_dir}/{name}"
        for rel_dir, r in (result.get("records") or {}).items()
        for name in (r or {}).get("restored") or []
    ]
    db = sqlite3.connect(cfg.db_path)
    try:
        with db:
            for path in restored:
                db.execute("UPDATE documents SET released_at = NULL WHERE path = ?", (path,))
    finally:
        db.close()
    print(f"✓ 已送回并清除标记 {len(restored)} 个文件")
    for rel_dir in result.get("invalid") or []:
        print(f"! 服务器上没有这个记录目录，标记未清：{rel_dir}")
    return 0
