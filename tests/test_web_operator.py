"""经办人 (operator): manage the companies of their own groups — add, edit
password/contact, 测试登录, 手动巡检, delete — and nothing outside them; plus
the per-account limits on console-started platform logins and scans."""

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
    states = (accounts_routes._web_logins, accounts_routes._tasks, accounts_routes._scan_tasks,
              accounts_routes._last_scan)
    for state in states:
        state.clear()
    yield {"spawned": spawned, "verified": verified}
    for state in states:
        state.clear()


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

def test_operator_manages_only_their_groups_companies(operator):
    body = operator.get("/accounts").text
    for action in ("edit-form", "delete", "scan", "platform-access"):
        assert f"/accounts/ua1/{action}" in body
    assert "/accounts/ub1/" not in body  # another group's company is not even listed
    assert "/accounts/new-form" in body and "/run-once" not in body
    assert 'name="group"' not in body  # moving a company between groups stays with admins


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
    ("post", "/accounts/ua1/group", {"group": "group_b"}),
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
    # No typed password: there is nothing stored they may test.
    assert "不存在" in operator.post("/accounts/verify", data={"username": "unsaved"}).text
    assert _no_platform["verified"] == []


def test_operator_can_test_a_new_account_before_adding_it(operator, _no_platform):
    resp = operator.post("/accounts/verify", data={"username": "brand-new", "password": "typed"})
    assert "登录成功" in resp.text and _no_platform["verified"] == ["brand-new"]


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


# --- self-service: add, delete, scan --------------------------------------------------

def test_the_add_form_offers_only_their_groups(operator):
    form = operator.get("/accounts/new-form").text
    assert 'value="group_a"' in form and 'value="group_b"' not in form
    assert "未分组" not in form
    assert '<option value="opa" selected>' in form  # they own what they add


def test_operator_adds_a_company_to_their_group(operator, saved, _isolated_audit):
    resp = operator.post("/accounts", data={"company": "新公司", "username": "n1", "password": "p",
                                            "group": "group_a", "owner": "opa"})
    assert resp.headers.get("HX-Trigger") == "accountSaved", resp.text[:200]
    assert saved[-1]["n1"] == ("p", "", "group_a")


@pytest.mark.parametrize("group", ["", "group_b"])
def test_operator_cannot_add_outside_their_groups(operator, saved, group):
    resp = operator.post("/accounts", data={"company": "新公司", "username": "n1", "password": "p",
                                            "group": group})
    assert "你自己的分组" in resp.text and not saved


@pytest.mark.parametrize("field,value", [("username", "ub1"), ("company", "丙公司")])
def test_a_clash_with_another_groups_company_names_nothing(operator, saved, field, value):
    data = {"company": "新公司", "username": "n1", "password": "p", "group": "group_a", field: value}
    resp = operator.post("/accounts", data=data)
    assert accounts_routes._TAKEN_ELSEWHERE in resp.text and not saved
    # The admin's messages would say whose account it is or which company it clashes with.
    assert "已存在" not in resp.text and "重名" not in resp.text


def test_operator_deletes_only_their_own(operator, saved):
    assert operator.post("/accounts/ub1/delete").status_code == 404
    assert not saved
    assert operator.post("/accounts/ua1/delete").status_code == 200
    assert "ua1" not in saved[-1]


def test_operator_scan_is_limited_per_account(operator, _no_platform, _isolated_audit):
    first = operator.post("/accounts/ua1/scan")
    assert first.status_code == 200 and len(_no_platform["spawned"]) == 1
    accounts_routes._scan_tasks.clear()  # the stubbed scan never finishes by itself
    again = operator.post("/accounts/ua1/scan")
    assert "过于频繁" in again.text and len(_no_platform["spawned"]) == 1
    assert "不存在" in operator.post("/accounts/ub1/scan").text
