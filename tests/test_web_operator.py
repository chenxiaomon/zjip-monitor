"""经办人 (operator): own groups' platform password and contact, 测试登录 —
nothing else — and the per-account limit on console-started platform logins."""

import pytest

from tests.conftest import ADMIN, GROUP_A_USER, OPERATOR_A
from web.routes import accounts as accounts_routes
from web.services import users


@pytest.fixture(autouse=True)
def _no_platform(monkeypatch):
    """Never launch a real platform login; record what would have run."""
    spawned, verified = [], []
    monkeypatch.setattr(accounts_routes, "spawn", lambda coro: spawned.append(coro) or coro.close())
    monkeypatch.setattr(accounts_routes, "verify_credentials",
                        lambda u, p: verified.append(u) or "token")
    accounts_routes._web_logins.clear()
    accounts_routes._tasks.clear()
    yield {"spawned": spawned, "verified": verified}
    accounts_routes._web_logins.clear()
    accounts_routes._tasks.clear()


@pytest.fixture
def saved(monkeypatch, fake_accounts):
    """Capture save_accounts() instead of writing accounts.enc."""
    out = []
    monkeypatch.setattr(accounts_routes, "save_accounts", lambda accts: out.append(
        {a.username: (a.password, a.contact, a.group) for a in accts}))
    return out


@pytest.fixture
def operator(web_client, login_as, isolated_settings, isolated_snapshots, fake_accounts):
    return login_as(web_client, *OPERATOR_A)


# --- what an operator can do -------------------------------------------------------

def test_operator_sees_edit_but_not_delete_or_scan(operator):
    body = operator.get("/accounts").text
    assert "/accounts/ua1/edit-form" in body
    assert "/accounts/ua1/delete" not in body and "/accounts/ua1/scan" not in body
    assert "/accounts/ub1/" not in body  # another group's company is not even listed


def test_operator_edit_form_has_no_group_selector(operator):
    form = operator.get("/accounts/ua1/edit-form")
    assert form.status_code == 200
    assert 'name="group"' not in form.text and "分组由管理员调整" in form.text


def test_operator_cannot_open_another_groups_account(operator):
    assert operator.get("/accounts/ub1/edit-form").status_code == 404


def test_operator_changes_password_and_contact_but_never_the_group(operator, saved, _no_platform):
    resp = operator.post("/accounts/ua1/edit", data={
        "password": "new-platform-pw", "contact": "新联系人", "group": "group_b",
    })
    assert resp.headers["HX-Trigger"] == "accountSaved"
    assert saved[-1]["ua1"] == ("new-platform-pw", "新联系人", "group_a")
    assert len(_no_platform["spawned"]) == 1  # the automatic check after a password change


def test_operator_cannot_edit_another_groups_account(operator, saved):
    resp = operator.post("/accounts/ub1/edit", data={"password": "x", "contact": "y"})
    assert "不存在" in resp.text and not saved


def test_operator_edit_respects_the_group_at_write_time(operator, saved, fake_accounts):
    """Visibility is decided again under the write lock: an account moved
    out of the operator's group meanwhile is not theirs to edit."""
    fake_accounts[0].group = "group_b"
    resp = operator.post("/accounts/ua1/edit", data={"password": "x", "contact": "y"})
    assert "不存在" in resp.text and not saved


@pytest.mark.parametrize("method,path,data", [
    ("get", "/accounts/new-form", None),
    ("post", "/accounts", {"company": "新公司", "username": "n1", "password": "p"}),
    ("post", "/accounts/ua1/delete", None),
    ("post", "/accounts/ua1/group", {"group": "group_b"}),
    ("post", "/accounts/ua1/scan", None),
    ("post", "/run-once", None),
    ("get", "/settings", None),
    ("get", "/notify", None),
    ("get", "/reports", None),
])
def test_operator_is_refused_admin_actions(operator, saved, method, path, data):
    resp = getattr(operator, method)(path, data=data) if data else getattr(operator, method)(path)
    assert resp.status_code == 403
    assert not saved


@pytest.mark.parametrize("method,path", [
    ("get", "/accounts/ua1/edit-form"), ("post", "/accounts/ua1/edit"), ("post", "/accounts/verify"),
])
def test_viewers_cannot_edit_or_test(web_client, login_as, fake_accounts, isolated_snapshots,
                                     method, path):
    login_as(web_client, *GROUP_A_USER)
    assert getattr(web_client, method)(path).status_code == 403


# --- 测试登录 ----------------------------------------------------------------------

def test_operator_verify_on_another_group_never_reaches_the_platform(operator, _no_platform):
    resp = operator.post("/accounts/verify", data={"username": "ub1", "password": "guess"})
    assert "不存在" in resp.text
    resp = operator.post("/accounts/verify", data={"username": "unsaved", "password": "guess"})
    assert "不存在" in resp.text
    assert _no_platform["verified"] == []


def test_operator_verify_is_limited_per_account(operator, _no_platform):
    for _ in range(accounts_routes._VERIFY_LIMIT):
        assert "登录成功" in operator.post("/accounts/verify", data={"username": "ua1"}).text
    refused = operator.post("/accounts/verify", data={"username": "ua1"})
    assert "过于频繁" in refused.text
    assert len(_no_platform["verified"]) == accounts_routes._VERIFY_LIMIT
    # The limit is per account.
    assert "登录成功" in operator.post("/accounts/verify", data={"username": "ua2"}).text


def test_admin_verify_is_not_limited(web_client, login_as, fake_accounts, _no_platform):
    login_as(web_client, *ADMIN)
    for _ in range(accounts_routes._VERIFY_LIMIT + 2):
        assert "登录成功" in web_client.post("/accounts/verify", data={"username": "ua1"}).text


def test_over_the_limit_a_password_is_saved_but_not_checked(operator, saved, _no_platform):
    for i in range(accounts_routes._VERIFY_LIMIT):
        operator.post("/accounts/ua1/edit", data={"password": f"pw-{i}", "contact": ""})
    assert len(_no_platform["spawned"]) == accounts_routes._VERIFY_LIMIT

    operator.post("/accounts/ua1/edit", data={"password": "pw-last", "contact": ""})
    assert saved[-1]["ua1"][0] == "pw-last"
    assert len(_no_platform["spawned"]) == accounts_routes._VERIFY_LIMIT
    assert "验证过于频繁" in accounts_routes._tasks["ua1"]["msg"]


def test_relogin_counts_against_the_same_window(operator, _no_platform):
    operator.post("/accounts/verify", data={"username": "ua1"})
    assert "过于频繁" in operator.post("/accounts/ua1/relogin").text


# --- several groups ------------------------------------------------------------------

def test_a_user_in_two_groups_sees_both(web_client, login_as, fake_accounts, isolated_snapshots):
    users.update_user("opa", display_name="", role="operator", groups=["group_a", "group_b"])
    login_as(web_client, *OPERATOR_A)
    body = web_client.get("/accounts").text
    assert "甲 公司" in body and "丙公司" in body
    assert web_client.get("/accounts/ub1/edit-form").status_code == 200
