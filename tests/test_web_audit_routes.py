"""Every state-changing console route leaves one operation-log line — and
no line ever carries a secret."""

import base64
import json

import pytest
from fastapi.responses import PlainTextResponse

from tests.conftest import ADMIN, OPERATOR_A
from web import login_guard
from web.routes import accounts as accounts_routes
from web.routes import reports as reports_routes
from web.routes import settings as settings_routes
from web.services import audit_log, users

BOSS = ("boss", "boss-password")


def _audit(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


def _client():
    from fastapi.testclient import TestClient

    from web.app import app

    return TestClient(app)


def _post_paths():
    from web.app import app

    def walk(routes):
        for r in routes:
            if hasattr(r, "original_router"):  # newer FastAPI wraps included routers
                yield from walk(r.original_router.routes)
            else:
                yield r

    return {r.path for r in walk(app.routes)
            if set(getattr(r, "methods", None) or ()) & {"POST", "PUT", "PATCH", "DELETE"}}


@pytest.fixture
def world(monkeypatch, tmp_path, fake_accounts, isolated_settings, isolated_snapshots,
          isolated_control_dir):
    """Everything the routes touch, faked: no platform, no accounts.enc, no
    background work, reports in a temp dir."""
    monkeypatch.setattr(accounts_routes, "spawn", lambda coro: coro.close())
    monkeypatch.setattr(reports_routes, "spawn", lambda coro: coro.close())
    monkeypatch.setattr(accounts_routes, "save_accounts", lambda accts: None)
    monkeypatch.setattr(accounts_routes, "verify_credentials", lambda u, p: "token")
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "report_x.html").write_text("<html></html>")
    monkeypatch.setattr(reports_routes, "_REPORTS_DIR", reports_dir)
    accounts_routes._web_logins.clear()
    users.create_user(BOSS[0], display_name="老板", role="admin", groups=[],
                      password=BOSS[1], by="t", must_change=False)
    settings = isolated_settings.read()
    settings["groups"]["g_empty"] = "空组"  # something the delete route may remove
    isolated_settings.write(settings)
    yield
    accounts_routes._web_logins.clear()
    # spawn is stubbed, so a "running" scan would never finish and leak into later tests.
    accounts_routes._scan_tasks.clear()
    accounts_routes._tasks.clear()


def _thresholds_form():
    form = {}
    for _section, key, _label, kind, lower, exclusive, upper, *_ in settings_routes.THRESHOLDS:
        form[key] = str(min(upper, lower + 1))
    return form


# path → (form data, expected action, expected target)
ROUTES = {
    "/accounts/{username}/relogin": ("/accounts/ua1/relogin", {}, "account.relogin", "ua1"),
    "/accounts/{username}/scan": ("/accounts/ua1/scan", {}, "account.scan", "ua1"),
    "/run-once": ("/run-once", {}, "scan.start", None),
    "/run-once/stop": ("/run-once/stop", {}, "scan.stop", None),
    "/accounts": ("/accounts", {"company": "新公司", "username": "n1", "password": "p",
                                "group": "group_a"}, "account.add", "n1"),
    "/accounts/verify": ("/accounts/verify", {"username": "ua1"}, "account.verify", "ua1"),
    "/accounts/{username}/edit": ("/accounts/ua1/edit", {"contact": "新联系人", "group": "group_a"},
                                  "account.edit", "ua1"),
    "/accounts/{username}/group": ("/accounts/ua1/group", {"group": "group_b"}, "account.group", "ua1"),
    "/accounts/{username}/owner": ("/accounts/ua1/owner", {"owner": "opa"}, "account.owner", "ua1"),
    "/accounts/{username}/platform-access": ("/accounts/ua1/platform-access", {}, "account.reveal", "ua1"),
    "/accounts/{username}/delete": ("/accounts/ua2/delete", {}, "account.delete", "ua2"),
    "/notify/dingtalk": ("/notify/dingtalk", {"webhook": "https://x/robot?access_token=t"},
                         "notify.dingtalk", None),
    "/notify/wechat": ("/notify/wechat", {"webhook": "https://x/wx?key=k"}, "notify.wechat", None),
    "/notify/email": ("/notify/email", {"smtp_host": "smtp.example.com"}, "notify.email", None),
    "/reports/generate": ("/reports/generate", {}, "report.generate", None),
    "/reports/delete/{filename}": ("/reports/delete/report_x.html", {}, "report.delete", "report_x.html"),
    "/settings/pause": ("/settings/pause", {}, "settings.pause", None),
    "/settings/schedule": ("/settings/schedule", {"crons": ["0 8 * * *"]}, "settings.schedule", None),
    "/settings/login": ("/settings/login", {"method": "http"}, "settings.login", None),
    "/settings/thresholds": ("/settings/thresholds", "THRESHOLDS", "settings.thresholds", None),
    "/users": ("/users", {"username": "lisi", "role": "viewer", "groups": ["group_a"],
                          "password": "long-enough-pw"}, "user.create", "lisi"),
    "/users/{username}/edit": ("/users/opa/edit", {"display_name": "小王", "role": "operator",
                                                    "groups": ["group_a"]}, "user.update", "opa"),
    "/users/{username}/password": ("/users/opa/password", {"password": "reset-password",
                                                           "password2": "reset-password"},
                                   "user.password_reset", "opa"),
    "/users/{username}/disable": ("/users/opa/disable", {}, "user.disable", "opa"),
    "/users/{username}/enable": ("/users/opa/enable", {}, "user.enable", "opa"),
    "/users/{username}/revoke": ("/users/opa/revoke", {}, "user.revoke", "opa"),
    "/users/{username}/delete": ("/users/gb/delete", {}, "user.delete", "gb"),
    "/users/groups": ("/users/groups", {"label": "新组"}, "group.create", None),
    "/users/groups/{key}/rename": ("/users/groups/group_a/rename", {"label": "来源一甲"},
                                   "group.rename", "group_a"),
    "/users/groups/{key}/delete": ("/users/groups/g_empty/delete", {}, "group.delete", "g_empty"),
    "/me/password": ("/me/password", {"current": BOSS[1], "password": "boss-new-password",
                                      "password2": "boss-new-password"}, "self.password_change", "boss"),
}


def test_every_state_changing_route_is_in_the_table():
    """A new POST route has to be added here — and so has to decide what it logs.
    The login routes (console and mini program) are covered by their own tests."""
    assert _post_paths() == set(ROUTES) | {"/login", "/logout", "/api/login", "/api/logout"}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_each_route_logs_exactly_one_line(world, login_as, _isolated_audit, route):
    path, data, action, target = ROUTES[route]
    client = login_as(_client(), *BOSS)
    before = len(_audit(_isolated_audit))
    resp = client.post(path, data=_thresholds_form() if data == "THRESHOLDS" else data)
    assert resp.status_code < 400, resp.text[:300]

    new = _audit(_isolated_audit)[before:]
    assert [e["action"] for e in new] == [action], new
    (entry,) = new
    assert entry["actor"] == "boss" and entry["actor_name"] == "老板"
    assert entry["role"] == "admin" and entry["via"] == "cookie" and entry["ok"] is True
    if target:
        assert entry["target"] == target


def test_account_lines_name_the_company(world, login_as, _isolated_audit):
    client = login_as(_client(), *BOSS)
    client.post("/accounts/ua1/edit", data={"password": "new-pw", "contact": "C", "group": "group_b"})
    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] == "account.edit"]
    assert entry["company"] == "甲 公司"
    assert entry["detail"] == {"password_changed": True, "contact": ["A1", "C"],
                               "group": ["group_a", "group_b"]}


def test_settings_lines_say_what_changed(world, login_as, _isolated_audit):
    client = login_as(_client(), *BOSS)
    client.post("/settings/schedule", data={"crons": ["0 8 * * *"]})
    client.post("/settings/login", data={"method": "http", "token_max_age_hours": "12",
                                         "headless": "on"})
    schedule, login = [e for e in _audit(_isolated_audit) if e["action"].startswith("settings.")]
    assert schedule["detail"] == {"crons": [["0 9 * * *", "0 18 * * *"], ["0 8 * * *"]]}
    assert login["detail"]["method"] == ["auto", "http"]


@pytest.mark.parametrize("path,data", [
    ("/accounts", {"company": "  ", "username": "n1", "password": "p"}),
    ("/accounts/ghost/relogin", {}),
    ("/accounts/verify", {"username": ""}),
    ("/settings/schedule", {"crons": ["not a cron"]}),
    ("/settings/thresholds", {"min_failed_accounts": "x"}),
    ("/notify/dingtalk", {"enabled": "on"}),
    ("/users", {"username": "x", "password": "long-enough-pw", "role": "viewer", "groups": ["group_a"]}),
])
def test_a_refused_form_logs_nothing(world, login_as, _isolated_audit, path, data):
    client = login_as(_client(), *BOSS)
    before = len(_audit(_isolated_audit))
    assert client.post(path, data=data).status_code < 400
    assert _audit(_isolated_audit)[before:] == []


def test_the_safety_net_catches_a_route_that_forgot(world, login_as, _isolated_audit):
    from web.app import app

    async def forgotten():
        return PlainTextResponse("changed something")

    app.add_api_route("/accounts/forgotten-write", forgotten, methods=["POST"])
    try:
        client = login_as(_client(), *BOSS)
        client.post("/accounts/forgotten-write")
    finally:
        app.router.routes[:] = [r for r in app.router.routes
                                if getattr(r, "path", None) != "/accounts/forgotten-write"]
    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] == "http.unaudited"]
    assert entry["target"] == "/accounts/forgotten-write" and entry["actor"] == "boss"


# --- logins ----------------------------------------------------------------------

def test_login_and_logout(web_client, login_as, _isolated_audit):
    login_as(web_client, *OPERATOR_A)
    web_client.post("/logout")
    login, logout = _audit(_isolated_audit)
    assert (login["action"], login["actor"], login["role"]) == ("auth.login", "opa", "operator")
    assert (logout["action"], logout["actor"]) == ("auth.logout", "opa")
    assert login["ip"] == "testclient"


def test_failed_logins_mask_names_that_belong_to_nobody(web_client, _isolated_audit):
    web_client.post("/login", data={"username": "opa", "password": "wrong-password"})
    web_client.post("/login", data={"username": "Sentinel-Typed-Password", "password": "x"})
    known, unknown = _audit(_isolated_audit)
    assert known["action"] == "auth.login_failed" and known["actor"] == "opa" and not known["ok"]
    assert unknown["actor"] == login_guard.mask("Sentinel-Typed-Password")
    assert "Sentinel-Typed-Password" not in json.dumps(_audit(_isolated_audit))


def test_lockout_is_logged_once(web_client, _isolated_audit):
    for _ in range(login_guard.MAX_FAILURES + 3):
        web_client.post("/login", data={"username": "opa", "password": "wrong-password"})
    actions = [e["action"] for e in _audit(_isolated_audit)]
    assert actions.count("auth.lockout") == 1
    # Attempts while locked out are refused before any check, and not logged.
    assert actions.count("auth.login_failed") == login_guard.MAX_FAILURES


def test_basic_auth_failures_are_logged(web_client, _isolated_audit):
    bad = base64.b64encode(f"{ADMIN[0]}:wrong".encode()).decode()
    web_client.get("/", headers={"Authorization": f"Basic {bad}"}, follow_redirects=False)
    (entry,) = _audit(_isolated_audit)
    assert entry["action"] == "auth.login_failed" and entry["detail"] == {"via": "basic"}
    good = base64.b64encode(f"{ADMIN[0]}:{ADMIN[1]}".encode()).decode()
    web_client.post("/settings/pause", headers={"Authorization": f"Basic {good}"})
    assert _audit(_isolated_audit)[-1]["via"] == "basic"


# --- reads that are logged --------------------------------------------------------

def test_attachment_downloads_are_logged_once_per_window(
        web_client, login_as, fake_accounts, tmp_path, monkeypatch, _isolated_audit):
    from web.services import attachments_svc

    audit_log._recent_downloads.clear()
    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", tmp_path)
    (tmp_path / "甲_公司" / "R1").mkdir(parents=True)
    (tmp_path / "甲_公司" / "R1" / "notice.pdf").write_bytes(b"%PDF-1.4 test")
    login_as(web_client, *OPERATOR_A)
    for headers in ({}, {"Range": "bytes=0-3"}, {}):
        assert web_client.get("/attachments/甲 公司/R1/notice.pdf", headers=headers).status_code < 400
    web_client.get("/attachments/丙公司/R1/notice.pdf")  # not theirs: refused, not logged
    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] != "auth.login"]
    assert entry["action"] == "data.attachment" and entry["company"] == "甲 公司"
    assert entry["target"] == "R1/notice.pdf"
    audit_log._recent_downloads.clear()


def test_csv_export_is_logged_with_its_filter(web_client, login_as, fake_accounts,
                                              isolated_snapshots, _isolated_audit):
    isolated_snapshots.write("甲 公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 7,
                                          "dataRegName": "n", "id": 1}])
    login_as(web_client, *OPERATOR_A)
    web_client.get("/records.csv?account=ua1&q=n")
    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] == "data.records_csv"]
    assert entry["detail"] == {"account": "ua1", "q": "n", "rows": 1}


# --- no secrets, ever --------------------------------------------------------------

def test_no_secret_ever_reaches_the_log(world, login_as, _isolated_audit, monkeypatch):
    S = "SENTINEL"
    client = login_as(_client(), *BOSS)
    client.post("/accounts", data={"company": "哨兵公司", "username": "s1",
                                   "password": f"{S}-platform-1", "group": "group_a"})
    client.post("/accounts/ua1/edit", data={"password": f"{S}-platform-2", "contact": "",
                                            "group": "group_a"})
    client.post("/accounts/verify", data={"username": "ua1", "password": f"{S}-platform-3"})
    client.post("/notify/dingtalk", data={"enabled": "on", "webhook": f"https://x/?access_token={S}1",
                                          "secret": f"{S}-sign"})
    client.post("/notify/wechat", data={"webhook": f"https://x/?key={S}2"})
    client.post("/notify/email", data={"smtp_password": f"{S}-smtp"})
    client.post("/users", data={"username": "lisi", "role": "viewer", "groups": ["group_a"],
                                "password": f"{S}-console-1"})
    client.post("/users/opa/password", data={"password": f"{S}-console-2",
                                             "password2": f"{S}-console-2"})
    client.post("/me/password", data={"current": BOSS[1], "password": f"{S}-console-3",
                                      "password2": f"{S}-console-3"})
    _client().post("/login", data={"username": f"{S}-typed-in-the-name-box", "password": "x"})
    _client().post("/login", data={"username": "lisi", "password": f"{S}-wrong-guess"})

    entries = _audit(_isolated_audit)
    assert len(entries) >= 11
    raw = "".join(f.read_text(encoding="utf-8") for f in _isolated_audit.glob("*.jsonl"))
    assert S not in raw
