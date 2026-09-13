"""Data fetching for zjip.org.cn registration records.

Uses direct HTTP (requests) with the token from login.py.
API: POST /v1/datareg/list?pageNum=N&pageSize=N
Auth: x-access-token header
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import requests
import yaml
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .exceptions import RunCancelled, ScraperError, TokenExpiredError
from .runtime_settings import scraper_settings
from .settings_cache import get_settings

# ---------------------------------------------------------------------------
# Read-only contract with the platform
# ---------------------------------------------------------------------------
# This tool only ever READS from zjip.org.cn. Every request that goes through
# _build_session() must be one of these. The platform uses POST for two of its
# query endpoints (all parameters in the query string, empty JSON body), so
# those two are allowed with a mandatory empty body; anything else — other
# methods, other POST paths, a POST with a payload, or a different host — is
# refused before it leaves the process. Extend deliberately, never casually.
READ_ONLY_METHODS = frozenset({"GET", "HEAD"})
READ_ONLY_POST_PATHS = frozenset({
    "/v1/datareg/list",          # registration list (paged)
    "/v1/datareg/confirmation",  # certificate download URLs
})
_EMPTY_BODIES = (None, b"", b"{}", "", "{}")


class ReadOnlyViolation(ScraperError):
    """A request that would break the read-only contract was blocked."""


class _ReadOnlyAdapter(HTTPAdapter):
    """HTTPAdapter that enforces the read-only contract on every send()."""

    def __init__(self, allowed_host: str | None, **kwargs):
        super().__init__(**kwargs)
        self.allowed_host = (allowed_host or "").lower() or None

    def send(self, request, **kwargs):
        check_read_only(request.method, request.url, request.body, self.allowed_host)
        return super().send(request, **kwargs)


def check_read_only(
    method: str | None, url: str, body: Any, allowed_host: str | None
) -> None:
    """Raise ReadOnlyViolation unless (method, url, body) is a permitted read."""
    method = (method or "").upper()
    parsed = urlparse(url or "")
    path = parsed.path or "/"
    host = (parsed.hostname or "").lower()

    if allowed_host and host != allowed_host:
        raise ReadOnlyViolation(
            f"read-only guard: host {host!r} is not the platform host {allowed_host!r}"
        )
    if method in READ_ONLY_METHODS:
        return
    if method == "POST" and path in READ_ONLY_POST_PATHS:
        if body in _EMPTY_BODIES:
            return
        raise ReadOnlyViolation(
            f"read-only guard: POST {path} must carry an empty body, got {body!r:.80}"
        )
    raise ReadOnlyViolation(f"read-only guard: {method} {path} is not a permitted read")


if TYPE_CHECKING:
    from .control import CancelToken

# Full status map extracted from the platform frontend bundle (2026-07):
# all 17 dataRegAuditStatus codes with their official UI labels.
AUDIT_STATUS: dict[int, str] = {
    1: "待审核",
    2: "待补正",
    3: "公示中",
    4: "异议",
    5: "主动撤回",
    6: "不予登记",
    7: "登记成功",
    8: "终止登记",
    9: "登记撤销",
    10: "未续展失效",
    11: "视为撤回",
    12: "待审核",
    13: "续展登记",
    14: "批量登记失败",
    15: "撤回审查",
    16: "撤回补正",
    17: "待发证",
}


# Warn once per unknown status code: a new platform code should reach the
# operator's logs, not silently render as "状态18" forever.
_warned_unknown_codes: set[int] = set()


def int_or_none(value) -> int | None:
    """Coerce a raw JSON status code (the platform has been seen to send both
    7 and "7") to int, else None. The single normalizer for status codes —
    web/deps re-exports it so page rendering and diffing cannot drift apart."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def decode_status(status_code: int | None) -> str:
    """Convert numeric dataRegAuditStatus to Chinese label."""
    if status_code is None:
        return "未知"
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return f"状态{status_code}"
    if code not in AUDIT_STATUS and code not in _warned_unknown_codes:
        _warned_unknown_codes.add(code)
        logger.warning(
            f"Unknown dataRegAuditStatus code {code} — platform may have added "
            "a new status; update AUDIT_STATUS in src/scraper.py"
        )
    return AUDIT_STATUS.get(code, f"状态{code}")

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent
_SETTINGS_FILE = _ROOT / "config" / "settings.yaml"


def _load_settings() -> dict:
    return get_settings()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Hard page cap independent of the server's totalSize (which is advisory only).
# At the server's fixed ~10 records/page this allows ~3000 records; override
# via settings.yaml → scraper.max_pages.


@dataclass
class FetchResult:
    """Outcome of one full pagination sweep.

    complete=False means a mid-pagination error (or the safety cap) truncated
    the crawl — `records` holds everything fetched up to that point instead of
    being discarded; `error` says why. `reported_total` is the server's
    totalSize claim from page 1, kept for reconciliation only — pagination
    never trusts it.
    """

    records: list[dict] = field(default_factory=list)
    reported_total: int = 0
    pages_fetched: int = 0
    complete: bool = True
    error: str | None = None
    # True only when page 1 actually carried a total field. _parse_response
    # otherwise synthesizes total from the page length, and that synthetic
    # value must never "confirm" a shrink (main.py's escape hatch checks this).
    total_from_server: bool = False


def fetch_all_records(
    token: str,
    page_size: int | None = None,
    base_url: str | None = None,
    cancel: "CancelToken | None" = None,
) -> FetchResult:
    """Fetch every datareg record for the authenticated account.

    Args:
        token:     Value of localStorage['token_key'] obtained from login.py.
        page_size: Records per page (default from settings.yaml → scraper.page_size).
        base_url:  Override the platform base URL (default from settings.yaml).

    Returns:
        FetchResult — .records holds the (deduped) record dicts; .complete is
        False when a page ≥2 failed or the safety cap was hit, in which case
        the pages fetched so far are still returned.

    Raises:
        TokenExpiredError: API returns 401 or token-expired error code.
        ScraperError:      Page 1 failed (nothing worth keeping).
    """
    settings = _load_settings()
    # All four numeric knobs go through the same validation as the rest of
    # settings.yaml: a quoted "30" for the timeout used to reach requests as
    # a str, and a bad max_pages was the only one with a fallback.
    sc = scraper_settings()

    if page_size is None:
        page_size = sc.page_size
    if base_url is None:
        base_url = settings.get("base_url", "https://www.zjip.org.cn")

    timeout = sc.request_timeout_seconds
    max_pages = sc.max_pages
    page_delay = sc.page_delay_seconds

    session = _build_session(token, timeout=timeout)

    try:
        first_page = _fetch_page(session, base_url, page_num=1, page_size=page_size)
        total, first_records = _parse_response(first_page, page_num=1)
        total_from_server = total is not None
        logger.info(f"Total records (server): {total if total_from_server else '(not reported)'}")

        # Deduplicate incrementally (API may return overlapping pages) so the
        # stop conditions below count unique records.
        seen: set[str] = set()
        records: list[dict] = []

        def _add_unique(page_records: list[dict]) -> int:
            added = 0
            for r in page_records:
                key = str(r.get("id") or r.get("dataRegNo") or id(r))
                if key not in seen:
                    seen.add(key)
                    records.append(r)
                    added += 1
            return added

        _add_unique(first_records)
        result = FetchResult(
            records=records,
            reported_total=total if total_from_server else 0,
            pages_fetched=1,
            total_from_server=total_from_server,
        )
        if not first_records:
            return result

        # The server ignores 'size' and returns a fixed page length (~10), but
        # totalSize has been observed to under-report — so pagination trusts
        # only what the pages themselves say, and only the two conditions that
        # cannot be faked by a degraded response: an empty page, or a page with
        # no new unique records (server repeating its last page).
        #
        # A "short page means end of data" heuristic used to live here. It was
        # removed because a mid-crawl short page (a throttled/degraded HTTP 200
        # carrying fewer rows) silently truncated the sweep while still
        # reporting complete=True, so the partial-merge guard in main.py never
        # ran and the unfetched tail was diffed as 「消失」. The cost is one
        # extra request per account whose last page is short; the platform
        # answers an out-of-range page with an empty list, not an error
        # (observed in production: an account with exactly 10 records reports
        # "2 pages").
        page_num = 2

        while True:
            if page_num > max_pages:
                result.complete = False
                result.error = f"safety cap scraper.max_pages={max_pages} reached"
                logger.warning(
                    f"Pagination stopped at the max_pages cap ({max_pages}) with "
                    f"{len(records)} records fetched — raise scraper.max_pages in "
                    "settings.yaml if this account really has more"
                )
                break
            if cancel:
                cancel.wait(page_delay)  # polite crawl delay, cancellable per page
            else:
                time.sleep(page_delay)
            try:
                page_data = _fetch_page(session, base_url, page_num=page_num, page_size=page_size)
                _, page_records = _parse_response(page_data, page_num=page_num)
            except TokenExpiredError:
                raise  # main.py re-logins and retries the whole fetch
            except ScraperError as exc:
                # Keep pages 1..n-1 instead of discarding the whole account.
                result.complete = False
                result.error = f"page {page_num}: {exc}"
                logger.error(
                    f"Page {page_num} failed ({exc}) — keeping the {len(records)} "
                    "records fetched so far as a partial result"
                )
                break
            result.pages_fetched += 1
            if not page_records:
                break
            added = _add_unique(page_records)
            if added == 0:
                break  # server is repeating records — end of data
            page_num += 1

        if result.complete and result.total_from_server and len(records) != result.reported_total:
            logger.warning(
                f"Reconciliation mismatch: server reported {result.reported_total} "
                f"records but {len(records)} were fetched"
            )
        logger.info(f"Fetched {len(records)} records total ({result.pages_fetched} pages)")
        return result

    except (TokenExpiredError, ScraperError, RunCancelled):
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
        # GET included: attachments.py reuses this session for getNotification
        allowed_methods=["POST", "GET"],
        raise_on_status=False,
    )
    # Every platform request funnels through this adapter, which refuses
    # anything outside the read-only contract (see check_read_only).
    allowed_host = urlparse(_load_settings().get("base_url", "https://www.zjip.org.cn")).hostname
    adapter = _ReadOnlyAdapter(allowed_host, max_retries=retry)
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


def _parse_response(data: dict[str, Any], page_num: int) -> tuple[int | None, list[dict]]:
    """Extract total count and record list from an API response.

    The API response shape is unknown until first runtime; this function tries
    common patterns used by Chinese government platforms.

    Returns:
        (total_count, records_list) — total_count is None when the response
        carried no usable total field (flat list, missing/non-numeric keys),
        so callers can tell a genuine server total from a synthesized one.

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
        # `is not None` checks, not `or`-chaining: totalSize=0 is a legitimate
        # value (empty account) and must not fall through to other keys.
        # None (no key present) — NOT len(records) — so a synthesized total can
        # never masquerade as a server-asserted one downstream.
        total = next(
            (
                inner[k]
                for k in ("totalSize", "total", "totalCount", "count", "totalNum")
                if inner.get(k) is not None
            ),
            None,
        )
    # Pattern B: data = [...]  (flat list — no total field exists)
    elif isinstance(inner, list):
        records = inner
        total = None
    else:
        raise ScraperError(
            f"Unexpected response shape on page {page_num}: "
            f"data type={type(inner).__name__}"
        )

    if not isinstance(records, list):
        raise ScraperError(
            f"Record list is not a list on page {page_num}: type={type(records).__name__}"
        )

    if total is not None:
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = None

    logger.debug(f"Page {page_num}: {len(records)} records (total reported: {total})")
    return total, list(records)
