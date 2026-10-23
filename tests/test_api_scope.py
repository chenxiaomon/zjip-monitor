"""?scope=mine|all — the console's 我负责的 / 全部 for the mini program.

A view, never a permission: it narrows what is listed to the companies one
follows up, it never widens anything past what the caller may see, and a
single record of any visible company still opens.
"""

import json
from datetime import datetime, timezone

import pytest

from tests.conftest import GROUP_A_USER, GROUP_B_USER, OPERATOR_A


@pytest.fixture
def owned(fake_accounts, isolated_snapshots, isolated_settings, monkeypatch, tmp_path):
    """opa (operator, group_a) owns 甲 公司; 乙·公司 is also group_a; 丙公司 is group_b."""
    from web.services import events as ev

    fake_accounts[0].owner = "opa"
    for company, reg in (("甲 公司", "RA"), ("乙·公司", "RB"), ("丙公司", "RC")):
        isolated_snapshots.write(company, [{"dataRegNo": reg, "dataRegAuditStatus": 2,
                                            "dataRegName": f"数据{reg}", "id": reg}])
    now = datetime.now(timezone.utc).isoformat()
    events = [
        {"account": acct, "company": company, "timestamp": now, "added": [], "removed": [],
         "changed": [{"reg_no": reg, "name": "n", "old_status_code": 1, "new_status_code": 2,
                      "old_status": "待审核", "new_status": "待补正"}]}
        for acct, company, reg in (("ua1", "甲 公司", "RA"), ("ua2", "乙·公司", "RB"), ("ub1", "丙公司", "RC"))
    ]
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    monkeypatch.setattr(ev, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ev, "_EVENTS_FILE", tmp_path / "events.jsonl")
    ev._cache.clear()
    yield fake_accounts
    ev._cache.clear()


def _headers(client, user) -> dict:
    token = client.post("/api/login", json={"username": user[0], "password": user[1]}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _companies(client, headers, path) -> list[str]:
    d = client.get(path, headers=headers).json()
    if "accounts" in d:
        return sorted(a["company"] for a in d["accounts"])
    if "records" in d:
        return sorted({r["company"] for r in d["records"]})
    if "rows" in d:
        return sorted({r["company"] for r in d["rows"]})
    return sorted({c["company"] for c in d["corrections"]})


ENDPOINTS = ["/api/status", "/api/records", "/api/changes", "/api/deadlines"]


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("scope,expected", [
    ("mine", ["甲 公司"]),
    ("all", ["乙·公司", "甲 公司"]),
    ("", ["乙·公司", "甲 公司"]),          # what every build before the switch sends
    ("everything", ["乙·公司", "甲 公司"]),  # anything else is 全部 too
])
def test_the_scope_narrows_every_list(owned, web_client, path, scope, expected):
    headers = _headers(web_client, OPERATOR_A)
    query = f"?scope={scope}" if scope else ""
    assert _companies(web_client, headers, path + query) == expected


@pytest.mark.parametrize("path", ENDPOINTS)
def test_owning_nothing_means_everything_visible(owned, web_client, path):
    headers = _headers(web_client, GROUP_A_USER)
    assert _companies(web_client, headers, path + "?scope=mine") == ["乙·公司", "甲 公司"]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_the_scope_never_widens_past_the_group(owned, web_client, path):
    headers = _headers(web_client, GROUP_B_USER)
    assert _companies(web_client, headers, path + "?scope=all") == ["丙公司"]


def test_status_and_me_report_the_switch(owned, web_client):
    headers = _headers(web_client, OPERATOR_A)
    assert web_client.get("/api/status?scope=mine", headers=headers).json()["scope"] == {
        "mine": True, "owned": 1, "visible": 2}
    assert web_client.get("/api/me", headers=headers).json()["scope"] == {
        "mine": False, "owned": 1, "visible": 2}
    assert web_client.get("/api/me?scope=mine", headers=headers).json()["scope"]["mine"] is True


def test_a_record_outside_my_companies_still_opens(owned, web_client):
    headers = _headers(web_client, OPERATOR_A)
    resp = web_client.get("/api/record?scope=mine&company=乙·公司&reg_no=RB", headers=headers)
    assert resp.status_code == 200 and resp.json()["reg_no"] == "RB"


def test_the_console_cookie_does_not_leak_into_the_api(owned, web_client, login_as):
    """The console's 全部 cookie and the phone's ?scope= are separate choices."""
    login_as(web_client, *OPERATOR_A)
    web_client.get("/scope?to=all&next=/")
    headers = _headers(web_client, OPERATOR_A)
    assert _companies(web_client, headers, "/api/status?scope=mine") == ["甲 公司"]
    web_client.get("/scope?to=mine&next=/")
    assert _companies(web_client, headers, "/api/status") == ["乙·公司", "甲 公司"]
