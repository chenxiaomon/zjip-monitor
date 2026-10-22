"""Console sessions on top of config/users.yaml: who can log in, and when an
issued cookie stops working."""

import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest
import yaml

from tests.conftest import ADMIN, GROUP_A_USER, OPERATOR_A
from web import auth, sse
from web.routes import accounts as accounts_routes
from web.services import users


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    monkeypatch.setattr(accounts_routes, "spawn", lambda coro: coro.close())


def _fresh_client():
    from fastapi.testclient import TestClient

    from web.app import app

    return TestClient(app)


def _bounced(client):
    resp = client.get("/", follow_redirects=False)
    return resp.status_code == 302 and resp.headers["location"] == "/login"


def test_a_file_user_logs_in_and_sees_their_role(web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    body = web_client.get("/").text
    assert "经办人" in body and "group_a" in body


def test_usernames_are_case_insensitive(web_client, login_as):
    login_as(web_client, "OPA", OPERATOR_A[1])
    assert not _bounced(web_client)


def test_a_disabled_user_is_told_so_only_with_the_right_password(web_client):
    users.set_disabled("opa", True)
    wrong = web_client.post("/login", data={"username": "opa", "password": "nope-nope-nope"},
                            follow_redirects=False)
    assert wrong.status_code == 401 and "已停用" not in wrong.text
    right = web_client.post("/login", data={"username": "opa", "password": OPERATOR_A[1]},
                            follow_redirects=False)
    assert right.status_code == 403 and "已停用" in right.text


@pytest.mark.parametrize("username", ["nobody", ADMIN[0], "opa"])
def test_every_attempt_costs_exactly_one_hash(web_client, monkeypatch, username):
    """Response time must not reveal which usernames exist."""
    calls = []
    real_dummy, real_verify = users.dummy_verify, users.verify_password
    monkeypatch.setattr(users, "dummy_verify", lambda pw: calls.append("dummy") or real_dummy(pw))
    monkeypatch.setattr(users, "verify_password",
                        lambda pw, h: calls.append("verify") or real_verify(pw, h))
    web_client.post("/login", data={"username": username, "password": "wrong-password"},
                    follow_redirects=False)
    assert len(calls) == 1


def test_password_reset_signs_out_every_open_session(web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    other_device = login_as(_fresh_client(), *OPERATOR_A)
    users.set_password("opa", "brand-new-password")

    assert _bounced(web_client) and _bounced(other_device)
    hx = web_client.get("/partials/dashboard", headers={"HX-Request": "true"})
    assert hx.status_code == 401 and hx.headers["HX-Redirect"] == "/login"
    login_as(_fresh_client(), "opa", "brand-new-password")


@pytest.mark.parametrize("change", [
    lambda: users.revoke_sessions("opa"),
    lambda: users.set_disabled("opa", True),
    lambda: users.delete_user("opa"),
    lambda: users.update_user("opa", display_name="", role="viewer", groups=["group_a"]),
])
def test_revocations(web_client, login_as, change):
    login_as(web_client, *OPERATOR_A)
    assert not _bounced(web_client)
    change()
    assert _bounced(web_client)


def test_a_groups_change_applies_without_a_new_login(web_client, login_as, fake_accounts):
    login_as(web_client, *GROUP_A_USER)
    assert "丙公司" not in web_client.get("/accounts").text
    users.update_user("ga", display_name="", role="viewer", groups=["group_a", "group_b"])
    body = web_client.get("/accounts").text
    assert "丙公司" in body and "甲 公司" in body


def test_imported_users_keep_their_pre_users_yaml_cookie():
    """WEB_USERS users were imported with an empty session_key: the cookie
    they already hold was signed over the bare "user|expiry" — it must still
    be accepted after the deploy."""
    expiry = int(time.time()) + 3600
    payload = f"ga|{expiry}"
    sig = hmac.new(b"test-session-secret", payload.encode(), hashlib.sha256).hexdigest()
    user = auth.user_from_cookie(f"{payload}|{sig}")
    assert user["username"] == "ga" and user["role"] == "viewer" and user["groups"] == ["group_a"]


def test_a_cookie_cannot_be_moved_to_another_user():
    users.create_user("zhangsan", display_name="", role="viewer", groups=["group_a"],
                      password="correct-horse", by="t")
    cookie = auth.make_cookie("zhangsan")
    name, expiry, sig = cookie.rsplit("|", 2)
    assert auth.user_from_cookie(f"opa|{expiry}|{sig}") is None
    assert auth.user_from_cookie(f"view|{expiry}|{sig}") is None


def test_an_unreadable_user_file_leaves_only_the_env_admin(web_client, login_as, _isolated_users):
    _isolated_users.write_text("users: [broken", encoding="utf-8")
    users.invalidate()
    resp = web_client.post("/login", data={"username": "opa", "password": OPERATOR_A[1]},
                           follow_redirects=False)
    assert resp.status_code == 401
    login_as(web_client, *ADMIN)


def test_a_file_user_named_like_the_env_admin_is_ignored(_isolated_users):
    doc = yaml.safe_load(_isolated_users.read_text(encoding="utf-8"))
    doc["users"][ADMIN[0]] = dict(doc["users"]["opa"], role="viewer")
    _isolated_users.write_text(yaml.safe_dump(doc), encoding="utf-8")
    users.invalidate()
    assert auth.authenticate(ADMIN[0], OPERATOR_A[1]) == (None, "bad")
    assert auth.authenticate(*ADMIN) == (ADMIN[0], "ok")


def test_overlong_passwords_are_refused_before_hashing(monkeypatch):
    monkeypatch.setattr(users, "verify_password", lambda *a: pytest.fail("hashed"))
    monkeypatch.setattr(users, "dummy_verify", lambda *a: pytest.fail("hashed"))
    assert auth.authenticate("opa", "x" * 10_000) == (None, "bad")


# --- SSE ---------------------------------------------------------------------

def _sse_request(user, cookie=None):
    return SimpleNamespace(state=SimpleNamespace(user=user),
                           cookies={auth.COOKIE_NAME: cookie} if cookie else {})


def test_sse_stream_notices_a_revoked_session():
    cookie = auth.make_cookie("opa")
    req = _sse_request(auth.user_from_cookie(cookie), cookie)
    assert sse._still_signed_in(req)

    users.update_user("opa", display_name="", role="operator", groups=["group_a", "group_b"])
    assert sse._still_signed_in(req)
    assert req.state.user["groups"] == ["group_a", "group_b"]  # refreshed for the filter

    users.revoke_sessions("opa")
    assert not sse._still_signed_in(req)


def test_sse_basic_auth_callers_have_nothing_to_recheck():
    assert sse._still_signed_in(_sse_request(auth.admin_user(via="basic")))
    assert not sse._still_signed_in(_sse_request(None))


def test_startup_imports_web_users_once(_isolated_users, monkeypatch):
    import asyncio

    from web import app as web_app

    _isolated_users.unlink()
    users.invalidate()
    monkeypatch.setenv("WEB_USERS", "legacy:old-pass:group_a")
    asyncio.run(web_app._migrate_users())
    assert auth.authenticate("legacy", "old-pass") == ("legacy", "ok")
