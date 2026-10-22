"""The 用户 page (/users) and 修改密码 (/me/password)."""

import json

import pytest

from tests.conftest import ADMIN, GROUP_A_USER, OPERATOR_A
from web import login_guard
from web.services import users


def _audit(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


def _client():
    from fastapi.testclient import TestClient

    from web.app import app

    return TestClient(app)


@pytest.fixture
def admin(web_client, login_as, isolated_settings):
    return login_as(web_client, *ADMIN)


def _make_file_admin(name="boss", password="boss-password"):
    users.create_user(name, display_name="老板", role="admin", groups=[], password=password,
                      by="t", must_change=False)
    return name, password


# --- access --------------------------------------------------------------------

@pytest.mark.parametrize("who", [GROUP_A_USER, OPERATOR_A])
@pytest.mark.parametrize("method,path", [
    ("get", "/users"), ("get", "/users/rows"), ("get", "/users/new-form"),
    ("post", "/users"), ("get", "/users/ga/edit-form"), ("post", "/users/ga/edit"),
    ("post", "/users/ga/password"), ("post", "/users/ga/disable"), ("post", "/users/ga/delete"),
    ("get", "/audit"),
])
def test_only_admins_reach_user_management(web_client, login_as, who, method, path):
    login_as(web_client, *who)
    assert getattr(web_client, method)(path).status_code == 403


def test_non_admins_see_no_users_link(web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    body = web_client.get("/").text
    assert 'href="/users"' not in body and 'href="/audit"' not in body
    assert 'href="/me/password"' in body


def test_the_page_lists_file_users_and_the_env_admin_read_only(admin):
    body = admin.get("/users").text
    assert ".env 备用管理员" in body
    assert 'id="user-opa"' in body and 'id="user-ga"' in body
    assert f'/users/{ADMIN[0]}/' not in body  # no actions for the .env admin


# --- create ----------------------------------------------------------------------

def test_admin_creates_a_user_who_can_then_log_in(admin, _isolated_audit, login_as):
    resp = admin.post("/users", data={
        "username": "ZhangSan", "display_name": "张三", "role": "operator",
        "groups": ["group_a", "group_b"], "password": "first-password",
    })
    assert resp.status_code == 200 and resp.headers["HX-Trigger"] == "userSaved"
    rec = users.get("zhangsan")
    assert rec["role"] == "operator" and rec["groups"] == ["group_a", "group_b"]
    assert rec["must_change_password"] is True

    colleague = login_as(_client(), "zhangsan", "first-password")
    assert "请修改初始口令" in colleague.get("/").text

    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] == "user.create"]
    assert entry["actor"] == ADMIN[0] and entry["target"] == "zhangsan"
    assert entry["detail"] == {"role": "operator", "groups": ["group_a", "group_b"]}
    assert "first-password" not in json.dumps(_audit(_isolated_audit))


@pytest.mark.parametrize("data,msg", [
    ({"username": "x", "password": "long-enough-pw"}, "用户名"),
    ({"username": "lisi", "password": "short"}, "至少"),
    ({"username": "lisi", "password": "long-enough-pw", "groups": []}, "分组"),
    ({"username": "lisi", "password": "long-enough-pw", "groups": ["nope"]}, "不存在"),
    ({"username": "opa", "password": "long-enough-pw"}, "已存在"),
])
def test_create_errors_come_back_in_the_form(admin, _isolated_audit, data, msg):
    data = {"role": "viewer", "groups": ["group_a"], **data}
    resp = admin.post("/users", data=data)
    assert resp.status_code == 200 and msg in resp.text
    assert resp.headers["HX-Retarget"] == "#modal-container"
    assert "long-enough-pw" not in resp.text
    assert users.get("lisi") is None
    assert not [e for e in _audit(_isolated_audit) if e["action"].startswith(("user.", "http."))]


# --- edit ------------------------------------------------------------------------

def test_admin_edits_role_and_groups(admin, _isolated_audit):
    resp = admin.post("/users/opa/edit", data={
        "display_name": "小王", "role": "viewer", "groups": ["group_b"],
    })
    assert resp.headers["HX-Trigger"] == "userSaved"
    rec = users.get("opa")
    assert (rec["display_name"], rec["role"], rec["groups"]) == ("小王", "viewer", ["group_b"])
    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] == "user.update"]
    assert entry["detail"]["role"] == ["operator", "viewer"]


def test_a_group_settings_no_longer_lists_is_kept(admin):
    users.update_user("opa", display_name="", role="operator", groups=["group_a", "legacy"])
    form = admin.get("/users/opa/edit-form").text
    assert "legacy（设置中已无此分组）" in form
    admin.post("/users/opa/edit", data={"display_name": "", "role": "operator",
                                        "groups": ["group_a", "legacy"]})
    assert users.get("opa")["groups"] == ["group_a", "legacy"]


def test_an_admin_cannot_demote_disable_or_delete_themselves(web_client, login_as, isolated_settings):
    name, password = _make_file_admin()
    login_as(web_client, name, password)
    resp = web_client.post(f"/users/{name}/edit", data={"display_name": "", "role": "viewer",
                                                        "groups": ["group_a"]})
    assert "不能修改自己的角色" in resp.text
    for action in ("disable", "revoke", "delete"):
        resp = web_client.post(f"/users/{name}/{action}")
        assert resp.status_code == 400 and "自己" in resp.text
    assert users.get(name)["role"] == "admin" and not users.get(name)["disabled"]


# --- password reset / disable / revoke / delete ------------------------------------

def test_password_reset_signs_the_user_out(admin, login_as, _isolated_audit):
    colleague = login_as(_client(), *OPERATOR_A)
    resp = admin.post("/users/opa/password", data={
        "password": "reset-password", "password2": "reset-password", "must_change": "on",
    })
    assert resp.headers["HX-Trigger"] == "userSaved"
    assert colleague.get("/", follow_redirects=False).status_code == 302
    login_as(_client(), "opa", "reset-password")
    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] == "user.password_reset"]
    assert entry["detail"] == {"must_change": True}
    assert "reset-password" not in json.dumps(_audit(_isolated_audit))


def test_password_reset_needs_matching_entries(admin):
    resp = admin.post("/users/opa/password", data={"password": "aaaaaaaaaaaa", "password2": "b"})
    assert "不一致" in resp.text
    assert users.verify_password(OPERATOR_A[1], users.get("opa")["password_hash"])


@pytest.mark.parametrize("action,check", [
    ("disable", lambda: users.get("opa")["disabled"]),
    ("revoke", lambda: users.get("opa") is not None),
    ("delete", lambda: users.get("opa") is None),
])
def test_row_actions(admin, login_as, _isolated_audit, action, check):
    colleague = login_as(_client(), *OPERATOR_A)
    resp = admin.post(f"/users/opa/{action}")
    assert resp.status_code == 200 and check()
    assert colleague.get("/", follow_redirects=False).status_code == 302
    assert [e["action"] for e in _audit(_isolated_audit)][-1] == f"user.{action}"


def test_enable_lets_a_disabled_user_back_in(admin, login_as):
    users.set_disabled("opa", True)
    row = admin.post("/users/opa/enable").text
    assert 'hx-post="/users/opa/disable"' in row  # the row swaps back to its enabled form
    assert not users.get("opa")["disabled"]
    login_as(_client(), *OPERATOR_A)


def test_unknown_users_are_404(admin):
    assert admin.post("/users/ghost/disable").status_code == 404
    assert admin.get("/users/ghost/edit-form").status_code == 404


def test_an_unreadable_file_is_reported_and_never_overwritten(admin, _isolated_users):
    _isolated_users.write_text("users: [broken", encoding="utf-8")
    users.invalidate()
    before = _isolated_users.read_bytes()
    assert "用户文件无法读取" in admin.get("/users").text
    resp = admin.post("/users", data={"username": "lisi", "role": "viewer",
                                      "groups": ["group_a"], "password": "long-enough-pw"})
    assert "未保存" in resp.text
    assert _isolated_users.read_bytes() == before


# --- /me/password --------------------------------------------------------------------

def test_change_own_password_keeps_this_session_and_ends_the_others(
        web_client, login_as, _isolated_audit):
    login_as(web_client, *OPERATOR_A)
    other_device = login_as(_client(), *OPERATOR_A)
    resp = web_client.post("/me/password", data={
        "current": OPERATOR_A[1], "password": "my-new-password", "password2": "my-new-password",
    })
    assert resp.status_code == 200 and "口令已修改" in resp.text
    assert web_client.get("/", follow_redirects=False).status_code == 200
    assert other_device.get("/", follow_redirects=False).status_code == 302
    assert users.get("opa")["must_change_password"] is False
    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] == "self.password_change"]
    assert entry["actor"] == "opa" and entry["ok"] is True
    assert "my-new-password" not in json.dumps(_audit(_isolated_audit))


def test_change_own_password_needs_the_current_one(web_client, login_as, _isolated_audit):
    login_as(web_client, *OPERATOR_A)
    resp = web_client.post("/me/password", data={
        "current": "wrong-current", "password": "my-new-password", "password2": "my-new-password",
    })
    assert "当前口令不对" in resp.text
    assert users.verify_password(OPERATOR_A[1], users.get("opa")["password_hash"])
    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] == "self.password_change"]
    assert entry["ok"] is False


def test_guessing_the_current_password_hits_the_login_lockout(web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    for _ in range(login_guard.MAX_FAILURES):
        web_client.post("/me/password", data={"current": "guess", "password": "x" * 12,
                                               "password2": "x" * 12})
    resp = web_client.post("/me/password", data={
        "current": OPERATOR_A[1], "password": "my-new-password", "password2": "my-new-password",
    })
    assert resp.status_code == 429


def test_the_env_admin_is_pointed_at_the_env_file(web_client, login_as):
    login_as(web_client, *ADMIN)
    assert "WEB_PASS" in web_client.get("/me/password").text
    resp = web_client.post("/me/password", data={"current": ADMIN[1], "password": "x" * 12,
                                                 "password2": "x" * 12})
    assert resp.status_code == 400
