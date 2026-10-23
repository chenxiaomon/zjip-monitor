"""/api/deadlines: the dashboard's 待办 and the console's 期限日历 on the phone."""

import json
from datetime import timedelta

import pytest

from src import attachments, todo
from tests.conftest import ADMIN, API_KEY, GROUP_B_USER
from web.deps import cert_label, due_label


@pytest.fixture
def world(fake_accounts, isolated_snapshots, isolated_settings):
    today = todo.today_local()

    def facts(company, reg, notices):
        rec = attachments._ATTACHMENTS_DIR / attachments._safe_name(company) / reg
        rec.mkdir(parents=True)
        (rec / ".sync.json").write_text(json.dumps({"facts": notices}, ensure_ascii=False), encoding="utf-8")

    isolated_snapshots.write("甲 公司", [
        {"dataRegNo": "RA1", "dataRegName": "甲待补正", "dataRegAuditStatus": 2},
        {"dataRegNo": "RA2", "dataRegName": "甲已登记", "dataRegAuditStatus": 7},
    ])
    isolated_snapshots.write("乙·公司", [{"dataRegNo": "RB1", "dataRegName": "乙待补正", "dataRegAuditStatus": 2}])
    isolated_snapshots.write("丙公司", [{"dataRegNo": "RC1", "dataRegName": "丙待补正", "dataRegAuditStatus": 2}])
    facts("甲 公司", "RA1", {"补正通知书.pdf": {"kind": "补正", "deadline": (today + timedelta(days=2)).isoformat(),
                                                "issued_on": today.isoformat(), "reasons": "缺字段说明\n其它"}})
    facts("甲 公司", "RA2", {"登记公告.pdf": {"kind": "公告", "valid_from": "2024-01-01",
                                              "valid_to": (today + timedelta(days=100)).isoformat()}})
    # 乙: no notice parsed → the deadline is unknown, and it must still show
    fake_accounts[0].owner = "admin"
    return fake_accounts


def _get(client, user, path="/api/deadlines"):
    token = client.post("/api/login", json={"username": user[0], "password": user[1]}).json()["token"]
    resp = client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return resp


def test_the_phone_gets_the_same_deadlines_as_the_console(world, web_client):
    d = _get(web_client, ADMIN).json()
    visible = world  # the admin sees all three companies
    built, dated = todo.build(visible), todo.deadline_items(visible)

    assert [(c["company"], c["reg_no"], c["deadline"], c["urgency"]) for c in d["corrections"]] == \
        [(c["company"], c["reg_no"], c["deadline"], c["urgency"]) for c in built["corrections"]]
    assert [(r["reg_no"], r["valid_to"]) for r in d["renewals"]] == [(r["reg_no"], r["valid_to"]) for r in built["renewals"]]
    assert [(i["kind"], i["date"], i["reg_no"]) for i in d["items"]] == \
        [(i["kind"], i["date"], i["reg_no"]) for i in dated["items"]]
    assert sorted(i["reg_no"] for i in d["undated"]) == ["RB1", "RC1"]


def test_each_entry_carries_its_label_and_whose_it_is(world, web_client):
    d = _get(web_client, ADMIN).json()
    (ra1,) = [c for c in d["corrections"] if c["reg_no"] == "RA1"]
    assert ra1["label"] == due_label(2, ra1["deadline"]) and ra1["urgency"] == "red"
    assert ra1["summary"] == "缺字段说明" and ra1["mine"] is True
    (renewal,) = d["renewals"]
    assert renewal["label"] == cert_label(100, renewal["valid_to"]) and renewal["urgency"] == "normal"
    (cert,) = [i for i in d["items"] if i["kind"] == "certificate"]
    assert cert["label"] == renewal["label"]
    (unknown,) = [i for i in d["undated"] if i["reg_no"] == "RB1"]
    assert unknown["label"] == "截止日期未知" and unknown["mine"] is False


def test_no_platform_login_and_no_other_group(world, web_client):
    text = _get(web_client, GROUP_B_USER).text
    assert "RC1" in text
    for leak in ("ua1", "ua2", "ub1", "RA1", "RB1", "甲 公司"):
        assert leak not in text


def test_the_admin_key_gets_no_deadlines(world, web_client):
    resp = web_client.get("/api/deadlines", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 403
