#!/usr/bin/env python3
"""Release archived attachments from the server — driven by `python -m archive release`.

    ... | python scripts/archive_release.py [--dry-run]
        stdin : JSON [{"path": "<企业目录>/<登记号>/<文件名>", "sha256": "<hex>"}, ...]
        stdout: JSON {"released": [...], "skipped": [{"path", "reason"}], "bytes": N, "dry_run": bool}
    python scripts/archive_release.py --unarchive <企业目录>/<登记号>
        after `python -m archive restore` copied the files back: forget their archived marks

A file is deleted only when every check passes:
  - this process holds the global run lock, so no scan is writing data/attachments
  - the path resolves inside data/attachments to a document (not .sync.json, not a hidden or .part file)
  - its sha256 equals the one the laptop verified its own copy against
  - src.archive_policy.releasable() agrees, judged from the server's own latest
    snapshot and settings.yaml → archive — not from the laptop's database
The record's .sync.json gets the file under "archived" BEFORE the file goes, so
a crash in between leaves a harmless extra copy, never a file the next sync
would silently fetch again.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from src import archive_policy, audit, control  # noqa: E402
from src.attachments import _MANIFEST_NAME, _safe_name, _sha256  # noqa: E402
from src.fsutil import atomic_write_text  # noqa: E402
from src.logging_setup import setup_logging  # noqa: E402
from src.runtime_settings import archive_settings  # noqa: E402
from src.scraper import int_or_none  # noqa: E402
from web.services.snapshot import get_latest_snapshot  # noqa: E402

_ATTACHMENTS_DIR = ROOT / "data" / "attachments"


def _resolve(rel: str, depth: int) -> Path | None:
    """rel as a path `depth` levels below data/attachments, or None if it is
    anything else (absolute, .., hidden parts, escapes via symlink)."""
    parts = Path(rel).parts
    if Path(rel).is_absolute() or len(parts) != depth or any(
        p in ("", ".", "..") or p.startswith(".") for p in parts
    ):
        return None
    base = _ATTACHMENTS_DIR.resolve()
    candidate = (_ATTACHMENTS_DIR / rel).resolve()
    if not candidate.is_relative_to(base) or candidate.name.endswith(".part"):
        return None
    return candidate


def _record_state(company_dir: str, reg_dir: str) -> tuple[int | None, datetime | None]:
    """Status code and platform updateTime of a record, from the server's own
    latest snapshot of that company."""
    snap = get_latest_snapshot(company_dir) or {}
    for r in snap.get("records") or []:
        if _safe_name(str(r.get("dataRegNo") or "")) == reg_dir:
            ms = int_or_none(r.get("updateTime"))
            updated = datetime.fromtimestamp(ms / 1000, timezone.utc) if ms else None
            return int_or_none(r.get("dataRegAuditStatus")), updated
    return None, None


def _load_manifest(rec_dir: Path) -> dict:
    try:
        data = json.loads((rec_dir / _MANIFEST_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_manifest(rec_dir: Path, manifest: dict) -> None:
    atomic_write_text(rec_dir / _MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))


def _audit(action: str, rec_dir: Path, detail: dict) -> None:
    """One operation-log line per record. The only thing in this system that
    deletes notices from the server, so the console's 操作日志 shows it next
    to everything people did there. The laptop's address comes from sshd."""
    company_dir, reg_dir = rec_dir.relative_to(_ATTACHMENTS_DIR.resolve()).parts
    audit.record(
        "本机归档", action, via="ssh",
        ip=(os.environ.get("SSH_CLIENT") or "").split(" ")[0],
        company=company_dir, target=reg_dir, detail=detail,
    )


def release(items: list, *, dry_run: bool, days: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    released: list[str] = []
    skipped: list[dict] = []
    total = 0
    by_record: dict[Path, list[tuple[Path, str, int]]] = {}

    for item in items:
        rel = str(item.get("path") or "") if isinstance(item, dict) else ""
        want = str(item.get("sha256") or "") if isinstance(item, dict) else ""
        path = _resolve(rel, 3)
        if path is None:
            skipped.append({"path": rel, "reason": "路径不合法"})
            continue
        if not path.is_file():
            skipped.append({"path": rel, "reason": "服务器上没有这个文件"})
            continue
        company_dir, reg_dir, name = Path(rel).parts
        status, updated = _record_state(company_dir, reg_dir)
        if not archive_policy.releasable(name, status, updated, days=days, now=now):
            skipped.append({"path": rel, "reason": "按服务器快照不满足释放条件"})
            continue
        digest = _sha256(path)
        if digest != want:
            skipped.append({"path": rel, "reason": "哈希与本机副本不一致"})
            continue
        size = path.stat().st_size
        released.append(rel)
        total += size
        by_record.setdefault(path.parent, []).append((path, digest, size))

    if not dry_run:
        stamp = now.isoformat(timespec="seconds")
        for rec_dir, entries in by_record.items():
            manifest = _load_manifest(rec_dir)
            archived = dict(manifest.get("archived") or {})
            for path, digest, size in entries:
                archived[path.name] = {"sha256": digest, "size": size, "archived_at": stamp}
            manifest["archived"] = archived
            manifest["files"] = [f for f in manifest.get("files") or [] if f not in archived]
            _write_manifest(rec_dir, manifest)
            for path, _, size in entries:
                path.unlink(missing_ok=True)
                logger.info(
                    f"归档释放 {path.relative_to(_ATTACHMENTS_DIR.resolve())}（{size / 1048576:.2f}MB）"
                )
            _audit("archive.release", rec_dir, {
                "files": len(entries), "bytes": sum(size for _, _, size in entries),
                "names": [path.name for path, _, _ in entries],
            })
    return {"released": released, "skipped": skipped, "bytes": total, "dry_run": dry_run}


def unarchive(rel_dir: str) -> dict:
    """Forget the archived marks of files that are back on disk."""
    rec_dir = _resolve(rel_dir, 2)
    if rec_dir is None or not rec_dir.is_dir():
        raise ValueError(f"不是一个附件记录目录: {rel_dir!r}")
    manifest = _load_manifest(rec_dir)
    archived = dict(manifest.get("archived") or {})
    restored = [name for name in sorted(archived) if (rec_dir / name).is_file()]
    if restored:
        files = list(manifest.get("files") or [])
        for name in restored:
            archived.pop(name)
            if name not in files:
                files.append(name)
        manifest["files"] = files
        if archived:
            manifest["archived"] = archived
        else:
            manifest.pop("archived", None)
        _write_manifest(rec_dir, manifest)
        logger.info(f"取消归档标记 {rel_dir}: {', '.join(restored)}")
        _audit("archive.unarchive", rec_dir, {"files": len(restored), "names": restored})
    return {"restored": restored, "still_archived": sorted(archived)}


def main(argv: list[str] | None = None) -> int:
    setup_logging("cli")
    parser = argparse.ArgumentParser(description="释放已归档到本机的附件（由 python -m archive 调用）")
    parser.add_argument("--dry-run", action="store_true", help="只核对，不删除")
    parser.add_argument("--unarchive", metavar="企业目录/登记号", help="文件已送回后，清掉它们的归档标记")
    args = parser.parse_args(argv)

    ctx = control.try_acquire(kind="archive_release", trigger="cli")
    if ctx is None:
        print(json.dumps({"error": "另一巡检进行中，稍后再试"}, ensure_ascii=False))
        return 2
    status, error = "done", None
    try:
        if args.unarchive:
            result = unarchive(args.unarchive)
        else:
            items = json.load(sys.stdin)
            if not isinstance(items, list):
                raise ValueError("stdin 应为 JSON 数组")
            days = archive_settings().release_after_terminal_days
            result = release(items, dry_run=args.dry_run, days=days)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 — reported as JSON to the laptop
        status, error = "error", f"{type(exc).__name__}: {exc}"
        print(json.dumps({"error": error}, ensure_ascii=False))
        return 1
    finally:
        ctx.finish(status, error=error)


if __name__ == "__main__":
    sys.exit(main())
