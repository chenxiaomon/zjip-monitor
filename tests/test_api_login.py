"""Mini program sign-in with a console account: /api/login and Bearer tokens."""

import json
import time

import pytest

from tests.conftest import ADMIN, API_KEY, GROUP_A_USER, OPERATOR_A
from web import auth, login_guard
from web.services import users


def _audit(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def two_companies(isolated_snapshots, fake_accounts):
    for company in ("甲 公司", "丙公司"):
        isolated_snapshots.write(company, [{"dataRegNo": "R1", "dataRegAuditStatus": 7,
                                            "dataRegName": "n", "id": 1}])


def test_a_colleague_sees_exactly_their_companies(web_client, two_companies, _isolated_audit):
    resp = _login(web_client, *OPERATOR_A)
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"] == {"username": "opa", "display_name": "", "role": "operator",
                            "groups": ["group_a"]}
    status = web_client.get("/api/status", headers=_bearer(data["token"])).json()
    assert {a["company"] for a in status["accounts"]} == {"甲 公司"}
    records = web_client.get("/api/records", headers=_bearer(data["token"])).json()
    assert {r["company"] for r in records["records"]} == {"甲 公司"}
    assert web_client.get("/api/me", headers=_bearer(data["token"])).json()["user"]["username"] == "opa"

    (entry,) = _audit(_isolated_audit)
    assert entry["action"] == "auth.login" and entry["via"] == "api" and entry["actor"] == "opa"


def test_the_admin_key_still_works_for_scripts(web_client, two_companies):
    status = web_client.get("/api/status", headers={"X-API-Key": API_KEY}).json()
    assert {a["company"] for a in status["accounts"]} == {"甲 公司", "丙公司"}


def test_the_env_admin_can_sign_in_too(web_client, two_companies):
    token = _login(web_client, *ADMIN).json()["token"]
    status = web_client.get("/api/status", headers=_bearer(token)).json()
    assert {a["company"] for a in status["accounts"]} == {"甲 公司", "丙公司"}


@pytest.mark.parametrize("body,status", [
    ({"username": "opa", "password": "wrong-password"}, 401),
    ({"username": "nobody", "password": "x"}, 401),
    ({"username": "opa"}, 401),
])
def test_bad_credentials(web_client, body, status):
    resp = web_client.post("/api/login", json=body)
    assert resp.status_code == status and "token" not in resp.json()


def test_a_malformed_body_is_refused(web_client):
    assert web_client.post("/api/login", content=b"not json").status_code == 400
    assert web_client.post("/api/login", json=["opa"]).status_code == 400


def test_a_disabled_user_is_told_so(web_client):
    users.set_disabled("opa", True)
    resp = _login(web_client, *OPERATOR_A)
    assert resp.status_code == 403 and "停用" in resp.json()["error"]


def test_an_initial_password_is_changed_in_the_console_first(
    web_client, two_companies, _isolated_audit
):
    """The console only reminds; the mini program has nowhere to change it."""
    users.set_password("opa", "set-by-the-admin", must_change=True)
    resp = _login(web_client, "opa", "set-by-the-admin")
    assert resp.status_code == 403
    assert "修改初始口令" in resp.json()["error"] and "token" not in resp.json()
    (entry,) = _audit(_isolated_audit)
    assert entry["action"] == "auth.login_failed"
    assert entry["detail"] == {"via": "api", "reason": "需先修改初始口令"}

    users.set_password("opa", "chosen-by-opa-1", must_change=False)  # what /me/password does
    resp = _login(web_client, "opa", "chosen-by-opa-1")
    assert resp.status_code == 200 and resp.json()["token"]


def test_a_token_from_before_the_rule_stops_working(web_client, two_companies):
    users.set_password("opa", "set-by-the-admin", must_change=True)
    token, _ = auth.make_api_token("opa")  # as /api/login used to hand out
    resp = web_client.get("/api/status", headers=_bearer(token))
    assert resp.status_code == 401


def test_the_lockout_is_shared_with_the_console_form(web_client):
    for _ in range(login_guard.MAX_FAILURES):
        web_client.post("/login", data={"username": "opa", "password": "wrong-password"})
    resp = _login(web_client, *OPERATOR_A)
    assert resp.status_code == 429


@pytest.mark.parametrize("revoke", [
    lambda: users.set_password("opa", "brand-new-password"),
    lambda: users.revoke_sessions("opa"),
    lambda: users.set_disabled("opa", True),
    lambda: users.delete_user("opa"),
])
def test_console_revocations_end_the_phone_session(web_client, two_companies, revoke):
    token = _login(web_client, *OPERATOR_A).json()["token"]
    assert web_client.get("/api/status", headers=_bearer(token)).status_code == 200
    revoke()
    resp = web_client.get("/api/status", headers=_bearer(token))
    assert resp.status_code == 401 and "重新登录" in resp.json()["error"]


def test_tokens_and_cookies_are_not_interchangeable(web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    cookie = web_client.cookies.get(auth.COOKIE_NAME)
    assert web_client.get("/api/status", headers=_bearer(cookie)).status_code == 401
    token, _ = auth.make_api_token("opa")
    assert auth.user_from_cookie(token) is None


def test_an_expired_token_is_refused(monkeypatch):
    token, _ = auth.make_api_token("opa")
    monkeypatch.setattr(time, "time", lambda: 10 ** 11)
    assert auth.user_from_api_token(token) is None


def test_garbage_bearers_are_refused(web_client):
    for bad in ("", "x", "opa|1|sig", "opa|notanumber|sig"):
        assert web_client.get("/api/status", headers=_bearer(bad)).status_code == 401


def test_logout_is_recorded(web_client, _isolated_audit):
    token = _login(web_client, *GROUP_A_USER).json()["token"]
    assert web_client.post("/api/logout", headers=_bearer(token)).json() == {"ok": True}
    assert _audit(_isolated_audit)[-1]["action"] == "auth.logout"


def test_failed_api_logins_mask_unknown_names(web_client, _isolated_audit):
    _login(web_client, "Sentinel-Typed-Password", "x")
    (entry,) = _audit(_isolated_audit)
    assert entry["action"] == "auth.login_failed" and entry["detail"] == {"via": "api"}
    assert "Sentinel-Typed-Password" not in json.dumps(entry)


def test_api_answers_are_never_cached(web_client, two_companies):
    """Tokens and per-person company lists: nothing in front may keep them."""
    login = _login(web_client, *OPERATOR_A)
    token = login.json()["token"]
    answers = [login,
               web_client.get("/api/status", headers=_bearer(token)),
               web_client.get("/api/records", headers=_bearer(token)),
               web_client.get("/api/status", headers=_bearer("garbage"))]
    assert [r.status_code for r in answers] == [200, 200, 200, 401]
    assert all(r.headers.get("cache-control") == "no-store" for r in answers)
