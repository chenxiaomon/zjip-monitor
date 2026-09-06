"""Browser-login watchdog: a wedged Playwright call must not block forever."""

import threading
import time
from contextlib import contextmanager

import pytest

from src import login


class _Blocking:
    """Fake Playwright objects: everything is a no-op except evaluate(), which
    blocks until `gate` is set — exactly what a hung renderer looks like."""

    def __init__(self, gate: threading.Event):
        self.gate = gate
        self.url = "https://www.zjip.org.cn/home"

    # page API used by _login_via_browser_inner
    def goto(self, *a, **k): pass
    def wait_for_selector(self, *a, **k): pass
    def wait_for_url(self, *a, **k): pass
    def evaluate(self, *a, **k):
        self.gate.wait(10)
        raise RuntimeError("Target closed")  # what Playwright raises once the browser dies
    # context/browser API
    def new_context(self, **k): return self
    def new_page(self): return self
    def close(self): pass


@pytest.fixture
def fake_playwright(monkeypatch):
    gate = threading.Event()
    killed: list[str] = []

    fake = _Blocking(gate)

    class _Chromium:
        def launch(self, **kwargs):
            fake.launch_args = kwargs.get("args", [])
            return fake

    class _PW:
        chromium = _Chromium()

    @contextmanager
    def _sync_playwright():
        yield _PW()

    def _kill(tag, account):
        killed.append(tag)
        gate.set()  # killing the browser unblocks the pending call

    monkeypatch.setattr(login, "sync_playwright", _sync_playwright)
    monkeypatch.setattr(login, "_fill_login_form", lambda *a, **k: None)
    monkeypatch.setattr(login, "_click_login_button", lambda *a, **k: None)
    monkeypatch.setattr(login, "_has_captcha", lambda page: False)
    monkeypatch.setattr(login, "_load_settings", lambda: {"login": {"headless": True}})
    monkeypatch.setattr(login.time, "sleep", lambda s: None)
    monkeypatch.setattr(login, "_kill_tagged_browser", _kill)
    return fake, killed, gate


def test_watchdog_kills_browser_and_releases_thread(fake_playwright, monkeypatch):
    fake, killed, _ = fake_playwright
    monkeypatch.setattr(login, "LOGIN_HARD_TIMEOUT_SECONDS", 0.3)

    start = time.monotonic()
    with pytest.raises(RuntimeError, match="Target closed"):
        login._login_via_browser("acct", "pw")
    elapsed = time.monotonic() - start

    assert elapsed < 3.0
    assert len(killed) == 1
    tag = killed[0]
    assert tag.startswith(login._BROWSER_TAG_SWITCH)
    assert tag in fake.launch_args  # browser really was launched with the tag


def test_watchdog_not_fired_on_fast_login(fake_playwright, monkeypatch):
    fake, killed, gate = fake_playwright
    monkeypatch.setattr(login, "LOGIN_HARD_TIMEOUT_SECONDS", 5)
    gate.set()
    fake.evaluate = lambda *a, **k: "tok"  # instant success

    assert login._login_via_browser("acct", "pw") == "tok"
    time.sleep(0.2)
    assert killed == []


def test_each_login_gets_unique_tag(fake_playwright, monkeypatch):
    fake, _, gate = fake_playwright
    monkeypatch.setattr(login, "LOGIN_HARD_TIMEOUT_SECONDS", 5)
    gate.set()
    fake.evaluate = lambda *a, **k: "tok"
    login._login_via_browser("a", "pw"); t1 = [x for x in fake.launch_args if "zjip-login-tag" in x][0]
    login._login_via_browser("b", "pw"); t2 = [x for x in fake.launch_args if "zjip-login-tag" in x][0]
    assert t1 != t2
