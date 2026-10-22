"""我负责的: owners see their own companies by default, and can switch to
everything they may see. A display preference — never a permission."""

import pytest

from tests.conftest import GROUP_A_USER, OPERATOR_A
from web.auth import SCOPE_COOKIE


@pytest.fixture
def owned(fake_accounts, isolated_snapshots, isolated_settings):
    """opa (operator, group_a) owns 甲 公司; 乙·公司 is also group_a."""
    fake_accounts[0].owner = "opa"
    for company, reg in (("甲 公司", "RA"), ("乙·公司", "RB"), ("丙公司", "RC")):
        isolated_snapshots.write(company, [{"dataRegNo": reg, "dataRegAuditStatus": 2,
                                            "dataRegName": f"数据{reg}", "id": reg}])
    return fake_accounts


def _client():
    from fastapi.testclient import TestClient

    from web.app import app

    return TestClient(app)


def test_an_owner_starts_on_their_own_companies(owned, login_as):
    c = login_as(_client(), *OPERATOR_A)
    body = c.get("/").text
    assert "我负责的 1" in body and "全部 2" in body
    assert "甲 公司" in body and "乙·公司" not in body


def test_switching_to_everything_sticks(owned, login_as):
    c = login_as(_client(), *OPERATOR_A)
    resp = c.get("/scope?to=all&next=/records", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/records"
    assert c.cookies.get(SCOPE_COOKIE) == "all"
    body = c.get("/").text
    assert "甲 公司" in body and "乙·公司" in body
    c.get("/scope?to=mine&next=/")
    assert "乙·公司" not in c.get("/").text


@pytest.mark.parametrize("path,present,absent", [
    ("/records", "RA", "RB"),
    ("/changes", "甲 公司", "乙·公司"),
])
def test_records_and_changes_follow_the_scope(owned, login_as, path, present, absent):
    c = login_as(_client(), *OPERATOR_A)
    body = c.get(path).text
    assert present in body and absent not in body


def test_the_calendar_follows_the_scope(owned, login_as, monkeypatch):
    from src import todo
    from web.routes import deadlines

    seen = []
    real = todo.deadline_items
    monkeypatch.setattr(deadlines.todo, "deadline_items",
                        lambda accounts, today: seen.append([a.company for a in accounts]) or real(accounts, today))
    c = login_as(_client(), *OPERATOR_A)
    c.get("/calendar")
    c.get("/scope?to=all&next=/calendar")
    assert seen == [["甲 公司"], ["甲 公司", "乙·公司"]]


def test_someone_who_owns_nothing_sees_everything_and_no_toggle(owned, login_as):
    c = login_as(_client(), *GROUP_A_USER)
    body = c.get("/").text
    assert "我负责的" not in body
    assert "甲 公司" in body and "乙·公司" in body


def test_the_scope_never_narrows_permissions(owned, login_as):
    """In 我负责的 mode, a visible-but-not-owned company's record still opens."""
    c = login_as(_client(), *OPERATOR_A)
    assert c.get("/partials/record?account=ua2&reg_no=RB").status_code == 200


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example/", "javascript:alert(1)",
                                    "/\\evil.example", ""])
def test_the_switch_never_redirects_off_site(owned, login_as, target):
    c = login_as(_client(), *OPERATOR_A)
    resp = c.get("/scope", params={"to": "all", "next": target}, follow_redirects=False)
    assert resp.headers["location"] == "/"


def test_the_api_lists_my_companies_first(owned, web_client):
    token = web_client.post("/api/login", json={"username": OPERATOR_A[0],
                                                "password": OPERATOR_A[1]}).json()["token"]
    accounts = web_client.get("/api/status", headers={"Authorization": f"Bearer {token}"}).json()["accounts"]
    assert [(a["company"], a["mine"]) for a in accounts] == [("甲 公司", True), ("乙·公司", False)]
    assert accounts[0]["owner"] == "opa"
