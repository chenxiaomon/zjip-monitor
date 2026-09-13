"""The single gate in front of every route: web/app.py::_auth_middleware.

Public allow-list, session cookie, Basic-admin fallback, the X-API-Key lane
and the admin-only prefixes were all previously untested — a hole here is an
unauthenticated read of every company's registration data.
"""

from tests.conftest import ADMIN, API_KEY, GROUP_A_USER


def test_public_paths_need_no_auth(web_client):
    assert web_client.get("/login").status_code == 200
    assert web_client.get("/static/app.css").status_code == 200

    resp = web_client.get("/favicon.ico", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/static/favicon.svg"


def test_view_page_is_public(web_client, isolated_snapshots):
    assert web_client.get("/view/").status_code == 200
    assert web_client.get("/view/partial").status_code == 200


def test_console_redirects_to_login_without_cookie(web_client):
    resp = web_client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_htmx_poll_without_cookie_gets_hx_redirect(web_client):
    """A polling partial must not swap a login page into the fragment."""
    resp = web_client.get("/accounts/rows", headers={"HX-Request": "true"})
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/login"


def test_forged_session_cookie_is_rejected(web_client):
    web_client.cookies.set("szt_session", "admin|99999999999|deadbeef")
    resp = web_client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_group_user_is_forbidden_on_admin_pages(web_client, login_as, fake_accounts):
    login_as(web_client, *GROUP_A_USER)
    for path in ("/settings", "/notify", "/reports"):
        assert web_client.get(path).status_code == 403, path


def test_admin_can_open_admin_pages(
    web_client, login_as, isolated_settings, isolated_control_dir
):
    login_as(web_client, *ADMIN)
    assert web_client.get("/settings").status_code == 200


def test_api_requires_a_key(web_client, isolated_snapshots):
    resp = web_client.get("/api/status")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}

    assert web_client.get(
        "/api/status", headers={"X-API-Key": "wrong-key"}
    ).status_code == 401


def test_api_accepts_key_or_basic_admin(web_client, isolated_snapshots):
    assert web_client.get(
        "/api/status", headers={"X-API-Key": API_KEY}
    ).status_code == 200
    assert web_client.get("/api/status", auth=ADMIN).status_code == 200
    assert web_client.get("/api/status", auth=(ADMIN[0], "wrong")).status_code == 401
