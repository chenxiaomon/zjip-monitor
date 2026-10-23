"""/api/records: the records page's own filters, and pages of 50 on request.

The point of record_matches() is that a search typed on the phone finds
exactly what the same search finds on the console's 登记记录 page.
"""

import pytest

from tests.conftest import ADMIN
from web.routes import records as records_routes


@pytest.fixture
def world(fake_accounts, isolated_snapshots, isolated_settings):
    isolated_snapshots.write("甲 公司", [
        {"dataRegNo": "DIP-A1", "dataRegName": "甲的客流数据", "dataRegAuditStatus": 2,
         "dataRegApplyTime": 1767225600000, "dataRegApplyName": "张三"},
        {"dataRegNo": "DIP-A2", "dataRegName": "甲的销售数据", "dataRegAuditStatus": 7,
         "dataRegApplyTime": 1767312000000},
    ])
    isolated_snapshots.write("乙·公司", [
        {"dataRegNo": "DIP-B1", "dataRegName": "乙的客流数据", "dataRegAuditStatus": 1,
         "dataRegApplyTime": 1767398400000},
        {"dataRegNo": "DIP-B2", "dataRegName": "乙的物流数据", "dataRegAuditStatus": 2},
    ])
    isolated_snapshots.write("丙公司", [{"dataRegNo": "DIP-C1", "dataRegName": "丙的客流", "dataRegAuditStatus": 7}])
    return fake_accounts


@pytest.fixture
def admin(web_client):
    token = web_client.post("/api/login", json={"username": ADMIN[0], "password": ADMIN[1]}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("q,statuses", [
    ("", None),
    ("客流", None),
    ("dip-a", None),      # the keyword ignores case
    ("张三", None),        # the applicant's name is searched too
    ("ua2", None),         # and the platform login, as on the console
    ("", {2}),
    ("", {2, 7}),
    ("客流", {7}),
    ("没有这个词", None),
])
def test_the_phone_finds_what_the_console_finds(world, web_client, admin, q, statuses):
    console, _, _ = records_routes._build_records(world, None, frozenset(statuses) if statuses else None, q=q)
    query = {"q": q} if q else {}
    if statuses:
        query["status"] = ",".join(str(s) for s in sorted(statuses))
    phone = web_client.get("/api/records", params=query, headers=admin).json()["records"]
    assert sorted((r["company"], r["reg_no"]) for r in phone) == \
        sorted((r["company"], r["reg_no"]) for r in console)


def test_the_status_may_also_repeat(world, web_client, admin):
    phone = web_client.get("/api/records?status=2&status=1", headers=admin).json()["records"]
    assert sorted(r["reg_no"] for r in phone) == ["DIP-A1", "DIP-B1", "DIP-B2"]


def test_rows_carry_the_deadline_and_mine(world, web_client, admin):
    world[0].owner = "admin"
    rows = {r["reg_no"]: r for r in web_client.get("/api/records", headers=admin).json()["records"]}
    assert rows["DIP-A1"]["due"]["label"] == "截止日期未知" and rows["DIP-A1"]["due"]["urgency"] == "unknown"
    assert "reasons" not in rows["DIP-A1"]["due"]  # the full text is for the detail page
    assert rows["DIP-A2"]["due"] is None and rows["DIP-A2"]["cert"] is None
    assert rows["DIP-A1"]["mine"] is True and rows["DIP-B1"]["mine"] is False


def test_pages_of_fifty_on_request(fake_accounts, isolated_snapshots, isolated_settings, web_client, admin):
    isolated_snapshots.write("甲 公司", [
        {"dataRegNo": f"R{i:03d}", "dataRegName": f"数据{i}", "dataRegAuditStatus": 1,
         "dataRegApplyTime": 1767225600000 + i * 86400000}
        for i in range(120)
    ])

    def page(n):
        return web_client.get(f"/api/records?company=甲 公司&page={n}", headers=admin).json()

    first, last = page(1), page(3)
    assert (len(first["records"]), first["page"], first["pages"], first["total"]) == (50, 1, 3, 120)
    assert first["records"][0]["reg_no"] == "R119"  # newest application first
    assert (len(last["records"]), last["page"]) == (20, 3)
    assert page(99)["page"] == 3 and page("x")["page"] == 1
    assert len(web_client.get("/api/records?company=甲 公司", headers=admin).json()["records"]) == 120
