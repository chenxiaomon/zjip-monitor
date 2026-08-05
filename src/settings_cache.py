"""mtime-cached loader for config/settings.yaml, shared by all modules.

One scan cycle used to re-read and re-parse the YAML dozens of times
(login, scraper, notifier, attachments, web deps). Caching on mtime keeps
"edit the file → takes effect immediately" semantics at zero read cost.
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path

import yaml
from loguru import logger

_ROOT = Path(__file__).parent.parent
_SETTINGS_FILE = _ROOT / "config" / "settings.yaml"

_lock = threading.Lock()
_cache: tuple[float, dict] | None = None  # (mtime, parsed)


def get_settings() -> dict:
    global _cache
    try:
        mtime = _SETTINGS_FILE.stat().st_mtime
    except OSError:
        return {}

    with _lock:
        if _cache is not None and _cache[0] == mtime:
            # Deep copy: callers (settings page) mutate the dict before saving;
            # the parse is what's expensive, copying a small dict is free.
            return copy.deepcopy(_cache[1])

    try:
        data = yaml.safe_load(_SETTINGS_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning(f"settings.yaml 解析失败（{exc}）— 使用默认配置")
        data = {}
    except OSError:
        return {}

    with _lock:
        _cache = (mtime, data)
    return copy.deepcopy(data)
