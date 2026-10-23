"""/api/record: one record as the console's detail drawer shows it."""

import pytest

from tests.conftest import ADMIN, API_KEY, GROUP_B_USER
from tests.test_web_record_drawer import NOTICE, record  # noqa: F401  (fixture)


def _headers(client, user) -> dict:
    token = client.post("/api/login", json={"username": user[0], "password": user[1]}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_the_phone_gets_what_the_drawer_shows(record, web_client):  # noqa: F811
    resp = web_client.get("/api/record", params={"company": "甲 公司", "reg_no": "DIP-A1"},
                          headers=_headers(web_client, ADMIN))
    assert resp.status_code == 200
    d = resp.json()
    assert (d["company"], d["reg_no"], d["name"]) == ("甲 公司", "DIP-A1", "甲的<数据>")  # JSON, not HTML
    assert (d["status_code"], d["status_label"], d["industry"]) == (2, "待补正", "制造业")
    assert d["due"]["deadline"] == "2099-10-12" and d["due"]["issued_on"] == "2099-09-21"
    assert "风险分级区间边界存在空档" in d["due"]["reasons"]
    assert [(n["kind_label"], n["current"]) for n in d["notices"]] == [("补正通知书", True), ("补正通知书", False)]
    assert d["attachments"] == [{"name": NOTICE, "ext": "pdf"}]
    assert d["archived"] == [{"name": "国家局登记证书.pdf", "archived_on": "2026-09-27"}]
    assert [(h["old_status_code"], h["new_status_code"]) for h in d["history"]] == [(2, 1), (1, 2)]  # newest first
    assert d["cert"] is None and d["detail_url"].endswith("/dregister/detail/DIP-A1")
    assert d["can"] == {"add": True, "scan": True, "reveal": True}


def test_the_platform_login_never_reaches_the_phone(record, web_client):  # noqa: F811
    resp = web_client.get("/api/record", params={"company": "甲 公司", "reg_no": "DIP-A1"},
                          headers=_headers(web_client, ADMIN))
    assert "ua1" not in resp.text


def test_other_groups_and_unknown_records_are_the_same_404(record, web_client):  # noqa: F811
    headers = _headers(web_client, GROUP_B_USER)
    for params in ({"company": "甲 公司", "reg_no": "DIP-A1"},        # another group's record
                   {"company": "丙公司", "reg_no": "NOPE"},            # no such record
                   {"company": "丙公司", "reg_no": "<script>"},        # never echoed back
                   {}):
        resp = web_client.get("/api/record", params=params, headers=headers)
        assert resp.status_code == 404 and resp.json() == {"error": "记录不存在或无权查看"}
    ok = web_client.get("/api/record", params={"company": "丙公司", "reg_no": "DIP-B1"}, headers=headers)
    assert ok.status_code == 200 and ok.json()["can"] == {"add": False, "scan": False, "reveal": False}


@pytest.mark.parametrize("headers", [{"X-API-Key": API_KEY}, None])
def test_only_a_personal_sign_in_opens_a_record(record, web_client, headers):  # noqa: F811
    """The admin key shipped in old mini program builds."""
    kwargs = {"headers": headers} if headers else {"auth": ADMIN}
    resp = web_client.get("/api/record", params={"company": "甲 公司", "reg_no": "DIP-A1"}, **kwargs)
    assert resp.status_code == 403 and resp.json() == {"error": "请用小程序个人账号登录"}
