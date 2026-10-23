"""/api/attachment: opening a 通知书 on the phone, as the console's download does.

The files here have ASCII names: Starlette's TestClient decodes response
headers as UTF-8 and httpx re-encodes them as ASCII, so a Chinese download
name in Content-Disposition breaks the client, not the app.
"""

import json

import pytest

from src import attachments
from tests.conftest import ADMIN, API_KEY, GROUP_A_USER, GROUP_B_USER
from web.services import attachments_svc


@pytest.fixture
def files(fake_accounts, isolated_snapshots, monkeypatch):
    isolated_snapshots.write("甲 公司", [{"dataRegNo": "R1", "dataRegName": "Data A", "dataRegAuditStatus": 2}])
    isolated_snapshots.write("丙公司", [{"dataRegNo": "R9", "dataRegName": "Data C", "dataRegAuditStatus": 7}])
    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", attachments._ATTACHMENTS_DIR)
    for company, reg, name, body in (("甲 公司", "R1", "notice.pdf", b"%PDF"),
                                     ("丙公司", "R9", "secret.pdf", b"%PDF secret")):
        rec = attachments._ATTACHMENTS_DIR / attachments._safe_name(company) / reg
        rec.mkdir(parents=True)
        (rec / name).write_bytes(body)


def _token(client, user) -> str:
    return client.post("/api/login", json={"username": user[0], "password": user[1]}).json()["token"]


def _get(client, token, name="notice.pdf", company="甲 公司", reg_no="R1"):
    return client.get("/api/attachment", params={"company": company, "reg_no": reg_no, "name": name},
                      headers={"Authorization": f"Bearer {token}"})


def _attachment_lines(audit_dir):
    return [e for f in sorted(audit_dir.glob("*.jsonl"))
            for e in map(json.loads, f.read_text(encoding="utf-8").splitlines())
            if e["action"] == "data.attachment"]


def test_a_colleague_opens_their_companys_notice(files, web_client, _isolated_audit):
    token = _token(web_client, GROUP_A_USER)
    resp = _get(web_client, token)
    assert resp.status_code == 200 and resp.content == b"%PDF"
    assert resp.headers["content-type"] == "application/pdf"
    assert "Data_A_R1_notice.pdf" in resp.headers["content-disposition"]  # the console's download name
    _get(web_client, token)  # a PDF viewer fetching the same file again
    (line,) = _attachment_lines(_isolated_audit)
    assert (line["actor"], line["via"], line["company"], line["target"]) == ("ga", "api", "甲 公司", "R1/notice.pdf")


def test_another_groups_file_reads_like_a_missing_one(files, web_client):
    token = _token(web_client, GROUP_B_USER)
    theirs, missing = _get(web_client, token), _get(web_client, token, name="nope.pdf", company="丙公司", reg_no="R9")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json() == {"error": "附件不存在"}


@pytest.mark.parametrize("name", [
    "../../丙公司/R9/secret.pdf",      # stays inside data/attachments, but another company's
    "..\\..\\丙公司\\R9\\secret.pdf",
    "..", ".", "",
])
def test_the_name_cannot_climb_into_another_company(files, web_client, name):
    resp = _get(web_client, _token(web_client, GROUP_A_USER), name=name)
    assert resp.status_code == 404 and b"secret" not in resp.content


def test_a_token_in_the_query_string_is_not_a_sign_in(files, web_client):
    token = _token(web_client, ADMIN)
    resp = web_client.get("/api/attachment", params={"company": "甲 公司", "reg_no": "R1",
                                                     "name": "notice.pdf", "token": token})
    assert resp.status_code == 401


def test_the_admin_key_cannot_download(files, web_client):
    resp = web_client.get("/api/attachment", params={"company": "甲 公司", "reg_no": "R1", "name": "notice.pdf"},
                          headers={"X-API-Key": API_KEY})
    assert resp.status_code == 403


def test_the_console_link_still_works(files, web_client, login_as):
    login_as(web_client, *ADMIN)
    resp = web_client.get("/attachments/甲 公司/R1/notice.pdf")
    assert resp.status_code == 200 and resp.content == b"%PDF"
