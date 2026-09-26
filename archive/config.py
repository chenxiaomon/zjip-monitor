"""archive/archive.env → ArchiveConfig.

Same shape as deploy/deploy.env (KEY=VALUE, # comments), but the file is only
read, never sourced: nothing in it is executed.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILE = Path(__file__).parent / "archive.env"

_DEFAULTS = {
    "SERVER": "root@104.249.172.32",
    "REMOTE": "/opt/zjip-monitor",
    "ARCHIVE_DIR": "~/zjip-archive",
    "SSH_CMD": "ssh",
}


@dataclass(frozen=True)
class ArchiveConfig:
    server: str
    remote: str
    archive_dir: Path
    ssh_cmd: str

    @property
    def db_path(self) -> Path:
        return self.archive_dir / "archive.sqlite"


def _parse_env(text: str) -> dict[str, str]:
    """The KEY=VALUE subset of shell syntax this file uses: comments, quotes,
    an optional `export`. Lines that do not parse are ignored."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        try:
            parts = shlex.split(raw, comments=True)
        except ValueError:
            continue
        values[key] = " ".join(parts)
    return values


def load_config(env_file: Path | None = None) -> ArchiveConfig:
    """Defaults, overridden by the env file (ZJIP_ARCHIVE_ENV picks another one)."""
    path = Path(env_file or os.environ.get("ZJIP_ARCHIVE_ENV") or DEFAULT_ENV_FILE)
    values = dict(_DEFAULTS)
    if path.is_file():
        parsed = _parse_env(path.read_text(encoding="utf-8"))
        values.update({k: v for k, v in parsed.items() if k in _DEFAULTS and v})
    return ArchiveConfig(
        server=values["SERVER"],
        remote=values["REMOTE"].rstrip("/"),
        archive_dir=Path(values["ARCHIVE_DIR"]).expanduser(),
        ssh_cmd=values["SSH_CMD"],
    )
