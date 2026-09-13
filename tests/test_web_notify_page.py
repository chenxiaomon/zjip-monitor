"""/notify must not claim a push happened when it did not.

The badge used to be derived from "is any channel enabled", so a rate-limited
webhook or a dead SMTP host still rendered every change as 「已发送」 — the one
place an operator would look to notice that notifications had stopped working.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import ADMIN
from web.services import events as ev


@pytest.fixture
def events_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ev, "_EVENTS_FILE", tmp_path / "events.jsonl")
    ev._cache.clear()

    def write(*events):
        (tmp_path / "events.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
            encoding="utf-8",
        )
        ev._cache.clear()

    yield write
    ev._cache.clear()


def _change_event(notified):
    return {
        "account": "ua1",
        "company": "甲 公司",
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "added": [{"dataRegNo": "R1", "dataRegName": "某数据"}],
        "changed": [],
        "removed": [],
        "notified": notified,
    }


def _legacy_change_event():
    """An event written before push outcomes were recorded at all."""
    e = _change_event(None)
    del e["notified"]
    return e


@pytest.mark.parametrize(
    "notified,expected",
    [
        ({"dingtalk": True}, "已发送"),
        ({"dingtalk": True, "email": False}, "推送失败"),
        ({"dingtalk": False}, "推送失败"),
        ({}, "未推送"),
    ],
)
def test_badge_reflects_the_recorded_outcome(
    web_client, login_as, isolated_settings, events_file, notified, expected
):
    events_file(_change_event(notified))
    login_as(web_client, *ADMIN)

    body = web_client.get("/notify").text
    assert expected in body


def test_legacy_events_without_an_outcome_are_not_claimed_as_sent(
    web_client, login_as, isolated_settings, events_file
):
    events_file(_legacy_change_event())
    login_as(web_client, *ADMIN)

    body = web_client.get("/notify").text
    assert "已发送" not in body


def test_page_states_that_no_channel_is_enabled(
    web_client, login_as, isolated_settings, events_file
):
    events_file(_change_event({}))
    login_as(web_client, *ADMIN)

    body = web_client.get("/notify").text
    assert "没有启用任何通知渠道" in body


def test_page_lists_the_enabled_channels(
    web_client, login_as, isolated_settings, events_file
):
    cfg = isolated_settings.read()
    cfg["notify"]["dingtalk"] = {"enabled": True, "webhook": "https://x", "secret": ""}
    isolated_settings.write(cfg)
    events_file(_change_event({"dingtalk": True}))
    login_as(web_client, *ADMIN)

    body = web_client.get("/notify").text
    assert "当前已启用：钉钉" in body
    assert "没有启用任何通知渠道" not in body


# ---------------------------------------------------------------------------
# Saving channel config: secrets are never echoed, and an empty submission
# means "leave unchanged" (the account password rule), not "erase".
# ---------------------------------------------------------------------------

TOKEN = "https://oapi.dingtalk.com/robot/send?access_token=TOK123"
SECRET = "SECsigning456"
SMTP_PW = "smtp-pw-789"


@pytest.fixture
def configured(isolated_settings):
    cfg = isolated_settings.read()
    cfg["notify"] = {
        "dingtalk": {"enabled": False, "webhook": TOKEN, "secret": SECRET},
        "wechat": {"enabled": False, "webhook": "https://qyapi/x?key=WKEY"},
        "email": {
            "enabled": False, "smtp_host": "smtp.x", "smtp_port": 465,
            "smtp_user": "u@x", "smtp_password": SMTP_PW,
            "from_addr": "u@x", "to_addrs": "a@x", "use_tls": True,
        },
    }
    isolated_settings.write(cfg)
    return isolated_settings


def test_get_never_renders_the_stored_secrets(
    web_client, login_as, configured, events_file
):
    """type="password" only hides them from eyes, not from view-source — and
    the page travels over plain HTTP."""
    events_file()
    login_as(web_client, *ADMIN)

    body = web_client.get("/notify").text
    for leak in (TOKEN, SECRET, SMTP_PW, "WKEY"):
        assert leak not in body
    assert "已保存，留空则不修改" in body


def test_saving_with_empty_secret_keeps_the_stored_one(web_client, login_as, configured):
    """The regression: after one save the inputs come back empty, and a second
    click used to write those empty strings into settings.yaml — leaving the
    channel enabled with no webhook and every push failing silently."""
    login_as(web_client, *ADMIN)

    first = web_client.post("/notify/dingtalk", data={"enabled": "on", "webhook": "", "secret": ""})
    assert first.status_code == 200
    assert TOKEN not in first.text and SECRET not in first.text

    # Submit the form exactly as it comes back: empty secret fields.
    web_client.post("/notify/dingtalk", data={"enabled": "on", "webhook": "", "secret": ""})

    saved = configured.read()["notify"]["dingtalk"]
    assert saved["webhook"] == TOKEN
    assert saved["secret"] == SECRET
    assert saved["enabled"] is True


def test_a_new_secret_overwrites(web_client, login_as, configured):
    login_as(web_client, *ADMIN)
    web_client.post(
        "/notify/dingtalk",
        data={"enabled": "on", "webhook": "https://new.example/hook", "secret": ""},
    )
    saved = configured.read()["notify"]["dingtalk"]
    assert saved["webhook"] == "https://new.example/hook"
    assert saved["secret"] == SECRET  # untouched


def test_clearing_a_secret_needs_the_explicit_checkbox(web_client, login_as, configured):
    login_as(web_client, *ADMIN)
    web_client.post(
        "/notify/dingtalk",
        data={"enabled": "off", "webhook": "", "secret": "", "clear_secret": "on"},
    )
    saved = configured.read()["notify"]["dingtalk"]
    assert saved["secret"] == ""
    assert saved["webhook"] == TOKEN


def test_enabling_without_a_webhook_is_refused(web_client, login_as, isolated_settings):
    """Enabled + empty webhook is the exact state that produced silent
    failures, so it cannot be saved at all."""
    login_as(web_client, *ADMIN)
    resp = web_client.post("/notify/wechat", data={"enabled": "on", "webhook": ""})

    assert "启用前必须填写 Webhook URL" in resp.text
    assert isolated_settings.read()["notify"]["wechat"].get("enabled") is False


def test_enabling_email_lists_every_missing_field(web_client, login_as, isolated_settings):
    login_as(web_client, *ADMIN)
    resp = web_client.post(
        "/notify/email",
        data={"enabled": "on", "smtp_host": "smtp.x", "smtp_port": "465"},
    )
    assert "启用前必须填写" in resp.text
    for label in ("用户名", "密码", "发件人", "收件人"):
        assert label in resp.text
    assert isolated_settings.read()["notify"]["email"].get("enabled") is False


def test_non_numeric_smtp_port_does_not_500(web_client, login_as, configured):
    login_as(web_client, *ADMIN)
    resp = web_client.post(
        "/notify/email",
        data={
            "enabled": "off", "smtp_host": "smtp.x", "smtp_port": "abc",
            "smtp_user": "u@x", "smtp_password": "", "from_addr": "u@x",
            "to_addrs": "a@x", "use_tls": "on",
        },
    )
    assert resp.status_code == 200
    assert configured.read()["notify"]["email"]["smtp_port"] == 465


def test_post_response_never_carries_a_secret(web_client, login_as, configured):
    login_as(web_client, *ADMIN)
    resp = web_client.post(
        "/notify/email",
        data={
            "enabled": "off", "smtp_host": "smtp.x", "smtp_port": "465",
            "smtp_user": "u@x", "smtp_password": "", "from_addr": "u@x",
            "to_addrs": "a@x", "use_tls": "on",
        },
    )
    assert SMTP_PW not in resp.text
    assert configured.read()["notify"]["email"]["smtp_password"] == SMTP_PW
