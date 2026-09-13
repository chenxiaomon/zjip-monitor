"""Disk facts for the disk guard: free space, per-directory usage, disk_stats.json.

The production box is a 9.6 GB root partition that has run at 96% full, and
data/attachments/ is the one tree this tool grows without bound (no file is
ever deleted automatically — notices and certificates are legal documents).
So the policy is measure + warn + stop adding:

    check_free()   → attachments.sync_attachments skips downloads when free
                     space is under settings.yaml → disk.min_free_mb
    write_stats()  → main._prune_data() drops a disk_stats.json snapshot at
                     the end of every run for the console banner
    read_stats()   → the web process reads that snapshot (stat-cached)

Every path is a module constant so tests can point the whole module at a
temp tree; nothing here ever touches the platform.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from .fsutil import atomic_write_text
from .runtime_settings import attachment_settings, disk_settings

_ROOT = Path(__file__).parent.parent
_DATA_DIR = _ROOT / "data"
_ATTACHMENTS_DIR = _DATA_DIR / "attachments"
_SNAPSHOTS_DIR = _DATA_DIR / "snapshots"
_REPORTS_DIR = _DATA_DIR / "reports"
_EVENTS_FILE = _DATA_DIR / "events.jsonl"
_LOGS_DIR = _ROOT / "logs"
_STATS_FILE = _DATA_DIR / "control" / "disk_stats.json"

_MIB = 1024 * 1024


@dataclass(frozen=True)
class FreeSpace:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    free_mb: float
    percent_used: float


@dataclass(frozen=True)
class DiskCheck:
    low: bool
    free_mb: float
    min_free_mb: float


@dataclass(frozen=True)
class DirStats:
    bytes: int
    files: int


# ---------------------------------------------------------------------------
# Free space
# ---------------------------------------------------------------------------

def _nearest_existing(path: Path) -> Path:
    """statvfs needs a path that exists; data/ subdirs are created lazily."""
    p = Path(path)
    while not p.exists():
        parent = p.parent
        if parent == p:
            break
        p = parent
    return p


def _guard_path() -> Path:
    """Where the guard measures: the data tree's partition (or the repo's
    until data/ exists — same partition in every deployment so far)."""
    return _DATA_DIR if _DATA_DIR.exists() else _ROOT


def free_space(path: Path | str) -> FreeSpace:
    usage = shutil.disk_usage(_nearest_existing(Path(path)))
    # `df` reports Use% as used / (used + avail), not used / total: ext4
    # reserves ~5% for root, so the two differ by several points and the
    # operator compares this number with what df prints on the server.
    denominator = usage.used + usage.free
    percent = round(usage.used / denominator * 100, 1) if denominator else 0.0
    return FreeSpace(
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        free_mb=usage.free / _MIB,
        percent_used=percent,
    )


def check_free(min_free_mb: float | None = None, path: Path | str | None = None) -> DiskCheck:
    """Is free space under the floor? Cheap (one statvfs), call it often."""
    if min_free_mb is None:
        min_free_mb = disk_settings().min_free_mb
    if path is None:
        path = _guard_path()
    free_mb = free_space(path).free_mb
    return DiskCheck(low=free_mb < min_free_mb, free_mb=free_mb, min_free_mb=min_free_mb)


# ---------------------------------------------------------------------------
# Directory usage
# ---------------------------------------------------------------------------

def dir_stats(path: Path | str) -> DirStats:
    """Recursive size/count of regular files under *path*.

    Symlinks are neither followed nor counted (a link into another tree
    would double-count or escape data/). Files that vanish between listing
    and stat are skipped: pruning runs on the same tree right before this.
    """
    total = 0
    files = 0
    stack = [Path(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except (PermissionError, FileNotFoundError):
                        continue
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue
    return DirStats(bytes=total, files=files)


def _files_stats(paths: Iterable[Path]) -> DirStats:
    total = 0
    files = 0
    for p in paths:
        try:
            st = p.stat()
        except (PermissionError, FileNotFoundError):
            continue
        total += st.st_size
        files += 1
    return DirStats(bytes=total, files=files)


def _events_stats() -> DirStats:
    """events.jsonl plus its rotations (main._prune_data names them
    events-<stamp>.jsonl next to the live file)."""
    return _files_stats([_EVENTS_FILE, *_EVENTS_FILE.parent.glob("events-*.jsonl")])


# ---------------------------------------------------------------------------
# disk_stats.json
# ---------------------------------------------------------------------------

def collect() -> dict[str, Any]:
    """One measurement of everything the console banner needs."""
    root = free_space(_guard_path())
    attachments = dir_stats(_ATTACHMENTS_DIR)
    disk = disk_settings()
    att = attachment_settings()
    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "root": asdict(root),
        "dirs": {
            "attachments": asdict(attachments),
            "snapshots": asdict(dir_stats(_SNAPSHOTS_DIR)),
            "reports": asdict(dir_stats(_REPORTS_DIR)),
            "events": asdict(_events_stats()),
            "logs": asdict(dir_stats(_LOGS_DIR)),
        },
        # The thresholds travel with the flags so a reader of the file can
        # tell what "over" meant when it was written.
        "attachments_over_limit": attachments.bytes > att.max_total_bytes,
        "attachments_max_total_bytes": att.max_total_bytes,
        "low_disk": root.free_mb < disk.min_free_mb,
        "min_free_mb": disk.min_free_mb,
    }


_read_lock = threading.Lock()
_read_cache: tuple[tuple[int, int], dict[str, Any]] | None = None  # ((mtime_ns, size), parsed)


def write_stats() -> dict[str, Any] | None:
    """collect() → data/control/disk_stats.json. Never raises: this runs in
    the run's finalizer, ahead of the lock release, and a full disk is the
    very situation it reports on (the write itself may hit ENOSPC).

    Returns what was collected, even if writing it failed; None if not even
    the measurement succeeded.
    """
    global _read_cache
    stats: dict[str, Any] | None = None
    try:
        stats = collect()
        if stats["low_disk"]:
            logger.warning(
                f"磁盘剩余 {stats['root']['free_mb']:.0f}MB 低于 "
                f"disk.min_free_mb={stats['min_free_mb']:g}MB — 附件下载已暂停，请清理磁盘"
            )
        if stats["attachments_over_limit"]:
            logger.warning(
                f"附件目录 {stats['dirs']['attachments']['bytes'] / _MIB:.0f}MB 超过 "
                f"attachments.max_total_mb={stats['attachments_max_total_bytes'] / _MIB:g}MB "
                "— 不会自动删除，请人工清理 data/attachments/"
            )
        atomic_write_text(_STATS_FILE, json.dumps(stats, ensure_ascii=False, indent=2))
        with _read_lock:
            _read_cache = None  # same-process readers must see this write immediately
    except Exception as exc:  # noqa: BLE001 — bookkeeping is never fatal
        logger.warning(f"disk_stats.json 写入失败（非致命）: {exc!r}")
    return stats


def read_stats() -> dict[str, Any] | None:
    """stat-cached read of disk_stats.json; None when missing or unreadable.

    Keyed on (mtime_ns, size) like status_store.get_all: the writer is the
    scan process, the reader the web process, and two writes within one
    mtime tick must not pin a stale view."""
    global _read_cache
    try:
        st = _STATS_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    with _read_lock:
        if _read_cache is not None and _read_cache[0] == key:
            return _read_cache[1]
    try:
        data = json.loads(_STATS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    with _read_lock:
        _read_cache = (key, data)
    return data
