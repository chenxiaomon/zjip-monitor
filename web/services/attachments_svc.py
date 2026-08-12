"""Read downloaded 通知书/证书 files from data/attachments/."""

from __future__ import annotations

import re
from pathlib import Path

from web.services.snapshot import get_latest_snapshot

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


def download_name(company: str, reg_no: str, filename: str) -> str:
    """浏览器下载名: {数据名称}_{登记号}_{原文件名主干}.{后缀}.

    磁盘上附件按证书/通知书类型命名(如 浙江省登记证书.pdf),不同记录重名;
    下载时反查最新快照拼上数据名称与登记号,保证跨记录唯一。查不到记录时
    降级为 {登记号}_{原文件名},仍然唯一。
    """
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    data_name = ""
    real_reg_no = reg_no
    snap = get_latest_snapshot(company)
    if snap:
        for r in snap.get("records", []):
            no = r.get("dataRegNo", "")
            if no and _safe_name(no) == reg_no:
                data_name = r.get("dataRegName", "") or ""
                real_reg_no = no
                break
    base = "_".join(p for p in (data_name, real_reg_no, stem) if p)
    base = re.sub(r'[\\/:*?"<>|\s]+', "_", base).strip("._") or "file"
    return base + (f".{ext}" if ext else "")


def attachment_path(company: str, reg_no: str, filename: str) -> Path | None:
    """Resolve one attachment file, guarding against path traversal."""
    base = _ATTACHMENTS_DIR.resolve()
    candidate = (base / _safe_name(company) / _safe_name(reg_no) / filename).resolve()
    if not candidate.is_relative_to(base) or not candidate.is_file() or candidate.name.startswith("."):
        return None
    return candidate
