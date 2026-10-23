"""/api/changes: the mini program's 最近变化 list and its 「有新变化」 pop-up."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import ADMIN, OPERATOR_A
from web.services import events as ev


def _event(account, company, at, reg_no, *, old=1, new=7):
    return {"account": account, "company": company, "timestamp": at.isoformat(),
            "added": [], "removed": [],
            "changed": [{"reg_no": reg_no, "name": f"{reg_no}数据集", "old_status_code": old,
                         "new_status_code": new, "old_status": "待审核", "new_status": "登记成功"}]}


@pytest.fixture
def log(tmp_path, monkeypatch, fake_accounts):
    """Appends events to a temp events.jsonl the way src.main does."""
    monkeypatch.setattr(ev, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ev, "_EVENTS_FILE", tmp_path / "events.jsonl")
    ev._cache.clear()

    def append(*events):
        with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    yield append
    ev._cache.clear()


@pytest.fixture
def opa(web_client):
    token = web_client.post("/api/login", json={"username": OPERATOR_A[0],
                                                "password": OPERATOR_A[1]}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _get(client, headers, since=None):
    resp = client.get("/api/changes", headers=headers, params={"since": since} if since else None)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_colleague_gets_only_their_companies_changes(web_client, opa, log, fake_accounts):
    now = datetime.now(timezone.utc)
    fake_accounts[0].owner = "opa"
    log(_event("ua1", "甲 公司", now - timedelta(hours=3), "RA1"),
        _event("ub1", "丙公司", now - timedelta(hours=2), "RB1"),
        _event("ua2", "乙·公司", now - timedelta(hours=1), "RA2", old=1, new=2))

    data = _get(web_client, opa)
    assert [r["reg_no"] for r in data["rows"]] == ["RA2", "RA1"]  # newest first, no 丙公司
    assert data["total"] == 2
    row = data["rows"][1]
    assert row["company"] == "甲 公司" and row["type"] == "changed" and row["mine"] is True
    assert (row["old_status"], row["new_status"]) == ("待审核", "登记成功")
    assert (row["old_status_code"], row["new_status_code"]) == (1, 7)
    assert row["detail_url"].endswith("/dregister/detail/RA1")
    assert data["rows"][0]["mine"] is False
    assert "ua1" not in json.dumps(data)  # the platform login never leaves the server


def test_the_admin_sees_every_company(web_client, log):
    now = datetime.now(timezone.utc)
    log(_event("ua1", "甲 公司", now - timedelta(hours=2), "RA1"),
        _event("ub1", "丙公司", now - timedelta(hours=1), "RB1"))
    token = web_client.post("/api/login", json={"username": ADMIN[0],
                                                "password": ADMIN[1]}).json()["token"]
    data = _get(web_client, {"Authorization": f"Bearer {token}"})
    assert [r["reg_no"] for r in data["rows"]] == ["RB1", "RA1"]


def test_since_and_the_overlap_catch_an_event_written_late(web_client, opa, log):
    """A scan's event is stamped at fetch time but written later. One stamped
    before the phone's last look, yet written after it, must still pop up."""
    now = datetime.now(timezone.utc)
    log(_event("ua1", "甲 公司", now - timedelta(days=2), "OLD"),
        _event("ua1", "甲 公司", now - timedelta(seconds=30), "SEEN"))
    first = _get(web_client, opa)
    seen = {r["key"] for r in first["rows"]}
    assert datetime.fromisoformat(first["next_since"]) < now

    log(_event("ua2", "乙·公司", now - timedelta(seconds=40), "LATE"))  # written after the look
    second = _get(web_client, opa, first["next_since"])
    assert [r["reg_no"] for r in second["rows"]] == ["LATE", "SEEN"]  # OLD is outside the window
    assert [r["reg_no"] for r in second["rows"] if r["key"] not in seen] == ["LATE"]


def test_rows_of_one_scan_share_a_key(web_client, opa, log):
    now = datetime.now(timezone.utc)
    event = _event("ua1", "甲 公司", now, "RA1")
    event["added"] = [{"dataRegNo": "RA9", "dataRegName": "新数据集", "dataRegAuditStatus": "1"}]
    log(event)
    rows = _get(web_client, opa)["rows"]
    assert [(r["reg_no"], r["type"]) for r in rows] == [("RA1", "changed"), ("RA9", "added")]
    assert rows[0]["key"] == rows[1]["key"]
    assert rows[1]["new_status_code"] == 1  # the platform sends "1" as often as 1


def test_the_window_is_thirty_days_and_capped(web_client, opa, log, monkeypatch):
    from web.routes import api

    now = datetime.now(timezone.utc)
    log(_event("ua1", "甲 公司", now - timedelta(days=40), "OLD"),
        *[_event("ua1", "甲 公司", now - timedelta(hours=h + 1), f"R{h}") for h in range(5)])
    long_ago = (now - timedelta(days=90)).isoformat()
    assert "OLD" not in [r["reg_no"] for r in _get(web_client, opa, long_ago)["rows"]]

    monkeypatch.setattr(api, "CHANGES_LIMIT", 3)
    data = _get(web_client, opa)
    assert len(data["rows"]) == 3 and data["total"] == 5


def test_a_malformed_since_is_refused(web_client, opa, log):
    resp = web_client.get("/api/changes", headers=opa, params={"since": "yesterday"})
    assert resp.status_code == 400 and resp.json()["error"]


def test_no_token_no_changes(web_client, log):
    assert web_client.get("/api/changes").status_code == 401
