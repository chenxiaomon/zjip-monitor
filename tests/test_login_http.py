"""HTTP login (POST /v1/user/login) and its browser fallback.

Contract captured against the live platform on 2026-09-06:
  POST /v1/user/login  {"account": ..., "password": md5hex(password)}
  → {"code": 0, "data": "<jwt>", "message": "成功"}
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest
import requests

from src import login, scraper
from src.exceptions import LoginError

BASE = "https://www.zjip.org.cn"
ACCOUNT = "13800000000"
PASSWORD = "s3cret-Pa55"
PASSWORD_MD5 = hashlib.md5(PASSWORD.encode()).hexdigest()


def _jwt(exp_offset: int = 86400) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": int(time.time()), "exp": int(time.time()) + exp_offset}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.signature-signature-signature"


TOKEN = _jwt()


def _response(request, status=200, body=b"", content_type="application/json"):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    resp.url = request.url
    resp.headers["Content-Type"] = content_type
    resp.request = request
    return resp


@pytest.fixture
def settings(monkeypatch, tmp_path):
    """Isolated sessions dir + mutable settings dict the tests can tweak."""
    cfg = {"base_url": BASE, "login": {"method": "auto", "headless": True}}
    monkeypatch.setattr(login, "_load_settings", lambda: cfg)
    monkeypatch.setattr(login, "_SESSIONS_DIR", tmp_path / "sessions")
    return cfg


@pytest.fixture
def browser_spy(monkeypatch):
    """Replace the Playwright path with a recorder so tests can assert whether
    the fallback fired (and no real browser is ever launched)."""
    calls: list[tuple[str, str]] = []

    def fake_browser(account, password):
        calls.append((account, password))
        return "browser-token.x.y"

    monkeypatch.setattr(login, "_login_via_browser", fake_browser)
    return calls


def _wire(monkeypatch, handler):
    """Route every HTTPAdapter.send through `handler(request) -> Response`."""
    seen: list[requests.PreparedRequest] = []

    def fake_send(self, request, **kw):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
    return seen


def _ok(request):
    return _response(request, body=json.dumps({"code": 0, "data": TOKEN, "message": "成功"}).encode())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_http_login_sends_md5_password_and_caches_token(settings, browser_spy, monkeypatch):
    seen = _wire(monkeypatch, _ok)

    token = login.get_token(ACCOUNT, PASSWORD, force_refresh=True)

    assert token == TOKEN
    assert browser_spy == [], "browser must not launch when HTTP login succeeds"
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url == f"{BASE}/v1/user/login"
    raw = req.body.decode() if isinstance(req.body, bytes) else req.body
    assert json.loads(raw) == {"account": ACCOUNT, "password": PASSWORD_MD5}
    assert PASSWORD not in raw, "plaintext password must never leave the process"
    assert "x-access-token" not in {k.lower() for k in req.headers}

    # Token is cached with the JWT exp, and the next call reuses it without any request
    cached = json.loads(login._session_path(ACCOUNT).read_text(encoding="utf-8"))
    assert cached["token"] == TOKEN
    assert login.get_token(ACCOUNT, PASSWORD) == TOKEN
    assert len(seen) == 1


def test_http_login_accepts_code_200_variant(settings, browser_spy, monkeypatch):
    _wire(monkeypatch, lambda r: _response(r, body=json.dumps({"code": 200, "data": TOKEN}).encode()))
    assert login.get_token(ACCOUNT, PASSWORD, force_refresh=True) == TOKEN
    assert browser_spy == []


# ---------------------------------------------------------------------------
# Rejected credentials: surfaced as LoginError, never retried in a browser
# ---------------------------------------------------------------------------

def test_rejected_credentials_raise_login_error_without_browser(settings, browser_spy, monkeypatch):
    _wire(monkeypatch, lambda r: _response(
        r, body=json.dumps({"code": 1001, "data": None, "message": "账号或密码错误"}).encode()
    ))
    with pytest.raises(LoginError, match="账号或密码错误"):
        login.get_token(ACCOUNT, PASSWORD, force_refresh=True)
    assert browser_spy == []
    assert not login._session_path(ACCOUNT).exists()


# ---------------------------------------------------------------------------
# Transport / shape problems: fall back (auto) or fail loudly (http)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("handler", [
    pytest.param(lambda r: (_ for _ in ()).throw(requests.ConnectionError("dns")), id="network-error"),
    pytest.param(lambda r: _response(r, status=502, body=b"Bad Gateway", content_type="text/html"), id="http-502"),
    pytest.param(lambda r: _response(r, body=b"<html>maintenance</html>", content_type="text/html"), id="non-json"),
    pytest.param(lambda r: _response(r, body=json.dumps({"code": 0, "data": {"token": "x"}}).encode()), id="reshaped-data"),
    pytest.param(lambda r: _response(r, body=json.dumps({"status": "ok"}).encode()), id="no-code-field"),
    pytest.param(lambda r: _response(r, body=json.dumps({"code": 0, "data": None}).encode()), id="code0-data-none"),
    pytest.param(lambda r: (_ for _ in ()).throw(requests.ReadTimeout("read timed out")), id="read-timeout"),
])
def test_auto_falls_back_to_browser_on_transport_problems(settings, browser_spy, monkeypatch, handler):
    _wire(monkeypatch, handler)
    token = login.get_token(ACCOUNT, PASSWORD, force_refresh=True)
    assert token == "browser-token.x.y"
    assert browser_spy == [(ACCOUNT, PASSWORD)]


def test_redirect_is_not_followed_and_falls_back(settings, browser_spy, monkeypatch):
    """A 302 must not be followed (it would become a GET the guard rejects);
    it is reported as unavailable and handed to the browser fallback."""
    def redirect(request):
        resp = _response(request, status=302, body=b"", content_type="text/html")
        resp.headers["Location"] = f"{BASE}/maintenance"
        return resp

    seen = _wire(monkeypatch, redirect)
    assert login.get_token(ACCOUNT, PASSWORD, force_refresh=True) == "browser-token.x.y"
    assert [r.path_url for r in seen] == ["/v1/user/login"], "redirect target must never be requested"
    assert browser_spy == [(ACCOUNT, PASSWORD)]


def test_method_http_never_falls_back(settings, browser_spy, monkeypatch):
    settings["login"]["method"] = "http"
    _wire(monkeypatch, lambda r: (_ for _ in ()).throw(requests.ConnectionError("dns")))
    with pytest.raises(LoginError, match="HTTP login unavailable"):
        login.get_token(ACCOUNT, PASSWORD, force_refresh=True)
    assert browser_spy == []


def test_method_browser_skips_http_entirely(settings, browser_spy, monkeypatch):
    settings["login"]["method"] = "browser"
    seen = _wire(monkeypatch, _ok)
    assert login.get_token(ACCOUNT, PASSWORD, force_refresh=True) == "browser-token.x.y"
    assert seen == []
    assert browser_spy == [(ACCOUNT, PASSWORD)]


def test_unknown_method_is_treated_as_auto(settings, browser_spy, monkeypatch):
    settings["login"]["method"] = "telnet"
    _wire(monkeypatch, _ok)
    assert login.get_token(ACCOUNT, PASSWORD, force_refresh=True) == TOKEN
    assert browser_spy == []


# ---------------------------------------------------------------------------
# Login-only guard: the login session can send exactly one kind of request
# ---------------------------------------------------------------------------

def _no_network(monkeypatch):
    def boom(self, request, **kw):
        raise AssertionError(f"request escaped the login guard: {request.method} {request.url}")
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", boom)


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/v1/user/login", None),
    ("POST", "/v1/user/userInfo", b'{"account":"a","password":"b"}'),
    ("POST", "/v1/datareg/list", b"{}"),
    ("POST", "/v1/user/login", b"{}"),
    ("POST", "/v1/user/login", b'{"account":"a"}'),
    ("POST", "/v1/user/login", b'{"account":"a","password":"b","captcha":"1"}'),
    ("POST", "/v1/user/login", b"account=a&password=b"),
    ("POST", "/v1/user/login", None),
])
def test_login_guard_blocks_everything_but_the_login_post(method, path, body):
    with pytest.raises(scraper.ReadOnlyViolation):
        login.check_login_only(method, f"{BASE}{path}", body, "www.zjip.org.cn")


def test_login_guard_blocks_other_hosts():
    with pytest.raises(scraper.ReadOnlyViolation):
        login.check_login_only(
            "POST", "https://attacker.example.com/v1/user/login",
            b'{"account":"a","password":"b"}', "www.zjip.org.cn",
        )


def test_login_guard_permits_the_login_post():
    login.check_login_only(
        "POST", f"{BASE}/v1/user/login", b'{"account":"a","password":"b"}', "www.zjip.org.cn"
    )
    login.check_login_only(  # str body, key order irrelevant
        "POST", f"{BASE}/v1/user/login", '{"password":"b","account":"a"}', "www.zjip.org.cn"
    )


def test_login_session_enforces_guard_before_the_wire(monkeypatch):
    _no_network(monkeypatch)
    sess = login._build_login_session(BASE)
    try:
        with pytest.raises(scraper.ReadOnlyViolation):
            sess.get(f"{BASE}/v1/user/userInfo")
        with pytest.raises(scraper.ReadOnlyViolation):
            sess.post(f"{BASE}/v1/datareg/submit", json={"account": "a", "password": "b"})
        with pytest.raises(scraper.ReadOnlyViolation):
            sess.post(f"{BASE}/v1/user/login", json={"account": "a", "password": "b", "extra": 1})
        with pytest.raises(scraper.ReadOnlyViolation):
            sess.post("https://evil.example.com/v1/user/login", json={"account": "a", "password": "b"})
    finally:
        sess.close()


def test_real_http_login_path_only_emits_the_login_post(settings, browser_spy, monkeypatch):
    """End-to-end through _login_via_http: exactly one request, and it is the guarded one."""
    seen = _wire(monkeypatch, _ok)
    login._login_via_http(ACCOUNT, PASSWORD, BASE)
    assert [(r.method, r.path_url) for r in seen] == [("POST", "/v1/user/login")]
    login.check_login_only(seen[0].method, seen[0].url, seen[0].body, "www.zjip.org.cn")


# ---------------------------------------------------------------------------
# verify_credentials: fresh login with ZERO token-cache interaction
# ---------------------------------------------------------------------------

def test_verify_credentials_never_touches_token_cache(settings, browser_spy, monkeypatch):
    seen = _wire(monkeypatch, _ok)
    token = login.verify_credentials(ACCOUNT, PASSWORD)
    assert token == TOKEN
    assert len(seen) == 1
    assert not login._session_path(ACCOUNT).exists(), \
        "verify must not cache — an unsaved password's token would mask a wrong stored one"


def test_verify_credentials_bypasses_existing_cache(settings, browser_spy, monkeypatch):
    seen = _wire(monkeypatch, _ok)
    # Prime a valid cached token the way a normal login would
    login._SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    login._save_token(ACCOUNT, TOKEN)
    cached_before = login._session_path(ACCOUNT).read_text(encoding="utf-8")

    login.verify_credentials(ACCOUNT, PASSWORD)

    assert len(seen) == 1, "must hit the platform even with a valid cache"
    assert login._session_path(ACCOUNT).read_text(encoding="utf-8") == cached_before, \
        "verify must not rewrite the cached token"


def test_verify_credentials_rejection_is_typed_with_platform_message(settings, browser_spy, monkeypatch):
    from src.exceptions import CredentialsRejectedError

    _wire(monkeypatch, lambda r: _response(
        r, body=json.dumps({"code": 1001, "data": None, "message": "账号或密码错误"}).encode()
    ))
    with pytest.raises(CredentialsRejectedError, match="账号或密码错误"):
        login.verify_credentials(ACCOUNT, PASSWORD)
    assert browser_spy == [], "rejected credentials must not retry in a browser"
    assert not login._session_path(ACCOUNT).exists()


def test_verify_credentials_sends_md5_only(settings, browser_spy, monkeypatch):
    seen = _wire(monkeypatch, _ok)
    login.verify_credentials(ACCOUNT, PASSWORD)
    raw = seen[0].body.decode() if isinstance(seen[0].body, bytes) else seen[0].body
    assert PASSWORD not in raw
    assert json.loads(raw)["password"] == PASSWORD_MD5


# ---------------------------------------------------------------------------
# Rejection classification: only genuine credential rejections are typed as
# CredentialsRejectedError (they feed the lockout streak) — server-side
# application errors stay plain LoginError
# ---------------------------------------------------------------------------

def test_server_app_error_is_not_a_credential_rejection(settings, browser_spy, monkeypatch):
    from src.exceptions import CredentialsRejectedError

    _wire(monkeypatch, lambda r: _response(
        r, body=json.dumps({"code": 500, "data": None, "message": "系统繁忙，请稍后再试"}).encode()
    ))
    with pytest.raises(LoginError, match="系统繁忙") as excinfo:
        login.get_token(ACCOUNT, PASSWORD, force_refresh=True)
    assert not isinstance(excinfo.value, CredentialsRejectedError)
    assert browser_spy == [], "same envelope would come back — no browser retry"
    assert not login._session_path(ACCOUNT).exists()


def test_rejection_by_message_pattern_without_known_code(settings, browser_spy, monkeypatch):
    from src.exceptions import CredentialsRejectedError

    _wire(monkeypatch, lambda r: _response(
        r, body=json.dumps({"code": 4007, "data": None, "message": "密码错误次数过多，账号已锁定"}).encode()
    ))
    with pytest.raises(CredentialsRejectedError, match="锁定"):
        login.get_token(ACCOUNT, PASSWORD, force_refresh=True)
    assert browser_spy == []


# ---------------------------------------------------------------------------
# Platform-side refusals (captcha / throttled / maintenance): not our password,
# so they must not feed the lockout streak — but they get their own type so the
# console can say what the operator should (not) do about it.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message,reason",
    [
        ("请输入验证码", "captcha"),
        ("操作过于频繁，请稍后再试", "throttled"),
        ("系统繁忙，请稍后再试", "maintenance"),
        ("平台维护中", "maintenance"),
    ],
)
def test_platform_refusal_is_typed_with_its_reason(
    settings, browser_spy, monkeypatch, message, reason
):
    from src.exceptions import CredentialsRejectedError, PlatformUnavailableError

    _wire(monkeypatch, lambda r: _response(
        r, body=json.dumps({"code": 500, "data": None, "message": message}).encode()
    ))
    with pytest.raises(PlatformUnavailableError) as excinfo:
        login.get_token(ACCOUNT, PASSWORD, force_refresh=True)

    assert excinfo.value.reason == reason
    assert message in str(excinfo.value)
    assert not isinstance(excinfo.value, CredentialsRejectedError)
    assert browser_spy == [], "a throttled platform must not also get a Chromium hit"
    assert not login._session_path(ACCOUNT).exists()


def test_unrecognised_platform_code_stays_a_plain_login_error(settings, monkeypatch):
    """Only messages we can actually classify get the specific type; an unknown
    code must not be relabelled as "平台暂时不可用" on a guess."""
    from src.exceptions import PlatformUnavailableError

    _wire(monkeypatch, lambda r: _response(
        r, body=json.dumps({"code": 9999, "data": None, "message": "unexpected"}).encode()
    ))
    with pytest.raises(LoginError) as excinfo:
        login.get_token(ACCOUNT, PASSWORD, force_refresh=True)
    assert not isinstance(excinfo.value, PlatformUnavailableError)
