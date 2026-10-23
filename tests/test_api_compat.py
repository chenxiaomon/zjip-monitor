"""Old mini program builds keep getting what they always got.

The builds already on colleagues' phones call /api/status, /api/records and
/api/me without ?scope=, ?q= or ?page=. Everything those builds read has to
stay: the same companies (everything visible, the caller's own first — even
for someone who owns companies, whose console starts on 我负责的), the same
keys with the same values, and every row at once.
"""

import pytest

from tests.conftest import API_KEY, OPERATOR_A

OLD_ACCOUNT_KEYS = {"company", "dot", "total", "status_counts", "last_check", "owner", "mine"}
OLD_RECORD_KEYS = {"company", "reg_no", "name", "apply_date", "status_code", "status_label", "detail_url"}
JAN_2026 = 1767225600000  # 2026-01-01 08:00 Asia/Shanghai
JAN_2025 = 1735660800000  # 2025-01-01 00:00 Asia/Shanghai


@pytest.fixture
def world(fake_accounts, isolated_snapshots, isolated_settings):
    fake_accounts[1].owner = "opa"  # 乙·公司 is opa's; 甲 公司 is in the same group
    isolated_snapshots.write("甲 公司", [
        {"dataRegNo": "RA1", "dataRegName": "甲一", "dataRegAuditStatus": 2, "dataRegApplyTime": JAN_2026},
        {"dataRegNo": "RA2", "dataRegName": "甲二", "dataRegAuditStatus": 7, "dataRegApplyTime": JAN_2025},
    ])
    isolated_snapshots.write("乙·公司", [
        {"dataRegNo": "RB1", "dataRegName": "乙一", "dataRegAuditStatus": 7, "dataRegApplyTime": JAN_2026 + 86400000},
    ])
    isolated_snapshots.write("丙公司", [{"dataRegNo": "RC1", "dataRegName": "丙一", "dataRegAuditStatus": 1}])
    return fake_accounts


def _get(client, path, headers):
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def opa(web_client):
    token = web_client.post("/api/login", json={"username": OPERATOR_A[0],
                                                "password": OPERATOR_A[1]}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_status_keeps_its_old_shape(world, web_client, opa):
    d = _get(web_client, "/api/status", opa)
    assert (d["total_records"], d["success_count"], d["pending_correction"]) == (3, 2, 1)
    assert len(d["status_labels"]) == 17
    by_company = {a["company"]: a for a in d["accounts"]}
    assert [a["company"] for a in d["accounts"]] == ["乙·公司", "甲 公司"]  # all visible, mine first
    for a in d["accounts"]:
        assert OLD_ACCOUNT_KEYS <= set(a)
    assert {k: by_company["甲 公司"][k] for k in OLD_ACCOUNT_KEYS} == {
        "company": "甲 公司", "dot": "amber", "total": 2, "status_counts": {"2": 1, "7": 1},
        "last_check": "01-01 09:00", "owner": "", "mine": False,
    }
    assert {k: by_company["乙·公司"][k] for k in ("dot", "total", "status_counts", "owner", "mine")} == {
        "dot": "green", "total": 1, "status_counts": {"7": 1}, "owner": "opa", "mine": True,
    }


def test_the_year_filter_works_as_before(world, web_client, opa):
    d = _get(web_client, "/api/status?year=2025", opa)
    assert {a["company"]: a["total"] for a in d["accounts"]} == {"乙·公司": 0, "甲 公司": 1}
    assert d["total_records"] == 1


def test_records_keep_their_old_shape(world, web_client, opa):
    d = _get(web_client, "/api/records", opa)
    assert set(d) == {"records", "total"}  # no paging unless asked
    assert [r["reg_no"] for r in d["records"]] == ["RB1", "RA1", "RA2"]  # newest application first
    assert d["total"] == 3
    for r in d["records"]:
        assert OLD_RECORD_KEYS <= set(r)
    ra1 = next(r for r in d["records"] if r["reg_no"] == "RA1")
    assert {k: ra1[k] for k in OLD_RECORD_KEYS} == {
        "company": "甲 公司", "reg_no": "RA1", "name": "甲一", "apply_date": "2026-01-01",
        "status_code": 2, "status_label": "待补正", "detail_url": "https://example.invalid/dregister/detail/RA1",
    }


@pytest.mark.parametrize("query,expected", [
    ("company=甲 公司", ["RA1", "RA2"]),
    ("status=7", ["RB1", "RA2"]),
    ("year=2025", ["RA2"]),
    ("company=甲 公司&status=2", ["RA1"]),
])
def test_record_filters_work_as_before(world, web_client, opa, query, expected):
    assert [r["reg_no"] for r in _get(web_client, f"/api/records?{query}", opa)["records"]] == expected


def test_me_keeps_the_user(world, web_client, opa):
    assert _get(web_client, "/api/me", opa)["user"] == {
        "username": "opa", "display_name": "", "role": "operator", "groups": ["group_a"],
    }


def test_the_key_lane_still_reads_everything(world, web_client):
    key = {"X-API-Key": API_KEY}
    assert {a["company"] for a in _get(web_client, "/api/status", key)["accounts"]} == {"甲 公司", "乙·公司", "丙公司"}
    assert _get(web_client, "/api/records", key)["total"] == 4
