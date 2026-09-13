"""Cross-site state changes and the fail-closed auth defaults.

SameSite=Lax already blocks cross-site form POSTs that rely on the session
cookie — but the Basic-Auth admin fallback is not bound by SameSite, so a
browser that has cached those credentials could be made to POST
/accounts/{u}/delete from any page.
"""

from types import SimpleNamespace

import pytest

from tests.conftest import ADMIN, API_KEY
from web import app as web_app
from web import auth as web_auth


def test_cross_site_post_is_refused(web_client, login_as, fake_accounts):
    login_as(web_client, *ADMIN)
    resp = web_client.post(
        "/accounts/ua1/relogin", headers={"Origin": "http://evil.test"}
    )
    assert resp.status_code == 403
    assert "跨站" in resp.text


def test_same_origin_post_is_allowed(web_client, login_as, fake_accounts, isolated_snapshots):
    login_as(web_client, *ADMIN)
    resp = web_client.post(
        "/accounts/does-not-exist/relogin", headers={"Origin": "http://testserver"}
    )
    assert resp.status_code == 200  # reached the handler


def test_referer_is_used_when_origin_is_absent(web_client, login_as, fake_accounts):
    login_as(web_client, *ADMIN)
    assert web_client.post(
        "/accounts/ua1/relogin", headers={"Referer": "http://evil.test/page"}
    ).status_code == 403
    assert web_client.post(
        "/accounts/nope/relogin", headers={"Referer": "http://testserver/accounts"}
    ).status_code == 200


def test_opaque_origin_is_refused(web_client, login_as, fake_accounts):
    """A sandboxed iframe or a redirect-laundered request sends Origin: null."""
    login_as(web_client, *ADMIN)
    assert web_client.post(
        "/accounts/ua1/relogin", headers={"Origin": "null"}
    ).status_code == 403


def test_script_without_browser_headers_still_works(web_client, login_as, fake_accounts):
    """curl and deploy scripts send neither header; they are not confused
    deputies and must not be locked out."""
    login_as(web_client, *ADMIN)
    assert web_client.post("/accounts/nope/relogin").status_code == 200


def test_extra_allowed_origin_can_be_configured(web_client, login_as, fake_accounts, monkeypatch):
    monkeypatch.setattr(web_app, "_ALLOWED_ORIGINS", {"console.internal"})
    login_as(web_client, *ADMIN)
    assert web_client.post(
        "/accounts/nope/relogin", headers={"Origin": "http://console.internal"}
    ).status_code == 200


def test_api_lane_is_not_affected(web_client, isolated_snapshots):
    """/api/* authenticates with a header a cross-site page cannot set."""
    resp = web_client.get(
        "/api/status", headers={"X-API-Key": API_KEY, "Origin": "http://evil.test"}
    )
    assert resp.status_code == 200


def test_logout_requires_post(web_client, login_as, fake_accounts):
    login_as(web_client, *ADMIN)
    assert web_client.get("/logout", follow_redirects=False).status_code == 405

    resp = web_client.post(
        "/logout", headers={"Origin": "http://testserver"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "szt_session=" in resp.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# fail-closed defaults
# ---------------------------------------------------------------------------

def _anonymous_request():
    return SimpleNamespace(state=SimpleNamespace())


def test_unidentified_caller_is_not_an_admin():
    assert web_auth.is_admin(_anonymous_request()) is False
    with pytest.raises(Exception):
        web_auth.require_admin(_anonymous_request())


def test_unidentified_caller_sees_no_accounts(fake_accounts):
    req = _anonymous_request()
    assert web_auth.visible_accounts(req) == []
    assert web_auth.allowed_usernames(req) == set()
    assert web_auth.allowed_companies(req) == set()
