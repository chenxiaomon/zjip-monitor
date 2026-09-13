"""The public /view board lists every monitored company with its case counts.

It is deliberately unauthenticated, but on a public IP with no robots rule that
is a client list anyone can read. VIEW_TOKEN makes the URL itself the
credential — and when it is unset the page must stay exactly as open as it was,
so nobody's existing link breaks on deploy.
"""

import pytest

from tests.conftest import ADMIN
from web import auth


@pytest.fixture
def view_token(monkeypatch):
    monkeypatch.setattr(auth, "VIEW_TOKEN", "s3cret-view-token")
    return "s3cret-view-token"


def test_without_a_configured_token_the_page_stays_open(web_client, isolated_snapshots):
    assert auth.VIEW_TOKEN == ""
    assert web_client.get("/view/").status_code == 200
    assert web_client.get("/view/partial").status_code == 200


def test_configured_token_hides_the_page(web_client, isolated_snapshots, view_token):
    resp = web_client.get("/view/")
    assert resp.status_code == 404  # not 403: don't confirm it exists
    assert web_client.get("/view/partial").status_code == 404
    assert web_client.get("/view/?t=wrong").status_code == 404


def test_correct_token_grants_access_and_mints_a_cookie(
    web_client, isolated_snapshots, view_token
):
    resp = web_client.get(f"/view/?t={view_token}")
    assert resp.status_code == 200
    assert auth.VIEW_COOKIE in resp.cookies

    # The cookie alone now works, including for the polling partial.
    assert web_client.get("/view/partial").status_code == 200


def test_forged_view_cookie_is_rejected(web_client, isolated_snapshots, view_token):
    web_client.cookies.set(auth.VIEW_COOKIE, "view|99999999999|deadbeef")
    assert web_client.get("/view/").status_code == 404


def test_expired_view_cookie_is_rejected(view_token, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    cookie = auth.make_view_cookie()
    assert auth.view_cookie_ok(cookie) is True

    monkeypatch.setattr(time, "time", lambda: 1_000_000.0 + auth.VIEW_COOKIE_MAX_AGE + 1)
    assert auth.view_cookie_ok(cookie) is False


def test_logged_in_console_user_needs_no_token(
    web_client, login_as, isolated_snapshots, view_token, fake_accounts
):
    login_as(web_client, *ADMIN)
    assert web_client.get("/view/").status_code == 200


def test_view_responses_are_not_indexable(web_client, isolated_snapshots):
    resp = web_client.get("/view/")
    assert resp.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert 'name="robots"' in resp.text


def test_robots_txt_disallows_everything(web_client):
    resp = web_client.get("/robots.txt")
    assert resp.status_code == 200
    assert "Disallow: /" in resp.text
