"""Login and token management for zjip.org.cn.

Auth flow:
  1. POST /v1/user/login with {account, password} via Playwright UI
  2. Extract token from localStorage['token_key']
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
from typing import Optional

import yaml
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from .exceptions import (
    CaptchaRequiredError,
    LoginError,
    TokenExtractionError,
)
from .fsutil import atomic_write_text
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
        force_refresh: Skip cache and always perform a fresh browser login.

    Returns:
        Raw token string to be passed as ``x-access-token`` header.

    Raises:
        LoginError:           Credentials rejected or unexpected page state.
        CaptchaRequiredError: Slider captcha appeared — run headless=false once.
        TokenExtractionError: Login redirect OK but token missing from localStorage.
    """
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    if not force_refresh:
        cached = _load_cached_token(account)
        if cached:
            logger.debug(f"[{account}] Using cached token (valid)")
            return cached

    logger.info(f"[{account}] Performing browser login…")
    token = _login_via_browser(account, password)
    _save_token(account, token)
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

            # Wait for post-login navigation (SPA redirects to /dregister or /home)
            try:
                page.wait_for_url(
                    re.compile(r".*(dregister|/home|dashboard).*"),
                    timeout=15_000,
                )
            except PlaywrightTimeout:
                # Check if we're still on the login page (bad credentials)
                current_url = page.url
                if "login" in current_url:
                    # Try to grab an error message from the page
                    error_text = _get_page_error(page)
                    raise LoginError(
                        f"[{account}] Login failed — still on login page. "
                        f"Error: {error_text or '(no visible error message)'}"
                    )
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
