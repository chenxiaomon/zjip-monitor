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
