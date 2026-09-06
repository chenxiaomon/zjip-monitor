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
only when its status changes, so each record costs at most one API round-trip per
status transition.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests
from loguru import logger

from .exceptions import TokenExpiredError
from .scraper import _build_session, _load_settings

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


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("._") or "file"


def _url_ext(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix and len(suffix) <= 6 else ".pdf"


def _download_file(url: str, dest: Path, timeout: int = 60) -> bool:
    """Download a (typically OSS) file URL to dest. Plain request — the file
    URLs are pre-signed; never send our platform token to a third-party host."""
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        if not resp.ok:
            logger.warning(f"Attachment download HTTP {resp.status_code}: {url[:100]}")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        tmp.replace(dest)
        return True
    except requests.RequestException as exc:
        logger.warning(f"Attachment download failed: {exc} — {url[:100]}")
        return False


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
    """Return [{type, url}, ...] for a record's 通知书 list."""
    resp = session.get(
        f"{base_url}/v1/datareg/getNotification",
        params={"dataRegNo": reg_no},
        timeout=30,
    )
    data = _api_data(resp, f"getNotification[{reg_no}]")
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        url = item.get("dataMailFileUrl")
        if url:
            out.append({"type": item.get("dataMailType") or "通知书", "url": url})
    return out


def _fetch_certificates(session: requests.Session, base_url: str, reg_no: str) -> list[dict]:
    """Return [{type, url}, ...] for a record's 证书 (省 + 国家局)."""
    resp = session.post(
        f"{base_url}/v1/datareg/confirmation",
        params={"dataRegNo": reg_no},
        json={},
        timeout=30,
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


def sync_attachments(
    token: str,
    company: str,
    records: list[dict],
    base_url: str | None = None,
    cancel: "CancelToken | None" = None,
) -> list[Path]:
    """Download missing 通知书/证书 for all eligible records of one account.

    Idempotent: each record is only re-queried when its audit status differs
    from the manifest written at last successful sync.

    Returns:
        List of newly downloaded file paths.
    """
    settings = _load_settings()
    att_cfg = settings.get("attachments", {})
    if not att_cfg.get("enabled", True):
        return []
    # Separate switch for the certificate lookup: it is the one platform call
    # whose path carries a write-sounding name ("confirmation"). It is a query
    # (empty body, returns URLs), but this lets operators turn just it off.
    certificates_enabled = att_cfg.get("certificates_enabled", True)
    if base_url is None:
        base_url = settings.get("base_url", "https://www.zjip.org.cn")

    session = _build_session(token)
    downloaded: list[Path] = []
    try:
        for r in records:
            if cancel:
                cancel.checkpoint()
            code = r.get("dataRegAuditStatus")
            reg_no = r.get("dataRegNo")
            if not reg_no or code is None or int(code) not in NOTIFY_STATUSES:
                continue
            code = int(code)

            rec_dir = _ATTACHMENTS_DIR / _safe_name(company) / _safe_name(reg_no)
            manifest = _load_manifest(rec_dir)
            if manifest and manifest.get("status") == code:
                continue  # already synced at this status

            wanted = _fetch_notifications(session, base_url, reg_no)
            if code in CERT_STATUSES and certificates_enabled:
                wanted += _fetch_certificates(session, base_url, reg_no)

            ok = True
            files: list[str] = manifest.get("files", []) if manifest else []
            for item in wanted:
                fname = _safe_filename(item["type"]) + _url_ext(item["url"])
                dest = rec_dir / fname
                if dest.exists():
                    continue
                if _download_file(item["url"], dest):
                    downloaded.append(dest)
                    if fname not in files:
                        files.append(fname)
                    logger.info(f"[{company}] {reg_no} ← {fname}")
                else:
                    ok = False

            # Only record the status when everything succeeded, so failures
            # are retried on the next run
            if ok:
                rec_dir.mkdir(parents=True, exist_ok=True)
                (rec_dir / _MANIFEST_NAME).write_text(
                    json.dumps(
                        {
                            "status": code,
                            "synced_at": datetime.now(timezone.utc).isoformat(),
                            "files": files,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            if cancel:
                cancel.wait(0.5)  # polite crawl delay, cancellable
            else:
                time.sleep(0.5)
    finally:
        session.close()

    return downloaded
