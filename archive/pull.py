"""Pull the server's data/ into ARCHIVE_DIR — append-only.

No --delete, ever: the local copy is the long-term record, so what the server
prunes (snapshots beyond 30 per company, rotated event logs) stays here. A file
the server rewrote in place is first moved to .overwritten/<stamp>/ by rsync's
--backup-dir, so not even an overwrite costs the old bytes.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone

from .config import ArchiveConfig

_TRANSFERRED_RE = re.compile(r"Number of (?:regular )?files transferred: ([\d,]+)")


def plan(cfg: ArchiveConfig, *, reports: bool = False, stamp: str | None = None) -> list[list[str]]:
    """The rsync command lines, one per data directory."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    src = f"{cfg.server}:{cfg.remote}/data"
    dest = cfg.archive_dir

    def job(name: str, *extra: str, backup: bool = True, source: str | None = None) -> list[str]:
        argv = ["rsync", "-az", "--stats", "-e", cfg.ssh_cmd]
        if backup:
            argv += ["--backup", f"--backup-dir={dest / '.overwritten' / stamp / name}"]
        return argv + list(extra) + [source or f"{src}/{name}/", f"{dest / name}/"]

    jobs = [
        # .part = a download in flight on the server, not a document
        job("attachments", "--exclude=*.part"),
        job("snapshots"),
        # events.jsonl grows every run and index re-reads it whole; rotated
        # events-*.jsonl never change. No backup: each old copy is a prefix
        # of the next one.
        job("events", backup=False, source=f"{src}/events*.jsonl"),
    ]
    if reports:
        jobs.append(job("reports"))
    return jobs


def run(cfg: ArchiveConfig, *, reports: bool = False) -> int:
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    failed: list[str] = []
    for argv in plan(cfg, reports=reports):
        name = argv[-1].rstrip("/").rsplit("/", 1)[-1]
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            failed.append(name)
            print(f"✗ {name}: rsync 退出码 {proc.returncode}\n{proc.stderr.strip()}")
            continue
        m = _TRANSFERRED_RE.search(proc.stdout)
        count = int(m.group(1).replace(",", "")) if m else 0
        total += count
        print(f"✓ {name}: 传输 {count} 个文件")

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = "ok" if not failed else "failed: " + ",".join(failed)
    with (cfg.archive_dir / "pull.log").open("a", encoding="utf-8") as log:
        log.write(f"{stamp}\t{cfg.server}\ttransferred={total}\t{status}\n")
    return 1 if failed else 0
