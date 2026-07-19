"""Read downloaded 通知书/证书 files from data/attachments/."""

from __future__ import annotations

from pathlib import Path

_ATTACHMENTS_DIR = Path(__file__).parent.parent.parent / "data" / "attachments"


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def attachments_dir() -> Path:
    return _ATTACHMENTS_DIR


def list_attachments(company: str, reg_no: str) -> list[str]:
    """Filenames of downloaded attachments for one record (empty if none)."""
    rec_dir = _ATTACHMENTS_DIR / _safe_name(company) / _safe_name(reg_no)
    if not rec_dir.is_dir():
        return []
    return sorted(
        f.name for f in rec_dir.iterdir()
        if f.is_file() and not f.name.startswith(".")
    )


def attachment_path(company: str, reg_no: str, filename: str) -> Path | None:
    """Resolve one attachment file, guarding against path traversal."""
    base = _ATTACHMENTS_DIR.resolve()
    candidate = (base / _safe_name(company) / _safe_name(reg_no) / filename).resolve()
    if not candidate.is_relative_to(base) or not candidate.is_file() or candidate.name.startswith("."):
        return None
    return candidate
