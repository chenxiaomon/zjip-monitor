"""python -m archive release / restore — free server space, and get files back.

release: pick candidates from archive.sqlite with src.archive_policy, re-hash
the local copies (a missing or changed local copy is never offered), and send
the list to scripts/archive_release.py on the server, which re-checks all of
it — lock, path, hash, and the policy against its own snapshot — before it
deletes anything. A dry run unless --yes: the server then only verifies.

restore: copy one record's released files back to the server and clear their
archived marks, e.g. when a client asks for an old certificate.
"""

from __future__ import annotations

import json
import shlex
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from src.archive_policy import releasable
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
    proc = subprocess.run(["rsync", "-a", "--ignore-existing", "-e", cfg.ssh_cmd, *files, target],
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
