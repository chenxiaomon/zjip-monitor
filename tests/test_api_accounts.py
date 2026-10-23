"""The mini program's company actions: 立即巡检, 去平台办理, 新增企业.

They are the console's own implementation (web/routes/accounts.py), so the
rules are the console's: admins anything, 经办人 their own groups' companies,
只读 users nothing; a company outside one's groups reads like none at all.
"""

import json

import pytest

from tests.conftest import ADMIN, API_KEY, GROUP_A_USER, GROUP_B_USER, OPERATOR_A
from web.routes import accounts as accounts_routes


@pytest.fixture(autouse=True)
def _no_platform(monkeypatch):
    """Never launch a real platform login or scan; record what would have run."""
    spawned = []
    monkeypatch.setattr(accounts_routes, "spawn", lambda coro: spawned.append(coro) or coro.close())
    monkeypatch.setattr(accounts_routes, "verify_credentials", lambda u, p: pytest.fail("reached the platform"))
    states = (accounts_routes._web_logins, accounts_routes._tasks, accounts_routes._scan_tasks,
              accounts_routes._last_scan)
    for state in states:
        state.clear()
    yield spawned
    for state in states:
        state.clear()


@pytest.fixture
def world(fake_accounts, isolated_settings, isolated_snapshots, monkeypatch):
    """fake_accounts: 甲 公司 (ua1) and 乙·公司 (ua2) in group_a, 丙公司 (ub1) in group_b."""
    saved = []
    monkeypatch.setattr(accounts_routes, "save_accounts",
                        lambda accts: saved.append({a.username: (a.company, a.group, a.owner) for a in accts}))
    fake_accounts[0].owner = "opa"
    return saved


def _headers(client, user) -> dict:
    token = client.post("/api/login", json={"username": user[0], "password": user[1]}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _audit(audit_dir) -> list[dict]:
    return [json.loads(ln) for f in sorted(audit_dir.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


# --- who may do what ----------------------------------------------------------------

USERS = {"admin": ADMIN, "opa": OPERATOR_A, "ga": GROUP_A_USER, "gb": GROUP_B_USER}


@pytest.mark.parametrize("user,company,expected", [
    ("admin", "甲 公司", 200), ("opa", "甲 公司", 200), ("ga", "甲 公司", 200), ("gb", "甲 公司", 404),
    ("opa", "丙公司", 404), ("opa", "没有这家", 404),
])
def test_company_state_follows_visibility(world, web_client, user, company, expected):
    resp = web_client.get("/api/company", params={"company": company}, headers=_headers(web_client, USERS[user]))
    assert resp.status_code == expected


@pytest.mark.parametrize("user,path,body,expected", [
    ("admin", "/api/accounts/scan", {"company": "丙公司"}, 200),
    ("opa", "/api/accounts/scan", {"company": "甲 公司"}, 200),
    ("opa", "/api/accounts/scan", {"company": "丙公司"}, 404),      # another group's
    ("ga", "/api/accounts/scan", {"company": "甲 公司"}, 403),       # 只读
    ("admin", "/api/accounts/platform-access", {"company": "丙公司"}, 200),
    ("opa", "/api/accounts/platform-access", {"company": "乙·公司"}, 200),
    ("opa", "/api/accounts/platform-access", {"company": "丙公司"}, 404),
    ("ga", "/api/accounts/platform-access", {"company": "甲 公司"}, 403),
    ("ga", "/api/accounts", {"company": "新公司", "username": "n1", "password": "p", "group": "group_a"}, 403),
])
def test_actions_follow_the_console_rules(world, web_client, user, path, body, expected):
    resp = web_client.post(path, json=body, headers=_headers(web_client, USERS[user]))
    assert resp.status_code == expected, resp.text
    if expected == 403:
        assert resp.json() == {"error": "只读账号不能操作企业"}


@pytest.mark.parametrize("path", ["/api/accounts/scan", "/api/accounts/platform-access"])
def test_another_groups_company_reads_like_none(world, web_client, path):
    headers = _headers(web_client, OPERATOR_A)
    theirs = web_client.post(path, json={"company": "丙公司"}, headers=headers)
    missing = web_client.post(path, json={"company": "没有这家"}, headers=headers)
    assert (theirs.status_code, theirs.json()) == (missing.status_code, missing.json()) == (
        404, {"error": "企业不存在或无权查看"})


@pytest.mark.parametrize("method,path", [
    ("get", "/api/company?company=甲 公司"), ("get", "/api/accounts/new-form"),
    ("post", "/api/accounts"), ("post", "/api/accounts/scan"), ("post", "/api/accounts/platform-access"),
])
def test_the_admin_key_cannot_act(world, web_client, method, path):
    """The key old mini program builds shipped: it reads, it never acts."""
    resp = getattr(web_client, method)(path, headers={"X-API-Key": API_KEY},
                                       **({"json": {"company": "甲 公司"}} if method == "post" else {}))
    assert resp.status_code == 403 and resp.json() == {"error": "请用小程序个人账号登录"}


@pytest.mark.parametrize("kwargs", [
    {"data": {"company": "甲 公司"}},                                 # a form
    {"content": "company=甲", "headers": {"content-type": "text/plain"}},
    {"json": ["甲 公司"]},                                            # JSON, but not an object
])
def test_only_a_json_object_is_a_request(world, web_client, kwargs):
    headers = {**_headers(web_client, OPERATOR_A), **kwargs.pop("headers", {})}
    resp = web_client.post("/api/accounts/scan", headers=headers, **kwargs)
    assert resp.status_code == 400 and resp.json() == {"error": "请求格式错误"}


# --- 立即巡检 ---------------------------------------------------------------------------

def test_an_operator_scans_once_per_window(world, web_client, _no_platform, _isolated_audit):
    headers = _headers(web_client, OPERATOR_A)
    first = web_client.post("/api/accounts/scan", json={"company": "甲 公司"}, headers=headers)
    assert first.json() == {"ok": True, "already_running": False} and len(_no_platform) == 1
    accounts_routes._scan_tasks["ua1"] = "done"  # the scan finished
    again = web_client.post("/api/accounts/scan", json={"company": "甲 公司"}, headers=headers)
    assert again.status_code == 429 and again.json()["retry_after"] > 0
    assert "手动巡检过于频繁" in again.json()["error"] and len(_no_platform) == 1
    lines = [e for e in _audit(_isolated_audit) if e["action"] == "account.scan"]
    assert [(e["ok"], e["via"], e["company"]) for e in lines] == [(True, "api", "甲 公司"), (False, "api", "甲 公司")]
    state = web_client.get("/api/company", params={"company": "甲 公司"}, headers=headers).json()
    assert state["scan_wait"] > 0 and state["scan"]["state"] == "done"


def test_admins_have_no_cooldown(world, web_client, _no_platform):
    headers = _headers(web_client, ADMIN)
    for _ in range(2):
        accounts_routes._scan_tasks.pop("ua1", None)
        assert web_client.post("/api/accounts/scan", json={"company": "甲 公司"}, headers=headers).status_code == 200
    assert len(_no_platform) == 2


def test_a_running_scan_is_not_started_twice(world, web_client, _no_platform):
    headers = _headers(web_client, OPERATOR_A)
    web_client.post("/api/accounts/scan", json={"company": "甲 公司"}, headers=headers)
    second = web_client.post("/api/accounts/scan", json={"company": "甲 公司"}, headers=headers)
    assert second.json() == {"ok": True, "already_running": True} and len(_no_platform) == 1


# --- 去平台办理 -------------------------------------------------------------------------

def test_reveal_answers_once_and_is_logged(world, web_client, _isolated_audit):
    resp = web_client.post("/api/accounts/platform-access", json={"company": "甲 公司", "reg_no": "DIP-1"},
                           headers=_headers(web_client, OPERATOR_A))
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    d = resp.json()
    assert (d["company"], d["username"], d["password"]) == ("甲 公司", "ua1", "p1")
    assert d["login_url"] == "https://example.invalid/user/login"
    assert d["detail_url"] == "https://example.invalid/dregister/detail/DIP-1"
    (line,) = [e for e in _audit(_isolated_audit) if e["action"] == "account.reveal"]
    assert (line["actor"], line["via"], line["target"], line["detail"]) == ("opa", "api", "ua1", {"reg_no": "DIP-1"})
    assert "p1" not in json.dumps(_audit(_isolated_audit))  # the password itself is never logged


def test_a_reg_no_that_is_not_one_is_dropped(world, web_client):
    resp = web_client.post("/api/accounts/platform-access", json={"company": "甲 公司", "reg_no": "../x"},
                           headers=_headers(web_client, OPERATOR_A))
    assert resp.json()["detail_url"] == ""


def test_the_company_state_never_carries_the_login(world, web_client):
    accounts_routes._tasks["ua1"] = {"status": "error", "msg": "账号 ua1 密码错误"}
    text = web_client.get("/api/company", params={"company": "甲 公司"}, headers=_headers(web_client, ADMIN)).text
    assert "ua1" not in text and "p1" not in text and "（平台账号）" in text


# --- 新增企业 ---------------------------------------------------------------------------

def test_the_form_offers_an_operator_their_own_groups(world, web_client):
    d = web_client.get("/api/accounts/new-form", headers=_headers(web_client, OPERATOR_A)).json()
    assert [g["key"] for g in d["groups"]] == ["group_a"]
    assert (d["default_group"], d["default_owner"]) == ("group_a", "opa")
    assert "opa" in [o["username"] for o in d["owners"]["group_a"]]


def test_the_form_offers_an_admin_every_group(world, web_client):
    d = web_client.get("/api/accounts/new-form", headers=_headers(web_client, ADMIN)).json()
    assert [g["key"] for g in d["groups"]] == ["", "group_a", "group_b"]
    assert d["groups"][0]["label"] == "未分组（仅管理员可见）" and d["default_group"] == ""


def test_a_viewer_gets_no_form(world, web_client):
    assert web_client.get("/api/accounts/new-form", headers=_headers(web_client, GROUP_A_USER)).status_code == 403


def test_an_operator_adds_a_company_and_its_first_login_starts(world, web_client, _no_platform, _isolated_audit):
    resp = web_client.post("/api/accounts", headers=_headers(web_client, OPERATOR_A), json={
        "company": " 新客户公司 ", "username": "new-acct", "password": "pw", "group": "group_a", "owner": "opa"})
    assert resp.json() == {"ok": True, "company": "新客户公司"}
    assert world[-1]["new-acct"] == ("新客户公司", "group_a", "opa")
    assert len(_no_platform) == 1 and accounts_routes._tasks["new-acct"]["status"] == "pending"
    (line,) = [e for e in _audit(_isolated_audit) if e["action"] == "account.add"]
    assert (line["via"], line["company"], line["target"]) == ("api", "新客户公司", "new-acct")


@pytest.mark.parametrize("body,error", [
    ({"company": "新公司", "username": "n1", "password": "p", "group": "group_b"}, "请选择你自己的分组"),
    ({"company": "新公司", "username": "n1", "password": "p", "group": ""}, "请选择你自己的分组"),
    ({"company": "", "username": "n1", "password": "p", "group": "group_a"}, "公司名称不能为空"),
    ({"company": "丙公司", "username": "n1", "password": "p", "group": "group_a"}, accounts_routes._TAKEN_ELSEWHERE),
    ({"company": "新公司", "username": "ub1", "password": "p", "group": "group_a"}, accounts_routes._TAKEN_ELSEWHERE),
])
def test_an_operator_gets_the_consoles_answers(world, web_client, _no_platform, _isolated_audit, body, error):
    resp = web_client.post("/api/accounts", json=body, headers=_headers(web_client, OPERATOR_A))
    assert resp.status_code == 400 and resp.json() == {"error": error}
    assert world == [] and _no_platform == []
    assert [e["action"] for e in _audit(_isolated_audit)] == ["auth.login"]  # nothing changed, nothing logged
