"""Download 通知书 (notices) and 证书 (certificates) for registration records.

Endpoints (verified against the platform frontend bundle, 2026-07):
- GET  /v1/datareg/getNotification?dataRegNo=X
    → data: [{"dataMailType": "补正通知书", "dataMailFileUrl": "https://..."}, ...]
- POST /v1/datareg/confirmation?dataRegNo=X
    → data: {"confirmationType": ...,
             "systemConfirmation":   {"confirmationUrl": "https://..."},   # 浙江省证书
             "nationalConfirmation": {"confirmationUrl": "https://..."}}   # 国家局证书

Files are stored under data/attachments/{company}/{dataRegNo}/ with a .sync.json
manifest per record recording the audit status at last sync; a record is re-synced
only when its status changes or the platform has updated the record since (its
updateTime), so each record costs one API round-trip per change on the platform.

getNotification lists only the record's *current* notice. A second 补正 round
replaces the first on the platform but arrives under the same file name here, so
the older file is kept under a dated name and the current one takes the plain name.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests
from loguru import logger

from . import diskstat
from .exceptions import TokenExpiredError
from .fsutil import atomic_write_text
from .runtime_settings import attachment_settings
from .scraper import _build_session, _load_settings, int_or_none

if TYPE_CHECKING:
    from .control import CancelToken

_ROOT = Path(__file__).parent.parent
_ATTACHMENTS_DIR = _ROOT / "data" / "attachments"

# 通知书 exists once a record leaves 待审核; query for any post-review status.
# (待补正2/公示中3/异议4/不予登记6/登记成功7/终止登记8/登记撤销9/续展登记13/待发证17)
# getNotification returns [] for records without notices, so being inclusive only
# costs one deduplicated call per status transition and never misses a document.
NOTIFY_STATUSES = {2, 3, 4, 6, 7, 8, 9, 13, 17}
# 证书 only for 登记成功 / 续展登记 (frontend enables the 证书 button for [7, 13])
CERT_STATUSES = {7, 13}

_MANIFEST_NAME = ".sync.json"

# Archived file names carry the day the operator's side of the world saw them.
_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("._") or "file"


def _url_ext(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix and len(suffix) <= 6 else ".pdf"


def _download_file(
    url: str, dest: Path, timeout: float | None = None, max_bytes: int | None = None
) -> bool:
    """Download a (typically OSS) file URL to dest. Plain request — the file
    URLs are pre-signed; never send our platform token to a third-party host.

    Guarantees no `.part` file survives a failure (a mid-stream disconnect used
    to leave one behind with nothing to clean it up), and refuses a body larger
    than *max_bytes* — these are notice/certificate PDFs, and the disk they
    land on has been as full as 96%. Both limits default to settings.yaml →
    attachments.download_timeout_seconds / max_file_mb.
    """
    if timeout is None or max_bytes is None:
        cfg = attachment_settings()
        if timeout is None:
            timeout = cfg.download_timeout_seconds
        if max_bytes is None:
            max_bytes = cfg.max_file_bytes
    tmp: Path | None = None
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        if not resp.ok:
            logger.warning(f"Attachment download HTTP {resp.status_code}: {url[:100]}")
            return False
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            logger.warning(
                f"Attachment refused: Content-Length {int(declared) / 1e6:.1f} MB exceeds "
                f"{max_bytes / 1e6:.0f} MB — {url[:100]}"
            )
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        written = 0
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                written += len(chunk)
                if written > max_bytes:
                    logger.warning(
                        f"Attachment aborted: body exceeded {max_bytes / 1e6:.0f} MB "
                        f"— {url[:100]}"
                    )
                    return False
                f.write(chunk)
        tmp.replace(dest)
        tmp = None  # renamed away; nothing to clean up
        return True
    except (requests.RequestException, OSError) as exc:
        # OSError covers ENOSPC: a full disk must fail this one file, not the
        # whole account's attachment sync.
        logger.warning(f"Attachment download failed: {exc} — {url[:100]}")
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _api_data(resp: requests.Response, what: str) -> dict | list | None:
    if resp.status_code == 401:
        raise TokenExpiredError(f"{what}: HTTP 401 — token expired during attachment sync")
    if not resp.ok:
        logger.warning(f"{what} HTTP {resp.status_code}: {resp.text[:150]}")
        return None
    try:
        payload = resp.json()
    except ValueError:
        logger.warning(f"{what} non-JSON response: {resp.text[:150]}")
        return None
    if payload.get("code") == 401:
        raise TokenExpiredError(f"{what}: code=401 — token expired during attachment sync")
    if payload.get("code") not in (200, 0):
        logger.warning(f"{what} API code={payload.get('code')}: {payload.get('message', '')[:100]}")
        return None
    return payload.get("data")


def _fetch_notifications(session: requests.Session, base_url: str, reg_no: str) -> list[dict]:
    """Return [{type, url, id}, ...] for a record's 通知书 list."""
    resp = session.get(
        f"{base_url}/v1/datareg/getNotification",
        params={"dataRegNo": reg_no},
        timeout=attachment_settings().request_timeout_seconds,
    )
    data = _api_data(resp, f"getNotification[{reg_no}]")
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        url = item.get("dataMailFileUrl")
        if url:
            out.append({
                "type": item.get("dataMailType") or "通知书",
                "url": url,
                "id": item.get("id"),
            })
    return out


def _fetch_certificates(session: requests.Session, base_url: str, reg_no: str) -> list[dict]:
    """Return [{type, url}, ...] for a record's 证书 (省 + 国家局)."""
    resp = session.post(
        f"{base_url}/v1/datareg/confirmation",
        params={"dataRegNo": reg_no},
        json={},
        timeout=attachment_settings().request_timeout_seconds,
    )
    data = _api_data(resp, f"confirmation[{reg_no}]")
    if not isinstance(data, dict):
        return []
    out = []
    for key, label in (
        ("systemConfirmation", "浙江省登记证书"),
        ("nationalConfirmation", "国家局登记证书"),
    ):
        url = (data.get(key) or {}).get("confirmationUrl")
        if url:
            out.append({"type": label, "url": url})
    return out


def _load_manifest(rec_dir: Path) -> dict | None:
    manifest = rec_dir / _MANIFEST_NAME
    if not manifest.exists():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None


def _touched_since(record: dict, synced_at: str | None) -> bool:
    """Has the platform updated this record after our last successful sync?

    Status alone cannot tell: a record that went 待补正 → 待审核 → 待补正 still
    matches a manifest that says 2 (待审核 is not a notify status, so nothing
    rewrote it), and the new round's notice was never fetched — seen 2026-09,
    when the console kept serving an 08-31 notice whose deadline had passed
    while the live one said 10-12. updateTime moves on every such transition,
    including one that starts and ends between two scans.
    """
    updated_ms = int_or_none(record.get("updateTime"))
    if updated_ms is None or not synced_at:
        return False
    try:
        synced = datetime.fromisoformat(synced_at)
    except ValueError:
        return False
    return updated_ms / 1000 > synced.timestamp()


def _identity(item: dict) -> str:
    """What makes two platform documents the same one: the notice id when the
    API gives one, else the URL path (links may be pre-signed, so the query
    string can differ on every call for the very same file)."""
    if item.get("id") is not None:
        return f"id:{item['id']}"
    return "path:" + urlparse(item["url"]).path


def _archive_name(dest: Path) -> Path:
    """Where the older document moves when a newer one of the same type
    arrives: same stem plus the day it was downloaded. Sorted by name, the
    console then lists the current document first and older rounds after it."""
    day = datetime.fromtimestamp(dest.stat().st_mtime, _SHANGHAI_TZ).strftime("%Y-%m-%d")
    candidate = dest.with_name(f"{dest.stem}_{day}{dest.suffix}")
    n = 2
    while candidate.exists():
        candidate = dest.with_name(f"{dest.stem}_{day}_{n}{dest.suffix}")
        n += 1
    return candidate


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_unless_archived(url: str, dest: Path, archived_sha: str | None) -> tuple[bool, bool]:
    """dest was released to the laptop's archive (scripts/archive_release.py)
    before its identity was recorded. Fetch url beside it and keep it only if
    it is not that same document. Returns (ok, kept)."""
    incoming = dest.with_name(f".{dest.name}.incoming.part")
    try:
        if not _download_file(url, incoming):
            return False, False
        if archived_sha and _sha256(incoming) == archived_sha:
            return True, False
        incoming.replace(dest)
        return True, True
    finally:
        incoming.unlink(missing_ok=True)


def _refresh_document(url: str, dest: Path) -> tuple[bool, Path | None]:
    """dest already holds a document of this type; fetch url and keep both if
    they differ. Returns (ok, archived): ok is False when the download failed
    (dest untouched), archived is where the old file went, or None when the
    platform served the very same bytes.

    The incoming copy ends in .part so it stays hidden from the console and
    main._prune_stale_parts() sweeps it if the process dies mid-swap.
    """
    incoming = dest.with_name(f".{dest.name}.incoming.part")
    try:
        if not _download_file(url, incoming):
            return False, None
        if incoming.stat().st_size == dest.stat().st_size and incoming.read_bytes() == dest.read_bytes():
            return True, None
        archived = _archive_name(dest)
        dest.replace(archived)
        incoming.replace(dest)
        return True, archived
    finally:
        incoming.unlink(missing_ok=True)


def sync_attachments(
    token: str,
    company: str,
    records: list[dict],
    base_url: str | None = None,
    cancel: "CancelToken | None" = None,
) -> list[Path]:
    """Download missing 通知书/证书 for all eligible records of one account.

    Idempotent: a record is only re-queried when its audit status differs from
    the manifest written at last successful sync, or the platform updated the
    record after that sync (a new 补正 round keeps the status at 2). A document
    already on disk under the same identity is never downloaded twice.

    Disk guard: nothing is downloaded while free space is under settings.yaml
    → disk.min_free_mb — checked once before opening a session and again
    before every file, since a big first sync can cross the floor mid-way.
    A record interrupted by the guard keeps no manifest, so it is retried.

    Returns:
        List of newly downloaded file paths.
    """
    settings = _load_settings()
    att_cfg = settings.get("attachments", {})
    if not att_cfg.get("enabled", True):
        return []
    disk = diskstat.check_free()
    if disk.low:
        logger.warning(
            f"[{company}] 磁盘剩余 {disk.free_mb:.0f}MB 低于 "
            f"disk.min_free_mb={disk.min_free_mb:g}MB — 本轮跳过附件下载"
        )
        return []
    # Separate switch for the certificate lookup: it is the one platform call
    # whose path carries a write-sounding name ("confirmation"). It is a query
    # (empty body, returns URLs), but this lets operators turn just it off.
    certificates_enabled = att_cfg.get("certificates_enabled", True)
    if base_url is None:
        base_url = settings.get("base_url", "https://www.zjip.org.cn")
    crawl_delay = attachment_settings().crawl_delay_seconds

    session = _build_session(token)
    downloaded: list[Path] = []
    low_disk = False  # flips once, ends the account's sync, warns once
    try:
        for r in records:
            if low_disk:
                break
            if cancel:
                cancel.checkpoint()
            reg_no = r.get("dataRegNo")
            # int() on a non-numeric status used to raise straight out of this
            # loop, so one malformed record silently skipped every remaining
            # attachment for the account (caught by main.py's broad handler as
            # a single "non-fatal" warning).
            code = int_or_none(r.get("dataRegAuditStatus"))
            if not reg_no or code is None or code not in NOTIFY_STATUSES:
                continue

            rec_dir = _ATTACHMENTS_DIR / _safe_name(company) / _safe_name(reg_no)
            manifest = _load_manifest(rec_dir)
            if (
                manifest
                and manifest.get("status") == code
                and not _touched_since(r, manifest.get("synced_at"))
            ):
                continue  # synced at this status, nothing moved on the platform since

            wanted = _fetch_notifications(session, base_url, reg_no)
            if code in CERT_STATUSES and certificates_enabled:
                wanted += _fetch_certificates(session, base_url, reg_no)

            ok = True
            files: list[str] = manifest.get("files", []) if manifest else []
            # file name → _identity() of the document stored under it
            sources: dict[str, str] = dict(manifest.get("sources") or {}) if manifest else {}
            # file name → {sha256, size, archived_at}: released to the laptop's
            # archive by scripts/archive_release.py, gone from this disk on purpose
            archived: dict[str, dict] = dict(manifest.get("archived") or {}) if manifest else {}
            for item in wanted:
                fname = _safe_filename(item["type"]) + _url_ext(item["url"])
                dest = rec_dir / fname
                ident = _identity(item)
                held = next((name for name, i in sources.items() if i == ident), None)
                if held and ((rec_dir / held).exists() or held in archived):
                    continue  # this very document is on disk, or safe in the local archive
                disk = diskstat.check_free()
                if disk.low:
                    logger.warning(
                        f"[{company}] 磁盘剩余 {disk.free_mb:.0f}MB 低于 "
                        f"disk.min_free_mb={disk.min_free_mb:g}MB — 停止本账号后续附件下载，"
                        "未完成的记录下轮重试"
                    )
                    low_disk = True
                    ok = False  # keep this record un-manifested → retried next run
                    break
                if not dest.exists() and fname in archived:
                    fetched, kept = _fetch_unless_archived(
                        item["url"], dest, archived[fname].get("sha256")
                    )
                    if not fetched:
                        ok = False
                        continue
                    sources[fname] = ident
                    if kept:
                        # The platform changed it after the release: the new
                        # version is current, the old one lives in the archive.
                        archived.pop(fname)
                        downloaded.append(dest)
                        if fname not in files:
                            files.append(fname)
                        logger.info(f"[{company}] {reg_no} ← {fname}（平台已更新，旧版在本机归档）")
                    continue
                if not dest.exists():
                    if _download_file(item["url"], dest):
                        downloaded.append(dest)
                        sources[fname] = ident
                        if fname not in files:
                            files.append(fname)
                        logger.info(f"[{company}] {reg_no} ← {fname}")
                    else:
                        ok = False
                    continue
                # A document of this type is already here: either this same one,
                # stored before identities were recorded, or an older round —
                # a second 补正通知书 has the same type, hence the same name.
                fetched, moved = _refresh_document(item["url"], dest)
                if not fetched:
                    ok = False
                    continue
                if moved is not None:
                    if fname in sources:
                        sources[moved.name] = sources[fname]
                    if moved.name not in files:
                        files.append(moved.name)
                    downloaded.append(dest)
                    logger.info(
                        f"[{company}] {reg_no} ← {fname}（新一轮，旧文件改名为 {moved.name}）"
                    )
                sources[fname] = ident

            # Only record the status when everything succeeded, so failures
            # are retried on the next run
            if ok:
                rec_dir.mkdir(parents=True, exist_ok=True)
                # Atomic like every other persisted file in the project: a
                # half-written manifest reads back as "never synced" (the
                # loader swallows the error), so the record would re-request
                # its documents from the platform on every single run.
                atomic_write_text(
                    rec_dir / _MANIFEST_NAME,
                    json.dumps(
                        {
                            "status": code,
                            "synced_at": datetime.now(timezone.utc).isoformat(),
                            "files": files,
                            "sources": sources,
                            **({"archived": archived} if archived else {}),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            if cancel:
                cancel.wait(crawl_delay)  # polite crawl delay, cancellable
            else:
                time.sleep(crawl_delay)
    finally:
        session.close()

    return downloaded
