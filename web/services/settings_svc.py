"""Read and write config/settings.yaml.

Display code reads through load_settings() (lenient, cached: a broken file
reads as {} and the page still renders). Every console SAVE is a
read-modify-write of the whole file, so it must not start from that lenient
view: a file that exists but cannot be parsed would be replaced by a file
holding only the section being saved, losing the notify secrets, the groups
and the schedule. Saves therefore go through editing(), which reads strictly
and refuses, and holds one lock so two tabs saving at once cannot drop each
other's change (the web app is a single process; the daemon never writes
this file).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import yaml

from src.fsutil import atomic_write_text
from src.settings_cache import get_settings, invalidate

_SETTINGS = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
_LOCK = threading.RLock()

REFUSED_HINT = "为免覆盖其余配置（通知密钥、分组、巡检计划），本次没有保存。请先在服务器上修好该文件再保存。"


class SettingsUnreadable(Exception):
    """settings.yaml exists but cannot be read or parsed."""


def load_settings() -> dict:
    return get_settings()


def load_for_update() -> dict:
    """The file's current content for a read-modify-write; raises
    SettingsUnreadable instead of pretending a broken file is empty."""
    try:
        text = _SETTINGS.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SettingsUnreadable(f"读不了 config/settings.yaml（{exc.strerror or exc}）") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SettingsUnreadable("config/settings.yaml 有格式错误") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SettingsUnreadable("config/settings.yaml 的最外层不是键值表")
    return data


@contextmanager
def editing() -> Iterator[dict]:
    """with editing() as data: change data, then call save_settings(data).
    Returning early without saving is fine; only one editor runs at a time."""
    with _LOCK:
        yield load_for_update()


def section(data: dict, name: str) -> dict:
    """data[name] as a dict; a missing, null or non-mapping value becomes {}."""
    value = data.get(name)
    if not isinstance(value, dict):
        value = data[name] = {}
    return value


def save_settings(data: dict) -> None:
    with _LOCK:
        try:
            mode = _SETTINGS.stat().st_mode & 0o777  # keep a chmod 600: the file holds secrets
        except FileNotFoundError:
            mode = None
        atomic_write_text(
            _SETTINGS,
            yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
            mode=mode,
        )
        # The page re-reads the file to render what was saved; don't make that
        # depend on the filesystem's mtime granularity.
        invalidate()
