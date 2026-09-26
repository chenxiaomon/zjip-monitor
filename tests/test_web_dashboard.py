"""The dashboard: one refresh target, live years, visible reasons, a run bar, links."""

import os
import re
from datetime import datetime, timedelta, timezone

import pytest

from src import control, status_store
from tests.conftest import ADMIN, GROUP_A_USER
from web.services import events as ev

BJ = timezone(timedelta(hours=8))


def _ms(y, m, d):
    return int(datetime(y, m, d, 10, tzinfo=BJ).timestamp() * 1000)


@pytest.fixture
def board(fake_accounts, isolated_snapshots, monkeypatch, tmp_path):
    isolated_snapshots.write("甲 公司", [
        {"dataRegNo": "A1", "dataRegName": "甲一", "dataRegAuditStatus": 7, "dataRegApplyTime": _ms(2024, 3, 1)},
        {"dataRegNo": "A2", "dataRegName": "甲二", "dataRegAuditStatus": 2, "dataRegApplyTime": _ms(2026, 8, 1)},
    ])
    isolated_snapshots.write("丙公司", [
        {"dataRegNo": "B1", "dataRegName": "丙一", "dataRegAuditStatus": 7, "dataRegApplyTime": _ms(2026, 1, 1)},
    ])
    monkeypatch.setattr(status_store, "get_all", lambda: {
        "ua1": {"status": "scrape_failed", "error": "平台返回 502 " * 20, "company": "甲 公司",
                "last_success_at": "2026-09-20T01:00:00+00:00"},
        "ua2": {"status": "ok", "company": "乙·公司", "last_success_at": "2026-09-26T10:00:00+00:00"},
        "ub1": {"status": "ok", "company": "丙公司", "last_success_at": "2026-09-25T10:00:00+00:00"},
    })
    monkeypatch.setattr(ev, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ev, "_EVENTS_FILE", tmp_path / "events.jsonl")
    ev._cache.clear()


def test_one_body_one_refresh(board, web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/").text
    assert 'id="dashboard-body"' in html and 'hx-get="/partials/dashboard"' in html
    assert html.count('hx-trigger="sse:update"') == 1  # the body only — no section refreshes itself
    assert 'sse-connect="/sse"' in html
    for section in ("ops-banner", "metrics-section", "todo-section", "account-grid-section", "timeline-section", "run-bar"):
        assert f'id="{section}"' in html


def test_standalone_partials_still_refresh_themselves(board, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    assert 'hx-get="/partials/account-grid"' in client.get("/partials/account-grid").text
    assert 'hx-get="/partials/ops-banner"' in client.get("/partials/ops-banner").text
    body = client.get("/partials/dashboard?year=2026").text
    assert 'hx-get="/partials/dashboard?year=2026"' in body
    assert body.count('hx-trigger="sse:update"') == 1


def test_years_come_from_the_data_and_the_filter_applies(board, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    html = client.get("/").text
    assert re.findall(r'href="/\?year=(\d{4})"', html) == ["2026", "2024"]
    assert "2025" not in re.findall(r'href="/\?year=(\d{4})"', html)
    assert "上次成功巡检：09-26 18:00" in html  # the newest success among visible accounts, Beijing time

    only_2024 = client.get("/partials/dashboard?year=2024").text
    assert re.search(r"登记总数</div>\s*<div[^>]*>1<", only_2024)


def test_failure_reason_is_visible_and_cards_link_to_records(board, web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/").text
    assert "上次巡检失败，数据为旧快照：平台返回 502" in html and "…" in html
    assert 'href="/records?account=ua1"' in html
    assert 'href="/records?account=ua1&status=2"' in html
    assert 'href="/records?status=2"' in html and 'href="/records?status=7"' in html
    assert "最后巡检：09-20 09:00" in html  # last_success_at, not the snapshot time


def test_group_user_sees_only_their_success_time(board, web_client, login_as):
    html = login_as(web_client, *GROUP_A_USER).get("/").text
    assert "上次成功巡检：09-26 18:00" in html
    assert "丙公司" not in html


def test_run_bar_shows_a_daemon_scan_and_hides_foreign_names(board, web_client, login_as, isolated_control_dir):
    now = datetime.now(timezone.utc).isoformat()
    control._write_state({
        "run_id": "r1", "kind": "run_all", "trigger": "daemon", "pid": os.getpid(), "started_at": now,
        "heartbeat_at": now, "status": "running", "progress": {"account": "丙公司", "index": 3, "total": 25},
        "error": None, "finished_at": None,
    })
    admin = login_as(web_client, *ADMIN).get("/partials/run-bar").text
    assert "巡检进行中（定时任务）：丙公司（3/25）" in admin
    assert 'hx-trigger="every 3s"' in admin

    group = login_as(web_client, *GROUP_A_USER).get("/partials/run-bar").text
    assert "丙公司" not in group and "巡检进行中（定时任务）：3/25" in group

    control._write_state({"run_id": "r1", "status": "done", "pid": os.getpid(), "progress": {}, "trigger": "daemon",
                          "started_at": now, "heartbeat_at": now, "finished_at": now, "error": None})
    idle = login_as(web_client, *ADMIN).get("/partials/run-bar").text
    assert "巡检进行中" not in idle and "hx-trigger" not in idle
