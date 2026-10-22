"""Reading the operation log back: the /audit page and 最近登录."""

from datetime import datetime, timedelta, timezone

import pytest

from src import audit
from tests.conftest import ADMIN, OPERATOR_A
from web.services import audit_log

NOW = datetime.now(timezone.utc)


def _at(monkeypatch, ts):
    monkeypatch.setattr(audit, "_now", lambda: ts)


@pytest.fixture
def history(monkeypatch, _isolated_audit):
    """A spread of lines across two months, written through the real writer."""
    audit_log._parsed.clear()
    lines = [
        (NOW - timedelta(days=40), "opa", "auth.login", {}),
        (NOW - timedelta(days=3), "opa", "auth.login", {}),
        (NOW - timedelta(days=2), "opa", "account.edit",
         {"company": "甲 公司", "target": "ua1", "detail": {"password_changed": True,
                                                         "contact": ["A1", "B1"]}}),
        (NOW - timedelta(days=1), "boss", "settings.schedule",
         {"detail": {"crons": [["0 9 * * *"], ["0 8 * * *"]]}}),
        (NOW - timedelta(hours=5), "本机归档", "archive.release",
         {"company": "乙_公司", "target": "R9", "detail": {"files": 3, "bytes": 1024}}),
        (NOW - timedelta(hours=1), "ga", "auth.login_failed", {"ok": False}),
    ]
    for ts, actor, action, extra in lines:
        _at(monkeypatch, ts)
        audit.record(actor, action, **extra)
    monkeypatch.undo()
    yield
    audit_log._parsed.clear()


def test_query_filters_and_orders_newest_first(history):
    week = NOW - timedelta(days=7)
    assert [e["action"] for e in audit_log.query(since=week)] == [
        "auth.login_failed", "archive.release", "settings.schedule", "account.edit", "auth.login",
    ]
    assert len(audit_log.query(since=NOW - timedelta(days=60))) == 6  # across month files
    assert [e["actor"] for e in audit_log.query(since=week, actor="opa")] == ["opa", "opa"]
    assert [e["action"] for e in audit_log.query(since=week, category="settings")] == ["settings.schedule"]
    assert [e["action"] for e in audit_log.query(since=week, failures_only=True)] == ["auth.login_failed"]
    assert audit_log.query(since=week, until=NOW - timedelta(days=2, hours=-1))[0]["action"] == "account.edit"


def test_keyword_matches_display_text_and_archive_directory_names(history):
    week = NOW - timedelta(days=7)
    assert [e["action"] for e in audit_log.query(since=week, keyword="甲 公司")] == ["account.edit"]
    assert [e["action"] for e in audit_log.query(since=week, keyword="改了密码")] == ["account.edit"]
    # Archive lines carry the on-disk directory name; the display name finds them.
    assert [e["action"] for e in audit_log.query(since=week, keyword="乙·公司")] == ["archive.release"]


def test_describe_reads_like_a_sentence():
    assert audit_log.describe({"action": "account.edit", "detail": {
        "password_changed": True, "contact": ["A1", "B1"]}}) == "改了密码；联系人 A1 → B1"
    assert audit_log.describe({"action": "user.update", "detail": {
        "role": ["operator", "viewer"]}}) == "角色 经办人 → 只读"
    assert audit_log.describe({"action": "user.create", "detail": {
        "role": "operator", "groups": ["g1", "g2"]}}) == "角色 经办人；分组 g1、g2"
    assert audit_log.describe({"action": "notify.email", "detail": {
        "changed": ["smtp_host", "smtp_password"]}}) == "改了 smtp_host、smtp_password"


def test_a_torn_line_costs_only_that_line(history):
    (f, *_) = reversed(audit.files())
    with f.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "broken\n')
    assert len(audit_log.query(since=NOW - timedelta(days=7))) == 5


def test_last_logins(history):
    logins = audit_log.last_logins()
    assert set(logins) == {"opa"}
    assert datetime.fromisoformat(logins["opa"]) > NOW - timedelta(days=4)


def test_audit_page_for_admins(history, web_client, login_as):
    login_as(web_client, *ADMIN)
    body = web_client.get("/audit").text
    assert "修改巡检计划" in body and "0 9 * * * → 0 8 * * *" in body
    assert "归档释放" in body and "登录失败" in body
    filtered = web_client.get("/audit?actor=opa&category=account").text
    assert "编辑企业账号" in filtered and "修改巡检计划" not in filtered
    assert "编辑企业账号" in web_client.get("/audit/partial?q=甲").text


def test_audit_page_pages_by_fifty(monkeypatch, _isolated_audit, web_client, login_as):
    audit_log._parsed.clear()
    for i in range(120):
        audit.record("bot", "report.generate", target=f"r{i:03d}")
    login_as(web_client, *ADMIN)
    body = web_client.get("/audit?category=report").text
    assert "共 120 条" in body and "第 1/3 页" in body
    assert "r119" in body and "r069" not in body
    assert "r000" in web_client.get("/audit?category=report&page=3").text
    audit_log._parsed.clear()


def test_operators_cannot_read_the_log(web_client, login_as):
    login_as(web_client, *OPERATOR_A)
    assert web_client.get("/audit").status_code == 403
    assert web_client.get("/audit/partial").status_code == 403


def test_users_page_shows_last_login(web_client, login_as, isolated_settings, _isolated_audit):
    audit_log._parsed.clear()
    login_as(web_client.__class__(web_client.app), *OPERATOR_A)
    login_as(web_client, *ADMIN)
    body = web_client.get("/users").text
    opa_row = body.split('id="user-opa"', 1)[1].split("</tr>", 1)[0]
    ga_row = body.split('id="user-ga"', 1)[1].split("</tr>", 1)[0]
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%m-%d")
    assert today in opa_row  # logged in just now
    assert today not in ga_row  # never logged in: shows —
    audit_log._parsed.clear()
