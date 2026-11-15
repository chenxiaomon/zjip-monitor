"""One archive command at a time on this Mac.

The scheduled pull (archive/schedule.py) and a pull, index, release or restore
started by hand would otherwise rsync into, or rewrite archive.sqlite under,
each other. `bundle` only reads, so several may run together, but never while
a pull is half-way.

flock on ARCHIVE_DIR/.archive.lock: the kernel drops it when the process dies,
so a crash never leaves the archive locked. Take it once per command (in
archive/__main__.py): a second flock from the same process on a new file
handle would wait for itself.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

LOCK_NAME = ".archive.lock"


class ArchiveBusy(RuntimeError):
    """Another archive command holds the lock; the message says which."""


def _holder(path: Path) -> str:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        since = datetime.fromisoformat(info["since"]).astimezone().strftime("%m-%d %H:%M")
        return f"另一个归档命令正在运行：{info['what']}（进程 {info['pid']}，{since} 开始）"
    except (OSError, ValueError, KeyError, TypeError):
        return "另一个归档命令正在运行（可能是 bundle 在打包）"


@contextmanager
def archive_lock(archive_dir: Path, *, what: str, shared: bool = False, wait: float = 0.0,
                 poll: float = 5.0, sleep: Callable[[float], None] = time.sleep) -> Iterator[None]:
    """Hold the archive lock for the duration; exclusive unless shared. Waits
    up to `wait` seconds, then raises ArchiveBusy."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / LOCK_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    held = False
    try:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ArchiveBusy(_holder(path)) from None
                sleep(min(poll, left))
        held = True
        if not shared:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({
                "what": what, "pid": os.getpid(),
                "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }, ensure_ascii=False).encode("utf-8"))
        yield
    finally:
        if held and not shared:  # so a later "busy" never names a process long gone
            os.ftruncate(fd, 0)
        os.close(fd)  # closing the descriptor releases the lock
