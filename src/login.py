"""Login and token management for zjip.org.cn.

Auth flow (login.method = auto, the default):
  1. POST /v1/user/login with {"account", "password": md5(password)} over plain
     HTTP — the same request the platform's own login page sends (captured
     2026-09-06: JSON body, no captcha, no cookie, token returned in `data`).
  2. Only if that transport fails in an unexpected way (network error, non-JSON
     or reshaped response) fall back to a Playwright browser login and read the
     token from localStorage['token_key'].
  3. Cache token to data/sessions/{safe_name}.json
  4. Reuse cached token until expired; then re-login

Auth header for API calls: x-access-token: <token>
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
import yaml
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from requests.adapters import HTTPAdapter

from .exceptions import (
    CaptchaRequiredError,
    CredentialsRejectedError,
    LoginError,
    TokenExtractionError,
)
from .fsutil import atomic_write_text
from .scraper import ReadOnlyViolation
from .settings_cache import get_settings

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent
_SESSIONS_DIR = _ROOT / "data" / "sessions"
_SETTINGS_FILE = _ROOT / "config" / "settings.yaml"

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Hard ceiling for one browser login, launch → token. A normal login takes
# 20-40s. Not every Playwright call has a timeout (page.evaluate, close()),
# so a wedged renderer can block the calling thread forever — which on
# 2026-09-04 froze a whole daemon run and, with it, every later scheduled
# run. A watchdog kills the tagged chromium after this many seconds; the
# pending Playwright call then raises and the thread is released.
LOGIN_HARD_TIMEOUT_SECONDS = 120

# Unknown chromium switches are ignored but stay on the command line, so a
# unique tag lets the watchdog kill exactly this login's browser tree.
_BROWSER_TAG_SWITCH = "--zjip-login-tag="

# HTTP login: the only request the login session may send is this POST with a
# JSON body of exactly these two keys (password already md5-hexed). Anything
# else is refused before it reaches the wire — same spirit as the read-only
# guard in scraper.py, which this login session also cannot bypass.
LOGIN_HTTP_PATH = "/v1/user/login"
LOGIN_BODY_KEYS = frozenset({"account", "password"})
HTTP_LOGIN_TIMEOUT_SECONDS = 20

# login.method in settings.yaml
LOGIN_METHODS = ("auto", "http", "browser")
DEFAULT_LOGIN_METHOD = "auto"

# Positive identification of a credential rejection. Anything non-success that
# does NOT match stays a plain LoginError: the lockout streak (which pauses
# scheduled scans) must never be fed by a platform outage, maintenance page,
# rate limit, or "系统繁忙" application code.
_REJECTION_CODES = {1001}  # observed live: 账号或密码错误
_REJECTION_PATTERNS = (
    "账号或密码", "密码错误", "密码不正确", "账号不存在", "用户不存在",
    "锁定", "禁用", "冻结",
)


def _is_credential_rejection(code, message: str) -> bool:
    try:
        if int(code) in _REJECTION_CODES:
            return True
    except (TypeError, ValueError):
        pass
    return any(p in (message or "") for p in _REJECTION_PATTERNS)


class _HttpLoginUnavailable(Exception):
    """HTTP login could not be completed for a reason a browser login might
    survive (network error, non-JSON page, response shape we don't recognise).
    Never raised for rejected credentials — those fail the same way in a
    browser, so falling back would only waste a Chromium launch."""


# ---------------------------------------------------------------------------
# Settings loader (minimal, avoids importing config_loader before Phase 2)
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    return get_settings()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_token(account: str, password: str, *, force_refresh: bool = False) -> str:
    """Return a valid token for *account*, using the cache unless expired.

    Args:
        account:       Platform account (username).
        password:      Plain-text password.
        force_refresh: Skip cache and always perform a fresh login.

    Returns:
        Raw token string to be passed as ``x-access-token`` header.

    Raises:
        LoginError:           Credentials rejected, HTTP login unavailable with
                              method=http, or unexpected browser page state.
        CaptchaRequiredError: Slider captcha appeared in the browser fallback.
        TokenExtractionError: Browser login redirect OK but token missing.
    """
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    if not force_refresh:
        cached = _load_cached_token(account)
        if cached:
            logger.debug(f"[{account}] Using cached token (valid)")
            return cached

    token = _fresh_login(account, password)
    _save_token(account, token)
    return token


def verify_credentials(account: str, password: str) -> str:
    """Fresh login with ZERO token-cache interaction — never reads and never
    writes data/sessions/. Used by the web console's 测试登录 so that testing
    an *unsaved* password cannot cache a token that would mask a wrong stored
    password for the next 12h. Raises CredentialsRejectedError with the
    platform's own message on rejection."""
    return _fresh_login(account, password)


def _login_method() -> str:
    method = str(_load_settings().get("login", {}).get("method") or DEFAULT_LOGIN_METHOD).lower()
    if method not in LOGIN_METHODS:
        logger.warning(
            f"settings.yaml login.method={method!r} 非法（可选 {'/'.join(LOGIN_METHODS)}）— 按 {DEFAULT_LOGIN_METHOD} 处理"
        )
        return DEFAULT_LOGIN_METHOD
    return method


def _fresh_login(account: str, password: str) -> str:
    """Dispatch on login.method: http → browser fallback (auto), http only, or browser only."""
    method = _login_method()
    if method == "browser":
        logger.info(f"[{account}] Performing browser login…")
        return _login_via_browser(account, password)

    base_url: str = _load_settings().get("base_url", "https://www.zjip.org.cn")
    logger.info(f"[{account}] Performing HTTP login…")
    try:
        return _login_via_http(account, password, base_url)
    except _HttpLoginUnavailable as exc:
        if method == "http":
            raise LoginError(f"[{account}] HTTP login unavailable: {exc}") from exc
        logger.warning(f"[{account}] HTTP login unavailable ({exc}) — falling back to browser login")
        return _login_via_browser(account, password)


# ---------------------------------------------------------------------------
# HTTP login
# ---------------------------------------------------------------------------

def check_login_only(method: str | None, url: str, body: Any, allowed_host: str | None) -> None:
    """Raise ReadOnlyViolation unless this is the one permitted login POST:
    POST {allowed_host}/v1/user/login with a JSON object of exactly
    LOGIN_BODY_KEYS. The login session sends nothing else, ever."""
    method = (method or "").upper()
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if allowed_host and host != allowed_host:
        raise ReadOnlyViolation(
            f"login guard: host {host!r} is not the platform host {allowed_host!r}"
        )
    if method != "POST" or parsed.path != LOGIN_HTTP_PATH:
        raise ReadOnlyViolation(
            f"login guard: {method} {parsed.path or '/'} is not the login request"
        )
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(body) if body else None
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict) or set(payload) != LOGIN_BODY_KEYS:
        raise ReadOnlyViolation(
            f"login guard: body must be a JSON object with exactly {sorted(LOGIN_BODY_KEYS)}"
        )


class _LoginOnlyAdapter(HTTPAdapter):
    """HTTPAdapter that enforces check_login_only on every send()."""

    def __init__(self, allowed_host: str | None, **kwargs):
        super().__init__(**kwargs)
        self.allowed_host = (allowed_host or "").lower() or None

    def send(self, request, **kwargs):
        check_login_only(request.method, request.url, request.body, self.allowed_host)
        return super().send(request, **kwargs)


def _build_login_session(base_url: str) -> requests.Session:
    """Unauthenticated session used for the login POST only. Mirrors the
    headers a browser sends from the login page; no retries — a failed
    login is reported (or handed to the browser fallback), not repeated."""
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": _DEFAULT_USER_AGENT,
        "Origin": base_url,
        "Referer": f"{base_url}/user/login",
    })
    adapter = _LoginOnlyAdapter(urlparse(base_url).hostname, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _looks_like_jwt(value: Any) -> bool:
    return isinstance(value, str) and value.count(".") == 2 and len(value) > 20


def _login_via_http(account: str, password: str, base_url: str) -> str:
    """POST the login form the way the platform frontend does and return the token.

    Raises:
        LoginError:            Platform answered with a non-zero code (bad
                               credentials, locked account, …). Not retried in
                               a browser: same credentials, same answer.
        _HttpLoginUnavailable: Transport/shape problem — caller may fall back.
    """
    body = {
        "account": account,
        "password": hashlib.md5(password.encode("utf-8")).hexdigest(),
    }
    session = _build_login_session(base_url)
    try:
        # allow_redirects=False: a 3xx here means the platform changed (maintenance
        # page, moved endpoint). Following it would turn the POST into a GET that
        # the login guard rejects with a confusing ReadOnlyViolation; report the
        # status instead and let the browser fallback decide.
        resp = session.post(
            f"{base_url}{LOGIN_HTTP_PATH}",
            json=body,
            timeout=HTTP_LOGIN_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise _HttpLoginUnavailable(f"request failed: {exc.__class__.__name__}: {exc}") from exc
    finally:
        session.close()

    if resp.status_code != 200:
        raise _HttpLoginUnavailable(f"HTTP {resp.status_code}: {resp.text[:120]!r}")
    try:
        payload = resp.json()
    except ValueError:
        raise _HttpLoginUnavailable(f"non-JSON response: {resp.text[:120]!r}") from None
    if not isinstance(payload, dict) or "code" not in payload:
        raise _HttpLoginUnavailable(f"unexpected response shape: {str(payload)[:120]!r}")

    code = payload.get("code")
    if code not in (0, 200):
        message = str(payload.get("message") or payload.get("msg") or "").strip()
        if _is_credential_rejection(code, message):
            raise CredentialsRejectedError(
                f"[{account}] Login rejected by platform (code={code}): {message or '(no message)'}"
            )
        # Server-side application error (维护/繁忙/限流/未知码): fail loudly but
        # never as a rejection — a browser retry would get the same envelope,
        # and the lockout streak must not count it.
        raise LoginError(
            f"[{account}] Platform login error (code={code}): {message or '(no message)'}"
        )

    token = payload.get("data")
    if not _looks_like_jwt(token):
        raise _HttpLoginUnavailable(
            f"code={code} but data is not a token: {str(token)[:60]!r}"
        )
    logger.info(f"[{account}] HTTP login successful — token length: {len(token)}")
    return token


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_name(account: str) -> str:
    """Convert account string to a safe filename stem."""
    safe = re.sub(r"[^\w@.-]", "_", account)
    # Append short hash to avoid collisions after sanitisation
    h = hashlib.sha1(account.encode()).hexdigest()[:6]
    return f"{safe}_{h}"


def _session_path(account: str) -> Path:
    return _SESSIONS_DIR / f"{_safe_name(account)}.json"


def _load_cached_token(account: str) -> Optional[str]:
    """Return cached token if the file exists and the token is still valid."""
    path = _session_path(account)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token: str = data.get("token", "")
        expires_at_str: str = data.get("expires_at", "")
        if not token or not expires_at_str:
            return None
        expires_at = datetime.fromisoformat(expires_at_str)
        # Give a 5-minute buffer before expiry
        if datetime.now(timezone.utc) < expires_at - timedelta(minutes=5):
            return token
        logger.debug(f"[{account}] Cached token expired at {expires_at_str}")
    except Exception as exc:
        logger.warning(f"[{account}] Could not read token cache: {exc}")
    return None


def _save_token(account: str, token: str) -> None:
    """Persist token with metadata to the sessions directory."""
    fetched_at = datetime.now(timezone.utc)
    expires_at = _compute_expiry(token, fetched_at)
    path = _session_path(account)
    payload = {
        "account": account,
        "token": token,
        "fetched_at": fetched_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    logger.debug(f"[{account}] Token cached → {path.name}, expires {expires_at.isoformat()}")


def _compute_expiry(token: str, fetched_at: datetime) -> datetime:
    """Try to read JWT exp claim; fall back to configured max-age."""
    try:
        parts = token.split(".")
        if len(parts) == 3:
            # Add padding and decode the payload segment
            payload_b64 = parts[1] + "=="
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp: Optional[int] = payload.get("exp")
            if exp:
                return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        pass

    settings = _load_settings()
    max_age_hours: int = settings.get("login", {}).get("token_max_age_hours", 12)
    return fetched_at + timedelta(hours=max_age_hours)


def _login_via_browser(account: str, password: str) -> str:
    """Launch Playwright, fill the login form, and extract the token.

    Returns:
        Raw token string from localStorage['token_key'].

    Raises:
        CaptchaRequiredError: If a slider captcha element is detected post-submit.
        LoginError:           If the browser hits an unexpected state.
        TokenExtractionError: If the redirect succeeds but the token is absent.
    """
    settings = _load_settings()
    login_cfg = settings.get("login", {})
    headless: bool = login_cfg.get("headless", True)
    base_url: str = settings.get("base_url", "https://www.zjip.org.cn")

    tag = f"{_BROWSER_TAG_SWITCH}{uuid.uuid4().hex}"
    watchdog = threading.Timer(
        LOGIN_HARD_TIMEOUT_SECONDS, _kill_tagged_browser, args=(tag, account)
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        return _login_via_browser_inner(account, password, headless, base_url, tag)
    finally:
        watchdog.cancel()


def _kill_tagged_browser(tag: str, account: str) -> None:
    """Watchdog: kill the chromium launched with `tag` so the stuck login
    thread gets a Playwright error instead of waiting forever."""
    logger.error(
        f"[{account}] Browser login exceeded {LOGIN_HARD_TIMEOUT_SECONDS}s — "
        "killing browser to release the worker thread"
    )
    try:
        # -f matches the full command line; every chromium child process
        # inherits the switch, so the whole browser tree goes down.
        subprocess.run(["pkill", "-9", "-f", tag], check=False, timeout=10)
    except Exception as exc:  # pragma: no cover — best effort
        logger.warning(f"[{account}] Could not kill tagged browser: {exc!r}")


def _login_via_browser_inner(
    account: str, password: str, headless: bool, base_url: str, tag: str
) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", tag],
        )
        context = browser.new_context(user_agent=_DEFAULT_USER_AGENT)
        page = context.new_page()

        try:
            login_url = f"{base_url}/user/login"
            logger.debug(f"[{account}] Navigating to {login_url}")
            # domcontentloaded, not networkidle: the SPA keeps connections open
            # and networkidle regularly burns the whole 30s timeout for nothing.
            # The wait_for_selector below is the real readiness check.
            page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)

            # Wait for the login form to be visible
            page.wait_for_selector('input[name="account"], input[type="text"]', timeout=10_000)

            # Fill credentials
            # The form uses name="account" and name="password" based on API analysis
            _fill_login_form(page, account, password)

            # Submit
            _click_login_button(page)

            # Check for captcha before waiting for redirect
            time.sleep(1.5)
            if _has_captcha(page):
                raise CaptchaRequiredError(
                    f"[{account}] Slider captcha detected. "
                    "Set 'login.headless: false' in settings.yaml and run once manually "
                    "to complete the captcha; the token will be cached for future runs."
                )

            # Wait for post-login navigation. The SPA lands on /drintro after a
            # successful login (observed 2026-09-06); /dregister, /home and
            # /dashboard are kept for older builds. Missing the real landing
            # page here silently cost a full 15s timeout on every login.
            try:
                page.wait_for_url(
                    re.compile(r".*(drintro|dregister|/home|dashboard).*"),
                    timeout=15_000,
                )
            except PlaywrightTimeout:
                # Check if we're still on the login page (bad credentials)
                current_url = page.url
                if "login" in current_url:
                    # Try to grab an error message from the page. Only a
                    # message that positively names a credential problem is a
                    # rejection — "still on login page" with no (or a generic)
                    # error also happens when the login backend is down, and
                    # that must not feed the lockout streak.
                    error_text = _get_page_error(page)
                    msg = (
                        f"[{account}] Login failed — still on login page. "
                        f"Error: {error_text or '(no visible error message)'}"
                    )
                    if error_text and _is_credential_rejection(None, error_text):
                        raise CredentialsRejectedError(msg)
                    raise LoginError(msg)
                # Otherwise we're somewhere else; proceed to token extraction

            # Extract token from localStorage.
            # The app stores it as JSON.stringify(token), so we JSON.parse it here.
            token: Optional[str] = page.evaluate(
                "(() => { const r = localStorage.getItem('token_key');"
                " if (!r) return null;"
                " try { return JSON.parse(r); } catch(e) { return r; } })()"
            )
            if not token:
                raise TokenExtractionError(
                    f"[{account}] Login redirect completed but 'token_key' is absent "
                    "from localStorage. The platform may have changed its auth mechanism."
                )

            logger.info(f"[{account}] Login successful — token length: {len(token)}")
            # Nothing else may happen in this browser once we have the token:
            # close the page right away so no later code can interact with the
            # logged-in session.
            try:
                page.close()
            except Exception:
                pass
            return token

        finally:
            # Never let a failing close() mask the real login outcome.
            for closer in (context.close, browser.close):
                try:
                    closer()
                except Exception as exc:
                    logger.debug(f"[{account}] Browser cleanup error ignored: {exc!r}")


def _assert_on_login_page(page, action: str) -> None:
    """Read-only guard: the only page we ever type into or click on is the
    login page. If the platform ever redirects /user/login elsewhere (e.g. an
    already-authenticated session), refuse rather than fill/click blindly —
    a generic submit selector on a business page could trigger a real action."""
    url = getattr(page, "url", "") or ""
    if "/login" not in url:
        raise LoginError(
            f"[guard] refusing to {action}: current page is not the login page ({url})"
        )


def _fill_login_form(page, account: str, password: str) -> None:
    """Fill account and password fields.

    Tries multiple selector strategies in order of preference.
    Logs which selector succeeded so operators can update if the UI changes.
    Deliberately no generic input[type=text]/[type=password] fallback: if the
    login form cannot be identified precisely we fail instead of guessing.
    """
    _assert_on_login_page(page, "fill credentials")
    strategies = [
        ('name="account"', 'name="password"'),
        ('placeholder*="账号"', 'placeholder*="密码"'),
    ]

    for acc_sel, pwd_sel in strategies:
        acc_locator = page.locator(f"input[{acc_sel}]")
        pwd_locator = page.locator(f"input[{pwd_sel}]")
        if acc_locator.count() > 0 and pwd_locator.count() > 0:
            logger.debug(f"Using selectors: [{acc_sel}] / [{pwd_sel}]")
            acc_locator.first.fill(account)
            pwd_locator.first.fill(password)
            return

    raise LoginError(
        "Cannot locate account/password fields. "
        "The login page structure may have changed."
    )


def _click_login_button(page) -> None:
    """Click the login submit button — the only click this tool ever performs."""
    _assert_on_login_page(page, "click the login button")
    # Semantic selector first; generic submit selectors only as fallback.
    strategies = [
        'button:has-text("登录")',
        'button[type="submit"]',
        'input[type="submit"]',
    ]
    for sel in strategies:
        btn = page.locator(sel)
        n = btn.count()
        if n > 0:
            if n > 1:
                logger.warning(
                    f"{n} elements match {sel!r} on the login page — clicking the first"
                )
            logger.debug(f"Clicking login button: {sel}")
            btn.first.click()
            return
    raise LoginError(
        "Cannot locate the login button. "
        "The login page structure may have changed."
    )


def _has_captcha(page) -> bool:
    """Return True if a slider captcha element is visible on the page."""
    captcha_selectors = [
        ".slider-captcha",
        ".slide-verify",
        '[class*="captcha"]',
        '[class*="slider"]',
    ]
    for sel in captcha_selectors:
        loc = page.locator(sel)
        if loc.count() > 0 and loc.first.is_visible():
            logger.warning(f"Captcha detected via selector: {sel}")
            return True
    return False


def _get_page_error(page) -> Optional[str]:
    """Try to extract a visible error message from the login page."""
    error_selectors = [
        ".error-msg",
        ".ant-message-error",
        '[class*="error"]',
        '[class*="alert"]',
    ]
    for sel in error_selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                return loc.first.inner_text().strip()
            except Exception:
                pass
    return None
