"""Read-only contract: this tool must never send anything but reads to the platform.

Two layers are covered:
  * HTTP — every request through scraper._build_session() passes the
    read-only adapter; anything outside the whitelist raises before sending.
  * Browser — the only page we type into / click on is the login page.
"""

import pytest
import requests

from src import attachments, login, scraper
from src.exceptions import LoginError

BASE = "https://www.zjip.org.cn"


# ---------------------------------------------------------------------------
# check_read_only (pure function)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("GET", "/v1/datareg/getNotification", None),
    ("HEAD", "/anything", None),
    ("POST", "/v1/datareg/list", b"{}"),
    ("POST", "/v1/datareg/list", None),
    ("POST", "/v1/datareg/confirmation", b"{}"),
])
def test_permitted_reads_pass(method, path, body):
    scraper.check_read_only(method, f"{BASE}{path}?x=1", body, "www.zjip.org.cn")


@pytest.mark.parametrize("method,path,body", [
    ("DELETE", "/v1/datareg/123", None),
    ("PUT", "/v1/datareg/list", b"{}"),
    ("PATCH", "/v1/datareg/list", b"{}"),
    ("POST", "/v1/datareg/withdraw", b"{}"),          # unknown POST path
    ("POST", "/v1/datareg/submit", b'{"dataRegNo": "X"}'),
    ("POST", "/v1/datareg/list", b'{"page": 1}'),     # payload on a query POST
    ("POST", "/v1/datareg/confirmation", b'{"confirm": true}'),
])
def test_writes_are_blocked(method, path, body):
    with pytest.raises(scraper.ReadOnlyViolation):
        scraper.check_read_only(method, f"{BASE}{path}", body, "www.zjip.org.cn")


def test_other_host_is_blocked():
    with pytest.raises(scraper.ReadOnlyViolation):
        scraper.check_read_only("GET", "https://evil.example.com/v1/datareg/list", None,
                                "www.zjip.org.cn")


# ---------------------------------------------------------------------------
# The real session wires the adapter in; blocked requests never reach the wire
# ---------------------------------------------------------------------------

@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(scraper, "_load_settings", lambda: {"base_url": BASE})
    sess = scraper._build_session("tok")
    yield sess
    sess.close()


def _no_network(monkeypatch):
    """If anything gets past the guard, HTTPAdapter.send would hit the network — fail loudly."""
    def boom(self, request, **kw):
        raise AssertionError(f"request escaped the read-only guard: {request.method} {request.url}")
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", boom)


def test_session_blocks_delete_and_unknown_post(session, monkeypatch):
    _no_network(monkeypatch)
    with pytest.raises(scraper.ReadOnlyViolation):
        session.delete(f"{BASE}/v1/datareg/DIP2026000000001")
    with pytest.raises(scraper.ReadOnlyViolation):
        session.post(f"{BASE}/v1/datareg/withdraw", json={"dataRegNo": "X"})
    with pytest.raises(scraper.ReadOnlyViolation):
        session.post(f"{BASE}/v1/datareg/list", json={"page": 1})
    with pytest.raises(scraper.ReadOnlyViolation):
        session.get("https://attacker.example.com/v1/datareg/list")


def test_session_lets_whitelisted_reads_through(session, monkeypatch):
    seen = []

    def fake_send(self, request, **kw):
        seen.append((request.method, request.url, request.body))
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"code":200,"data":{"elements":[],"totalSize":0}}'
        resp.url = request.url
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
    session.post(f"{BASE}/v1/datareg/list", params={"page": 1, "size": 50}, json={})
    session.get(f"{BASE}/v1/datareg/getNotification", params={"dataRegNo": "X"})
    session.post(f"{BASE}/v1/datareg/confirmation", params={"dataRegNo": "X"}, json={})
    assert [m for m, _, _ in seen] == ["POST", "GET", "POST"]
    assert all(b in (None, b"{}") for _, _, b in seen)


# ---------------------------------------------------------------------------
# End-to-end: the real scraper + attachments code only ever emits whitelisted calls
# ---------------------------------------------------------------------------

def test_full_fetch_and_attachment_sync_only_issue_reads(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper, "_load_settings", lambda: {"base_url": BASE, "scraper": {}})
    monkeypatch.setattr(attachments, "_load_settings",
                        lambda: {"base_url": BASE, "attachments": {"enabled": True}})
    monkeypatch.setattr(attachments, "_ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    monkeypatch.setattr(attachments, "_download_file", lambda url, dest, timeout=60: False)

    seen = []

    def fake_send(self, request, **kw):
        seen.append((request.method, request.url, request.body))
        resp = requests.Response()
        resp.status_code = 200
        resp.url = request.url
        if "/list" in request.url:
            resp._content = (b'{"code":200,"data":{"elements":[{"dataRegNo":"R1",'
                             b'"dataRegAuditStatus":7}],"totalSize":1}}')
        elif "getNotification" in request.url:
            resp._content = b'{"code":200,"data":[{"dataMailType":"n","dataMailFileUrl":"https://oss/x.pdf"}]}'
        else:
            resp._content = (b'{"code":200,"data":{"systemConfirmation":{"confirmationUrl":"https://oss/c.pdf"},'
                             b'"nationalConfirmation":{"confirmationUrl":"https://oss/d.pdf"}}}')
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)

    records = scraper.fetch_all_records("tok").records
    attachments.sync_attachments("tok", "公司", records)

    assert seen, "expected platform calls"
    for method, url, body in seen:
        scraper.check_read_only(method, url, body, "www.zjip.org.cn")  # must not raise


def test_certificates_switch_disables_confirmation_call(monkeypatch, tmp_path):
    monkeypatch.setattr(attachments, "_load_settings",
                        lambda: {"base_url": BASE,
                                 "attachments": {"enabled": True, "certificates_enabled": False}})
    monkeypatch.setattr(scraper, "_load_settings", lambda: {"base_url": BASE})
    monkeypatch.setattr(attachments, "_ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(attachments, "_download_file", lambda url, dest, timeout=60: False)
    calls = []
    monkeypatch.setattr(attachments, "_fetch_notifications", lambda s, b, r: calls.append("notif") or [])
    monkeypatch.setattr(attachments, "_fetch_certificates", lambda s, b, r: calls.append("cert") or [])

    attachments.sync_attachments("tok", "公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 7}])
    assert calls == ["notif"]


# ---------------------------------------------------------------------------
# Browser: fill/click refuse to act anywhere but the login page
# ---------------------------------------------------------------------------

class _Page:
    def __init__(self, url):
        self.url = url
        self.actions = []

    def locator(self, sel):
        page = self

        class _Loc:
            first = property(lambda s: s)
            def count(self): return 1
            def fill(self, v): page.actions.append(("fill", sel))
            def click(self): page.actions.append(("click", sel))
        return _Loc()


def test_fill_and_click_refuse_off_login_page():
    page = _Page(f"{BASE}/dregister")  # already-authenticated business page
    with pytest.raises(LoginError):
        login._fill_login_form(page, "u", "p")
    with pytest.raises(LoginError):
        login._click_login_button(page)
    assert page.actions == []


def test_fill_and_click_work_on_login_page():
    page = _Page(f"{BASE}/user/login")
    login._fill_login_form(page, "u", "p")
    login._click_login_button(page)
    kinds = [k for k, _ in page.actions]
    assert kinds == ["fill", "fill", "click"]
    assert page.actions[-1][1] == 'button:has-text("登录")'  # semantic selector preferred
