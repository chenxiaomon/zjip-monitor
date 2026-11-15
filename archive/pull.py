"""Pull the server's data/ into ARCHIVE_DIR — append-only.

No --delete, ever: the local copy is the long-term record, so what the server
prunes (snapshots beyond 30 per company, rotated event logs) stays here. A file
the server rewrote in place is first moved to .overwritten/<stamp>/ by rsync's
--backup-dir, so not even an overwrite costs the old bytes.

Notices are what matters most — the platform only ever serves the current
one, while certificates can be issued again — so the scheduled run
(archive/schedule.py) pulls them in a pass of their own before everything
else: an SSH drop half-way through a large certificate pull costs no notice.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.archive_policy import CERTIFICATE_TYPES

from .config import ArchiveConfig

_TRANSFERRED_RE = re.compile(r"Number of (?:regular )?files transferred: ([\d,]+)")
# rsync 24: some source files vanished during the transfer — the server
# renamed or swept a file mid-pull; the next pull takes what it became.
_OK_CODES = (0, 24)
# Certificate files and their older dated rounds: 国家局登记证书.pdf, 国家局登记证书_2026-07-19.pdf
_CERT_EXCLUDES = tuple(f"--exclude={label}*" for label in CERTIFICATE_TYPES)


def plan(cfg: ArchiveConfig, *, reports: bool = False, stamp: str | None = None,
         notices_only: bool = False) -> list[list[str]]:
    """The rsync command lines, one per data directory — or, notices_only, the
    single attachments pass without certificates (manifests included)."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    src = f"{cfg.server}:{cfg.remote}/data"
    dest = cfg.archive_dir

    def job(name: str, *extra: str, backup: bool = True, source: str | None = None) -> list[str]:
        argv = ["rsync", "-az", "--stats", "-e", cfg.ssh_cmd]
        if backup:
            argv += ["--backup", f"--backup-dir={dest / '.overwritten' / stamp / name}"]
        return argv + list(extra) + [source or f"{src}/{name}/", f"{dest / name}/"]

    if notices_only:
        return [job("attachments", "--exclude=*.part", *_CERT_EXCLUDES)]
    jobs = [
        # .part = a download in flight on the server, not a document
        job("attachments", "--exclude=*.part"),
        job("snapshots"),
        # events.jsonl grows every run and index re-reads it whole; rotated
        # events-*.jsonl never change. No backup: each old copy is a prefix
        # of the next one.
        job("events", backup=False, source=f"{src}/events*.jsonl"),
        # The console's operation log: monthly files that only grow — the
        # same reasoning as events, so no backup either.
        job("audit", backup=False, source=f"{src}/control/audit/"),
    ]
    if reports:
        jobs.append(job("reports"))
    return jobs


@dataclass
class PullResult:
    transferred: int = 0
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "PullResult") -> "PullResult":
        return PullResult(self.transferred + other.transferred, self.failed + other.failed,
                          self.errors + other.errors)


def pull_jobs(jobs: list[list[str]], *, tag: str = "", retries: int = 0, waits: tuple[int, ...] = (60, 300),
              sleep: Callable[[float], None] = time.sleep) -> PullResult:
    """Run the rsync jobs in order, each retried up to `retries` times (a dropped
    SSH connection usually comes back within minutes); print one line each."""
    result = PullResult()
    for argv in jobs:
        name = argv[-1].rstrip("/").rsplit("/", 1)[-1] + tag
        for attempt in range(retries + 1):
            proc = subprocess.run(argv, capture_output=True, text=True)
            if proc.returncode in _OK_CODES:
                m = _TRANSFERRED_RE.search(proc.stdout)
                count = int(m.group(1).replace(",", "")) if m else 0
                result.transferred += count
                print(f"✓ {name}: 传输 {count} 个文件")
                break
            error = f"✗ {name}: rsync 退出码 {proc.returncode}\n{proc.stderr.strip()[-800:]}"
            print(error)
            if attempt < retries:
                wait = waits[min(attempt, len(waits) - 1)]
                print(f"  {wait} 秒后重试（第 {attempt + 1}/{retries} 次）")
                sleep(wait)
        else:
            result.failed.append(name)
            result.errors.append(error)
    return result


# --- which notices are new -------------------------------------------------------

def _is_certificate_file(name: str) -> bool:
    return name.startswith(CERTIFICATE_TYPES)


def notice_files(root: Path) -> dict[str, tuple[int, int]]:
    """{attachments/<企业>/<登记号>/<文件>: (size, mtime_ns)} for every notice
    here — every document that is not a certificate."""
    out: dict[str, tuple[int, int]] = {}
    for f in (root / "attachments").glob("*/*/*"):
        if f.name.startswith(".") or f.name.endswith(".part") or _is_certificate_file(f.name):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if f.is_file():
            out[f.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_notices(root: Path, before: dict[str, tuple[int, int]], db_path: Path) -> list[str]:
    """Notice files that arrived or changed since `before` and whose bytes the
    archive has never indexed. Run it before `index`: a 补正 round then counts
    once — the server keeps the older round under a dated name, and those
    bytes are already known."""
    known: set[str] = set()
    if db_path.exists():
        db = sqlite3.connect(db_path)
        try:
            known = {sha for (sha,) in db.execute("SELECT sha256 FROM documents WHERE sha256 IS NOT NULL")}
        except sqlite3.Error:
            known = set()
        finally:
            db.close()
    fresh = []
    for rel, stat in sorted(notice_files(root).items()):
        if before.get(rel) == stat:
            continue
        try:
            digest = _sha256(root / rel)
        except OSError:
            continue
        if digest not in known:
            known.add(digest)
            fresh.append(rel)
    return fresh


def log_line(cfg: ArchiveConfig, result: PullResult, *, notices: int | None = None, via: str | None = None) -> None:
    """One line per pull in pull.log: time, server, files, ok/failed, then the
    newer optional columns."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = "ok" if not result.failed else "failed: " + ",".join(result.failed)
    extra = (f"\tnotices={notices}" if notices is not None else "") + (f"\tvia={via}" if via else "")
    with (cfg.archive_dir / "pull.log").open("a", encoding="utf-8") as log:
        log.write(f"{stamp}\t{cfg.server}\ttransferred={result.transferred}\t{status}{extra}\n")


def run(cfg: ArchiveConfig, *, reports: bool = False) -> int:
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    before = notice_files(cfg.archive_dir)
    result = pull_jobs(plan(cfg, reports=reports))
    fresh = new_notices(cfg.archive_dir, before, cfg.db_path)
    if fresh:
        print(f"新通知书 {len(fresh)} 份（运行 python -m archive index 入库）")
    log_line(cfg, result, notices=len(fresh), via="manual")
    return 1 if result.failed else 0
