"""Read and write config/settings.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from src.fsutil import atomic_write_text
from src.settings_cache import get_settings

_SETTINGS = Path(__file__).parent.parent.parent / "config" / "settings.yaml"


def load_settings() -> dict:
    return get_settings()


def save_settings(data: dict) -> None:
    atomic_write_text(
        _SETTINGS,
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
    )
