"""Change-keyed loader for config/settings.yaml, shared by all modules.

One scan cycle used to re-read and re-parse the YAML dozens of times
(login, scraper, notifier, attachments, web deps). Caching on the file's
(st_mtime_ns, st_size) keeps "edit the file → takes effect immediately"
semantics at zero read cost.
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path

import yaml
from loguru import logger

_ROOT = Path(__file__).parent.parent
_SETTINGS_FILE = _ROOT / "config" / "settings.yaml"

# How many times to re-read when the file changes under us (see get_settings).
_MAX_READ_ATTEMPTS = 3

_lock = threading.Lock()
# ((st_mtime_ns, st_size), parsed) — `None` means "nothing cached", and
# tests reset it that way, so keep both the name and that meaning.
_cache: tuple[tuple[int, int], dict] | None = None


def _file_key(path: Path) -> tuple[int, int]:
    """Identity of the file's current content. mtime alone (as a float) is
    not enough: two writes within one filesystem tick — or on a coarse-mtime
    filesystem — keep the same mtime, and float rounding can even collapse
    distinct nanosecond stamps. Size catches most same-tick edits."""
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def get_settings() -> dict:
    global _cache
    try:
        key = _file_key(_SETTINGS_FILE)
    except OSError:
        return {}

    with _lock:
        if _cache is not None and _cache[0] == key:
            # Deep copy: callers (settings page) mutate the dict before saving;
            # the parse is what's expensive, copying a small dict is free.
            return copy.deepcopy(_cache[1])

    # stat → read → stat. A writer replacing the file between our stat and
    # our read would otherwise file the NEW text under the OLD key: the key
    # then already matches on every later call and the change is invisible
    # until the next edit. Only a read bracketed by two identical keys is
    # trusted enough to cache.
    text = ""
    stable = False
    for _ in range(_MAX_READ_ATTEMPTS):
        try:
            text = _SETTINGS_FILE.read_text(encoding="utf-8")
            after = _file_key(_SETTINGS_FILE)
        except OSError:
            return {}
        if after == key:
            stable = True
            break
        key = after

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        logger.warning(f"settings.yaml 解析失败（{exc}）— 使用默认配置")
        data = {}

    if stable:
        # A broken file is cached as {} too, so the warning above fires once
        # per edit rather than once per request.
        with _lock:
            _cache = (key, data)
    return copy.deepcopy(data)
