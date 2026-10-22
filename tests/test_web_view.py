"""/view — the read-only board — is behind the console login and shows each
user only the companies they may see."""

import pytest

from tests.conftest import ADMIN, GROUP_A_USER


@pytest.fixture
def two_companies(isolated_snapshots, fake_accounts):
    for company in ("甲 公司", "丙公司"):
        isolated_snapshots.write(company, [{"dataRegNo": "R1", "dataRegAuditStatus": 7,
                                            "dataRegName": "n", "id": 1}])


@pytest.mark.parametrize("path", ["/view/", "/view/partial", "/view/?t=anything"])
def test_anonymous_visitors_are_sent_to_the_login(web_client, two_companies, path):
    resp = web_client.get(path, follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == "/login"


def test_a_colleague_sees_only_their_companies(web_client, login_as, two_companies):
    login_as(web_client, *GROUP_A_USER)
    body = web_client.get("/view/").text
    assert "甲 公司" in body and "丙公司" not in body
    assert "丙公司" not in web_client.get("/view/partial").text


def test_the_admin_sees_everything(web_client, login_as, two_companies):
    login_as(web_client, *ADMIN)
    body = web_client.get("/view/").text
    assert "甲 公司" in body and "丙公司" in body


def test_view_responses_are_not_indexable(web_client, login_as, two_companies):
    login_as(web_client, *ADMIN)
    resp = web_client.get("/view/")
    assert resp.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert resp.headers["Cache-Control"] == "no-store"


def test_robots_txt_disallows_everything(web_client):
    assert web_client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
