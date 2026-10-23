"""Isolation sweep: a colleague in their own group never sees the owner's company.

The owner keeps their company 未分组 (admins only). The colleague is an
operator whose only group is their personal one. Every page, partial, export
and API the colleague can reach is opened, and the owner's company name and
platform account must not appear anywhere — the console's promise that
colleagues "看不到我的内容".
"""

import json
from datetime import datetime, timezone

import pytest

from src.config_loader import Account
from tests.conftest import OPERATOR_A
from web.routes import accounts as accounts_routes
from web.routes import users as users_routes
from web.services import groups as group_svc
from web.services import users

OWNER_COMPANY = "老板自营数据公司"
OWNER_LOGIN = "boss-platform-acct"
COLLEAGUE_COMPANY = "同事客户公司"


@pytest.fixture
def world(monkeypatch, tmp_path, isolated_settings, isolated_snapshots):
    from web import auth as web_auth
    from web.services import attachments_svc
    from web.services import events as ev

    accounts = [
        Account(company=OWNER_COMPANY, username=OWNER_LOGIN, password="owner-secret", group=""),
        Account(company=COLLEAGUE_COMPANY, username="col-acct", password="col-secret",
                group="u_opa", owner="opa"),
    ]
    for module in (web_auth, accounts_routes, users_routes, group_svc):
        monkeypatch.setattr(module, "load_accounts", lambda: list(accounts))
    monkeypatch.setattr(accounts_routes, "spawn", lambda coro: coro.close())

    settings = isolated_settings.read()
    settings["groups"] = {"u_opa": "小王的企业"}
    isolated_settings.write(settings)
    users.update_user("opa", display_name="小王", role="operator", groups=["u_opa"])

    for company, reg in ((OWNER_COMPANY, "RBOSS1"), (COLLEAGUE_COMPANY, "RCOL1")):
        isolated_snapshots.write(company, [{"dataRegNo": reg, "dataRegAuditStatus": 2,
                                            "dataRegName": f"{company}的数据集", "id": reg,
                                            "dataRegApplyTime": 1767225600000}])

    att = tmp_path / "attachments"
    (att / OWNER_COMPANY / "RBOSS1").mkdir(parents=True)
    (att / OWNER_COMPANY / "RBOSS1" / "notice.pdf").write_bytes(b"%PDF-1.4 owner")
    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", att)

    now = datetime.now(timezone.utc).isoformat()
    events = [
        {"account": acct, "company": company, "timestamp": now, "added": [],
         "changed": [{"reg_no": reg, "name": "n", "old_status_code": 1, "new_status_code": 2,
                      "old_status": "待审核", "new_status": "待补正"}], "removed": []}
        for acct, company, reg in ((OWNER_LOGIN, OWNER_COMPANY, "RBOSS1"),
                                   ("col-acct", COLLEAGUE_COMPANY, "RCOL1"))
    ]
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    monkeypatch.setattr(ev, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ev, "_EVENTS_FILE", tmp_path / "events.jsonl")
    ev._cache.clear()
    yield
    ev._cache.clear()


PAGES = [
    "/", "/partials/dashboard", "/partials/todo", "/partials/timeline", "/partials/ops-banner",
    "/partials/account-grid", "/partials/metrics", "/accounts", "/accounts/rows",
    "/records", "/records?q=老板", "/records.csv", "/changes", "/calendar", "/view/", "/view/partial",
    "/me/password", "/scope?to=all&next=/", "/",
    f"/partials/record?account={OWNER_LOGIN}&reg_no=RBOSS1",
    f"/attachments/{OWNER_COMPANY}/RBOSS1/notice.pdf",
]


def _leaks(text: str) -> list[str]:
    return [s for s in (OWNER_COMPANY, OWNER_LOGIN, "RBOSS1", "owner-secret") if s in text]


def test_the_colleague_sees_their_own_company(world, web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    assert COLLEAGUE_COMPANY in web_client.get("/").text
    assert COLLEAGUE_COMPANY in web_client.get("/records").text


@pytest.mark.parametrize("path", PAGES)
def test_no_page_shows_the_owners_company(world, web_client, login_as, path):
    login_as(web_client, *OPERATOR_A)
    resp = web_client.get(path, follow_redirects=True)
    assert _leaks(resp.text) == [], (path, resp.status_code)


@pytest.mark.parametrize("method,path,data", [
    ("post", "/accounts/{acct}/platform-access", {}),
    ("post", "/accounts/{acct}/edit", {"password": "x", "contact": "y"}),
    ("get", "/accounts/{acct}/edit-form", None),
    ("post", "/accounts/{acct}/relogin", {}),
    ("post", "/accounts/{acct}/scan", {}),
    ("post", "/accounts/{acct}/delete", {}),
])
def test_the_owners_account_is_indistinguishable_from_none(world, web_client, login_as,
                                                           method, path, data, monkeypatch):
    """Acting on the owner's platform account answers exactly what acting on
    an account that does not exist answers — the colleague cannot even tell
    it is there (their own typed name is echoed either way)."""
    monkeypatch.setattr(accounts_routes, "verify_credentials",
                        lambda u, p: pytest.fail("reached the platform"))
    login_as(web_client, *OPERATOR_A)

    def call(acct):
        url = path.format(acct=acct)
        body = {k: v.format(acct=acct) for k, v in (data or {}).items()}
        resp = web_client.post(url, data=body) if method == "post" else web_client.get(url)
        return resp.status_code, resp.text.replace(acct, "<ACCT>")

    assert call(OWNER_LOGIN) == call("no-such-acct")


def test_testing_the_owners_account_never_reaches_the_platform(world, web_client, login_as, monkeypatch):
    """测试登录 with a typed password is how a colleague checks a NEW account
    before adding it, so for an unknown name it does go to the platform; for
    a name the system already holds elsewhere it is refused on the spot.
    That difference only confirms a platform account name the colleague
    already guessed — the same thing adding it would reveal, since names are
    unique — and it shows nothing of the account and never tries a password."""
    monkeypatch.setattr(accounts_routes, "verify_credentials",
                        lambda u, p: pytest.fail("reached the platform"))
    login_as(web_client, *OPERATOR_A)
    resp = web_client.post("/accounts/verify", data={"username": OWNER_LOGIN, "password": "guess"})
    assert "不存在" in resp.text
    assert _leaks(resp.text.replace(OWNER_LOGIN, "")) == []


def test_adding_the_owners_company_again_names_nothing(world, web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    resp = web_client.post("/accounts", data={"company": OWNER_COMPANY, "username": "x1",
                                              "password": "p", "group": "u_opa"})
    assert accounts_routes._TAKEN_ELSEWHERE in resp.text
    assert _leaks(resp.text.replace(f'value="{OWNER_COMPANY}"', "")) == []


API_PAGES = [
    "/api/status", "/api/status?scope=all", "/api/status?scope=mine", "/api/status?year=2026",
    "/api/records", "/api/records?scope=all", f"/api/records?company={OWNER_COMPANY}",
    "/api/records?q=老板", "/api/records?q=boss", "/api/records?status=2&page=1",
    "/api/changes", "/api/changes?scope=all", "/api/me",
    "/api/deadlines", "/api/deadlines?scope=all",
    f"/api/record?company={OWNER_COMPANY}&reg_no=RBOSS1",
    f"/api/attachment?company={OWNER_COMPANY}&reg_no=RBOSS1&name=notice.pdf",
    f"/api/company?company={OWNER_COMPANY}", f"/api/company?company={COLLEAGUE_COMPANY}",
    "/api/accounts/new-form",
]


def test_every_api_read_is_in_the_sweep():
    """A new GET under /api has to be added to API_PAGES — and so gets swept."""
    from web.app import app

    def walk(routes):
        for r in routes:
            if hasattr(r, "original_router"):  # newer FastAPI wraps included routers
                yield from walk(r.original_router.routes)
            else:
                yield r

    gets = {r.path for r in walk(app.routes)
            if r.path.startswith("/api/") and "GET" in (getattr(r, "methods", None) or ())}
    assert gets == {p.split("?")[0] for p in API_PAGES}


def _api_headers(web_client) -> dict:
    token = web_client.post("/api/login", json={"username": OPERATOR_A[0],
                                                "password": OPERATOR_A[1]}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_the_mini_program_api_shows_nothing_of_the_owner(world, web_client):
    headers = _api_headers(web_client)
    for path in API_PAGES:
        resp = web_client.get(path, headers=headers)
        assert _leaks(resp.text) == [], (path, resp.status_code)
    rows = web_client.get("/api/changes", headers=headers).json()["rows"]
    assert [r["reg_no"] for r in rows] == ["RCOL1"]  # not empty for an unrelated reason


@pytest.mark.parametrize("path", [
    "/api/record?company={company}&reg_no={reg}",
    "/api/attachment?company={company}&reg_no={reg}&name=notice.pdf",
    "/api/company?company={company}",
])
def test_the_owners_record_reads_like_no_record_at_all(world, web_client, path):
    headers = _api_headers(web_client)

    def call(company, reg):
        resp = web_client.get(path.format(company=company, reg=reg), headers=headers)
        return resp.status_code, resp.text

    assert call(OWNER_COMPANY, "RBOSS1") == call("没有这家公司", "NOPE")


@pytest.mark.parametrize("path", ["/api/accounts/scan", "/api/accounts/platform-access"])
def test_acting_on_the_owners_company_from_the_phone_reads_like_none(world, web_client, path):
    headers = _api_headers(web_client)

    def call(company):
        resp = web_client.post(path, json={"company": company}, headers=headers)
        return resp.status_code, resp.text

    assert call(OWNER_COMPANY) == call("没有这家公司")


def test_adding_the_owners_company_from_the_phone_names_nothing(world, web_client):
    resp = web_client.post("/api/accounts", headers=_api_headers(web_client), json={
        "company": OWNER_COMPANY, "username": "x1", "password": "p", "group": "u_opa"})
    assert resp.json() == {"error": accounts_routes._TAKEN_ELSEWHERE}
