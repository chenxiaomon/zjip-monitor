"""去平台办理: show a company's platform credentials to whoever handles it —
logged every time, never to 只读 users, never outside one's groups."""

import json

import pytest

from tests.conftest import ADMIN, GROUP_A_USER, OPERATOR_A


def _audit(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def operator(web_client, login_as, fake_accounts, isolated_snapshots, isolated_settings):
    isolated_settings.write({"base_url": "https://www.zjip.org.cn",
                             "groups": {"group_a": "来源一", "group_b": "来源二"}})
    return login_as(web_client, *OPERATOR_A)


def test_an_operator_gets_their_companys_credentials(operator, _isolated_audit):
    resp = operator.post("/accounts/ua1/platform-access")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert 'value="ua1"' in resp.text and 'value="p1"' in resp.text
    assert 'type="password"' in resp.text  # masked until 显示
    assert 'href="https://www.zjip.org.cn/user/login"' in resp.text
    assert "modal" in resp.text

    (entry,) = [e for e in _audit(_isolated_audit) if e["action"] == "account.reveal"]
    assert entry["actor"] == "opa" and entry["company"] == "甲 公司" and entry["target"] == "ua1"
    assert '"p1"' not in json.dumps(_audit(_isolated_audit))


def test_every_reveal_is_logged(operator, _isolated_audit):
    for _ in range(3):
        operator.post("/accounts/ua1/platform-access")
    assert [e["action"] for e in _audit(_isolated_audit)].count("account.reveal") == 3


def test_the_drawer_variant_links_the_record(operator):
    resp = operator.post("/accounts/ua1/platform-access", data={"reg_no": "R123", "inline": "1"})
    assert "drawer-section" in resp.text and "modal" not in resp.text
    assert 'href="https://www.zjip.org.cn/dregister/detail/R123"' in resp.text


def test_a_strange_reg_no_is_never_put_in_a_link(operator):
    resp = operator.post("/accounts/ua1/platform-access",
                         data={"reg_no": '"><script>x</script>', "inline": "1"})
    assert "dregister/detail" not in resp.text and "<script>x" not in resp.text


def test_another_groups_company_is_not_found(operator, _isolated_audit):
    assert operator.post("/accounts/ub1/platform-access").status_code == 404
    assert not [e for e in _audit(_isolated_audit) if e["action"] == "account.reveal"]


def test_viewers_never_see_a_password(web_client, login_as, fake_accounts, isolated_snapshots):
    login_as(web_client, *GROUP_A_USER)
    assert web_client.post("/accounts/ua1/platform-access").status_code == 403
    assert "platform-access" not in web_client.get("/accounts").text


def test_admins_reach_any_company(web_client, login_as, fake_accounts, isolated_snapshots):
    login_as(web_client, *ADMIN)
    assert 'value="p3"' in web_client.post("/accounts/ub1/platform-access").text


def test_the_button_is_on_rows_and_in_the_drawer_for_operators(operator, isolated_snapshots):
    isolated_snapshots.write("甲 公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 2,
                                          "dataRegName": "n", "id": 1}])
    assert 'hx-post="/accounts/ua1/platform-access"' in operator.get("/accounts").text
    drawer = operator.get("/partials/record?account=ua1&reg_no=R1").text
    assert "/accounts/ua1/platform-access" in drawer and 'id="platform-access"' in drawer
    assert 'value="p1"' not in drawer  # nothing is shown until the button is pressed


def test_cross_site_requests_are_refused(operator):
    resp = operator.post("/accounts/ua1/platform-access", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
