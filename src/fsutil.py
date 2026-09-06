"""Atomic file-write helpers (tmp file + os.replace)."""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path


def _tmp_for(path: Path) -> Path:
    """Unique temp name per call. pid alone is NOT enough: the concurrent scan
    path has several threads heart-beating state.json at the same instant, and
    a shared `.state.json.<pid>.tmp` let one thread's os.replace() pull the
    file out from under another's (FileNotFoundError, seen 2026-08-30 …
    2026-09-06, randomly failing 1-3 accounts at the start of each run)."""
    return path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text so readers never observe a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes so readers never observe a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
