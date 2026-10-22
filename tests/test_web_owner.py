"""负责人: who follows up each company (按客户分工)."""

import json

import pytest

from tests.conftest import ADMIN, OPERATOR_A
from web import deps
from web.routes import accounts as accounts_routes
from web.services import users


def _audit(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def saved(monkeypatch, fake_accounts):
    """Apply save_accounts() to the in-memory accounts instead of accounts.enc."""
    out = []

    def save(accts):
        out.append({a.username: a.owner for a in accts})
        by = {a.username: a for a in accts}
        for a in fake_accounts:
            if a.username in by:
                a.owner = by[a.username].owner

    monkeypatch.setattr(accounts_routes, "save_accounts", save)
    monkeypatch.setattr(accounts_routes, "spawn", lambda coro: coro.close())
    return out


@pytest.fixture
def admin(web_client, login_as, isolated_settings, isolated_snapshots, saved):
    return login_as(web_client, *ADMIN)


def test_admin_assigns_an_owner_from_the_row(admin, saved, _isolated_audit):
    resp = admin.post("/accounts/ua1/owner", data={"owner": "opa"})
    assert resp.status_code == 200
    assert saved[-1]["ua1"] == "opa"
    entry = _audit(_isolated_audit)[-1]
    assert entry["action"] == "account.owner" and entry["detail"] == {"owner": ["", "opa"]}
    assert entry["company"] == "甲 公司"


@pytest.mark.parametrize("owner,msg", [
    ("gb", "看不到"),          # a viewer of group_b cannot own a group_a company
    ("ghost", "不存在"),
])
def test_an_owner_must_be_able_to_see_the_company(admin, saved, owner, msg):
    resp = admin.post("/accounts/ua1/owner", data={"owner": owner})
    assert resp.status_code == 400 and msg in resp.text
    assert not saved


def test_a_disabled_user_cannot_be_made_owner(admin, saved):
    users.set_disabled("opa", True)
    assert admin.post("/accounts/ua1/owner", data={"owner": "opa"}).status_code == 400


def test_clearing_the_owner(admin, saved, fake_accounts):
    fake_accounts[0].owner = "opa"
    admin.post("/accounts/ua1/owner", data={"owner": ""})
    assert saved[-1]["ua1"] == ""


def test_owner_choices_only_list_who_can_see_the_group():
    users.create_user("boss", display_name="老板", role="admin", groups=[],
                      password="boss-password", by="t")
    choices = dict(deps.owner_choices("group_a"))
    assert set(choices) == {"boss", "ga", "opa"}  # gb is in group_b only
    users.set_disabled("ga", True)
    assert "ga" not in dict(deps.owner_choices("group_a"))


def test_owner_label_flags_owners_who_lost_sight():
    users.update_user("opa", display_name="小王", role="operator", groups=["group_a"])
    assert deps.owner_label("opa", "group_a") == "小王"
    assert deps.owner_label("opa", "group_b") == "小王（看不到该分组）"
    assert deps.owner_label("ghost", "group_a") == "ghost（已删除）"
    assert deps.owner_label("", "group_a") == ""


def test_add_and_edit_carry_the_owner(admin, saved, _isolated_audit):
    admin.post("/accounts", data={"company": "新公司", "username": "n1", "password": "p",
                                  "group": "group_a", "owner": "opa"})
    assert saved[-1]["n1"] == "opa"
    admin.post("/accounts/ua1/edit", data={"contact": "", "group": "group_a", "owner": "ga"})
    assert saved[-1]["ua1"] == "ga"
    edit = [e for e in _audit(_isolated_audit) if e["action"] == "account.edit"][-1]
    assert edit["detail"]["owner"] == ["", "ga"]


def test_add_refuses_an_owner_outside_the_group(admin, saved):
    resp = admin.post("/accounts", data={"company": "新公司", "username": "n1", "password": "p",
                                         "group": "group_b", "owner": "opa"})
    assert "看不到" in resp.text and not saved


def test_an_operator_cannot_change_the_owner(web_client, login_as, isolated_settings,
                                             isolated_snapshots, saved, fake_accounts):
    fake_accounts[0].owner = "opa"
    login_as(web_client, *OPERATOR_A)
    web_client.post("/accounts/ua1/edit", data={"contact": "x", "owner": "ga"})
    assert saved[-1]["ua1"] == "opa"
    assert web_client.post("/accounts/ua1/owner", data={"owner": "ga"}).status_code == 403
    page = web_client.get("/accounts").text
    assert 'name="owner"' not in page


def test_the_accounts_page_shows_owners(admin, fake_accounts):
    users.update_user("opa", display_name="小王", role="operator", groups=["group_a"])
    fake_accounts[0].owner = "opa"
    body = admin.get("/accounts").text
    row = body.split('id="row-ua1"', 1)[1].split("</tr>", 1)[0]
    assert '<option value="opa" selected>小王</option>' in row
