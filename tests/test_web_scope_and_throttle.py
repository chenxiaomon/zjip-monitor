"""Group-user scope on the console, and the two places it used to leak.

* a single-account scan takes the GLOBAL run lock and one of two shared
  workers, so any user able to click it could starve the daemon's round;
* attachment permission was checked against the raw company name while the
  file was resolved through the sanitized one.
"""

import pytest

from tests.conftest import ADMIN, API_KEY, GROUP_A_USER
from web.routes import accounts as accounts_routes


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    """Never launch a real platform login from a route test."""
    monkeypatch.setattr(accounts_routes, "spawn", lambda coro: coro.close())
    accounts_routes._web_logins.clear()
    yield
    accounts_routes._web_logins.clear()


def test_group_user_cannot_trigger_a_scan(web_client, login_as, fake_accounts, isolated_snapshots):
    login_as(web_client, *GROUP_A_USER)
    assert web_client.post("/accounts/ua1/scan").status_code == 403


def test_admin_can_trigger_a_scan(web_client, login_as, fake_accounts, isolated_snapshots):
    login_as(web_client, *ADMIN)
    assert web_client.post("/accounts/ua1/scan").status_code == 200


def test_group_user_relogin_is_rate_limited(web_client, login_as, fake_accounts, isolated_snapshots):
    login_as(web_client, *GROUP_A_USER)

    first = web_client.post("/accounts/ua1/relogin")
    assert first.status_code == 200
    assert "过于频繁" not in first.text

    second = web_client.post("/accounts/ua1/relogin")
    assert "过于频繁" in second.text


def test_admin_relogin_is_not_rate_limited(web_client, login_as, fake_accounts, isolated_snapshots):
    """The admin is the one fixing a password and needs the retry."""
    login_as(web_client, *ADMIN)
    for _ in range(3):
        assert "过于频繁" not in web_client.post("/accounts/ua1/relogin").text


def test_group_user_sees_no_scan_button(web_client, login_as, fake_accounts, isolated_snapshots):
    login_as(web_client, *GROUP_A_USER)
    body = web_client.get("/accounts").text
    assert "/relogin" in body
    assert "/scan\"" not in body and "/scan'" not in body


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------

def test_attachment_scope_is_checked_on_the_on_disk_key(
    web_client, login_as, fake_accounts, tmp_path, monkeypatch
):
    """"甲 公司" (group_a) and "甲_公司" (another group) share a directory once
    sanitized; the check must use that same key, not the display name."""
    from web.services import attachments_svc

    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", tmp_path)
    rec_dir = tmp_path / "甲_公司" / "R1"
    rec_dir.mkdir(parents=True)
    # ASCII filename on purpose: the download-name header uses a latin-1
    # fallback for non-ASCII names that httpx's test transport refuses to
    # decode (a client-side limit, not a server bug — see commit 99d679d).
    (rec_dir / "notice.pdf").write_bytes(b"%PDF-1.4 test")

    login_as(web_client, *GROUP_A_USER)
    # Its own company, spelled as stored: allowed.
    assert web_client.get("/attachments/甲 公司/R1/notice.pdf").status_code == 200
    # A company it may not see: refused, even though it maps to the same dir.
    assert web_client.get("/attachments/丙公司/R1/notice.pdf").status_code == 404


def test_api_returns_only_visible_accounts(web_client, isolated_snapshots, fake_accounts):
    isolated_snapshots.write("甲 公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 7,
                                          "dataRegName": "n", "id": 1}])
    isolated_snapshots.write("已删除的公司", [{"dataRegNo": "R9", "dataRegAuditStatus": 7,
                                              "dataRegName": "n", "id": 9}])

    data = web_client.get("/api/status", headers={"X-API-Key": API_KEY}).json()
    companies = {a["company"] for a in data["accounts"]}

    assert "甲 公司" in companies
    assert "已删除的公司" not in companies  # orphan snapshot, no account


def test_api_survives_a_broken_timestamp(web_client, isolated_snapshots, fake_accounts):
    isolated_snapshots.write("甲 公司", [{
        "dataRegNo": "R1", "dataRegAuditStatus": 7, "dataRegName": "n",
        "id": 1, "dataRegApplyTime": 10 ** 18,
    }])
    resp = web_client.get("/api/records", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    assert resp.json()["records"][0]["apply_date"] == ""
