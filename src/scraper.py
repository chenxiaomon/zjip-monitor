"""Data fetching for zjip.org.cn registration records.

Uses direct HTTP (requests) with the token from login.py.
API: POST /v1/datareg/list?pageNum=N&pageSize=N
Auth: x-access-token header
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

import requests
import yaml
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .exceptions import ScraperError, TokenExpiredError

# Verified against live DOM + API responses across 4 real accounts (2026-05-19):
# Cross-referenced dataRegAuditStatus codes with actual UI labels on /dregister page.
AUDIT_STATUS: dict[int, str] = {
    1: "待审核",
    2: "待补正",
    3: "公示中",
    5: "主动撤回",
    6: "不予登记",
    7: "登记成功",
    11: "视为撤回",
}


def decode_status(status_code: int | None) -> str:
    """Convert numeric dataRegAuditStatus to Chinese label."""
    if status_code is None:
        return "未知"
    return AUDIT_STATUS.get(int(status_code), f"状态{status_code}")

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent
_SETTINGS_FILE = _ROOT / "config" / "settings.yaml"


def _load_settings() -> dict:
    if _SETTINGS_FILE.exists():
        with open(_SETTINGS_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_all_records(
    token: str,
    page_size: int | None = None,
    base_url: str | None = None,
) -> list[dict]:
    """Fetch every datareg record for the authenticated account.

    Args:
        token:     Value of localStorage['token_key'] obtained from login.py.
        page_size: Records per page (default from settings.yaml → scraper.page_size).
        base_url:  Override the platform base URL (default from settings.yaml).

    Returns:
        List of record dicts, each containing the following keys (as returned by the API;
        field names are logged on first page so you can verify them):
            - 登记编号 (registration number)
            - 数据知识产权名称 (name)
            - 所属行业 / 行业大类 (industry)
            - 数据类型 / 数据来源 (data type / source)
            - 申请时间 (application time)
            - 审核状态 (audit status)  ← the key field for monitoring

    Raises:
        TokenExpiredError: API returns 401 or token-expired error code.
        ScraperError:      Any other fetch failure.
    """
    settings = _load_settings()
    scraper_cfg = settings.get("scraper", {})

    if page_size is None:
        page_size = scraper_cfg.get("page_size", 50)
    if base_url is None:
        base_url = settings.get("base_url", "https://www.zjip.org.cn")

    timeout = scraper_cfg.get("request_timeout_seconds", 30)

    session = _build_session(token, timeout=timeout)

    try:
        first_page = _fetch_page(session, base_url, page_num=1, page_size=page_size)
        total, records = _parse_response(first_page, page_num=1)
        logger.info(f"Total records (server): {total}")

        # Server ignores 'size' param and always returns a fixed page size (~10).
        # Paginate by incrementing 'page' until the last page returns fewer records
        # than the previous page (meaning we've hit the end).
        server_page_size = len(records)  # actual records per page from server
        if server_page_size == 0:
            return records

        page_num = 2
        # Safety cap: never fetch more than (total / server_page_size + 5) pages
        max_pages = math.ceil(total / max(server_page_size, 1)) + 5

        while page_num <= max_pages:
            time.sleep(0.5)  # polite crawl delay
            page_data = _fetch_page(session, base_url, page_num=page_num, page_size=page_size)
            _, page_records = _parse_response(page_data, page_num=page_num)
            if not page_records:
                break
            records.extend(page_records)
            # Stop when we've reached or exceeded the reported total
            if len(records) >= total:
                break
            # Also stop if this page returned fewer records (last page)
            if len(page_records) < server_page_size:
                break
            page_num += 1

        # Deduplicate by stable key (guard against API returning overlapping pages)
        seen: set[str] = set()
        deduped: list[dict] = []
        for r in records:
            key = str(r.get("id") or r.get("dataRegNo") or id(r))
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        logger.info(f"Fetched {len(deduped)} records total")
        return deduped

    except (TokenExpiredError, ScraperError):
        raise
    except Exception as exc:
        raise ScraperError(f"Unexpected error during data fetch: {exc}") from exc
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_session(token: str, timeout: int = 30) -> requests.Session:
    """Create a requests.Session with auth header and retry logic."""
    session = requests.Session()
    session.headers.update({
        "x-access-token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.zjip.org.cn/dregister",
    })

    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # Store timeout on the session object for use in _fetch_page
    session._zjip_timeout = timeout  # type: ignore[attr-defined]
    return session


def _fetch_page(
    session: requests.Session,
    base_url: str,
    page_num: int,
    page_size: int,
) -> dict[str, Any]:
    """POST a single page of /v1/datareg/list.

    Confirmed from live API (2026-05-19):
    - Correct param names: page (1-indexed) + size (server ignores size, always returns 10)
    - pageNum/pageSize are ignored by the server

    Returns:
        Parsed JSON response dict.

    Raises:
        TokenExpiredError: HTTP 401 or API code indicating auth failure.
        ScraperError:      Other HTTP or JSON errors.
    """
    url = f"{base_url}/v1/datareg/list"
    params = {"page": page_num, "size": page_size}
    timeout = getattr(session, "_zjip_timeout", 30)

    logger.debug(f"Fetching page {page_num} → {url} params={params}")

    try:
        resp = session.post(url, params=params, json={}, timeout=timeout)
    except requests.RequestException as exc:
        raise ScraperError(f"HTTP error on page {page_num}: {exc}") from exc

    if resp.status_code == 401:
        raise TokenExpiredError(
            "Server returned 401 — token is expired or invalid. Re-login required."
        )
    if not resp.ok:
        raise ScraperError(
            f"HTTP {resp.status_code} on page {page_num}: {resp.text[:200]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise ScraperError(f"Non-JSON response on page {page_num}: {resp.text[:200]}") from exc

    code = data.get("code")
    # Log raw first-page response at DEBUG so the operator can verify field names
    if page_num == 1:
        logger.debug(f"First page raw response keys: {list(data.keys())}")
        if isinstance(data.get("data"), dict):
            logger.debug(f"data keys: {list(data['data'].keys())}")

    # Common auth-failure codes
    if code in (401, 2001, 2002, 2208):
        raise TokenExpiredError(
            f"API returned auth-failure code {code}: {data.get('message', '')}. "
            "Re-login required."
        )

    return data


def _parse_response(data: dict[str, Any], page_num: int) -> tuple[int, list[dict]]:
    """Extract total count and record list from an API response.

    The API response shape is unknown until first runtime; this function tries
    common patterns used by Chinese government platforms.

    Returns:
        (total_count, records_list)

    Raises:
        ScraperError: If the response has an unexpected shape.
    """
    code = data.get("code")
    if code != 200 and code != 0:
        raise ScraperError(
            f"API error on page {page_num}: code={code}, message={data.get('message', '')}"
        )

    inner = data.get("data", {})

    # Pattern A: data = {"elements": [...], "totalSize": N}  ← confirmed from live API
    # Also try legacy names in case other endpoints differ
    if isinstance(inner, dict):
        records = (
            inner.get("elements")
            or inner.get("list")
            or inner.get("records")
            or inner.get("rows")
            or inner.get("data")
            or []
        )
        total = (
            inner.get("totalSize")
            or inner.get("total")
            or inner.get("totalCount")
            or inner.get("count")
            or inner.get("totalNum")
            or len(records)
        )
    # Pattern B: data = [...]  (flat list, only 1 page)
    elif isinstance(inner, list):
        records = inner
        total = len(records)
    else:
        raise ScraperError(
            f"Unexpected response shape on page {page_num}: "
            f"data type={type(inner).__name__}"
        )

    if not isinstance(records, list):
        raise ScraperError(
            f"Record list is not a list on page {page_num}: type={type(records).__name__}"
        )

    try:
        total = int(total)
    except (TypeError, ValueError):
        total = len(records)

    logger.debug(f"Page {page_num}: {len(records)} records (total reported: {total})")
    return total, list(records)
