"""Read and write config/settings.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

_SETTINGS = Path(__file__).parent.parent.parent / "config" / "settings.yaml"


def load_settings() -> dict:
    if not _SETTINGS.exists():
        return {}
    return yaml.safe_load(_SETTINGS.read_text(encoding="utf-8")) or {}


def save_settings(data: dict) -> None:
    _SETTINGS.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
