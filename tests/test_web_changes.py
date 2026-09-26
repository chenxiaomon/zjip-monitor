"""/changes: a real date window, pages of 50, filters that survive a click."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import ADMIN, GROUP_B_USER
from web.services import events as ev

BJ = timezone(timedelta(hours=8))


def _event(account, at, reg_no):
    return {"account": account, "company": f"{account}-公司", "timestamp": at.isoformat(), "added": [], "removed": [],
            "changed": [{"reg_no": reg_no, "name": "n", "old_status_code": 1, "new_status_code": 2,
                         "old_status": "待审核", "new_status": "待补正"}]}


@pytest.fixture
def history(tmp_path, monkeypatch, fake_accounts):
    """ua1 (group_a): 60 changes over the last 60 hours plus 5 quiet scans;
    ub1 (group_b): one change 5 days ago; one change 40 days ago (outside the default window)."""
    monkeypatch.setattr(ev, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ev, "_EVENTS_FILE", tmp_path / "events.jsonl")
    ev._cache.clear()
    now = datetime.now(timezone.utc)
    events = [_event("ua1", now - timedelta(hours=h + 1), f"A{h:02d}") for h in range(60)]
    events += [{"account": "ua1", "company": "x", "timestamp": (now - timedelta(minutes=m)).isoformat(),
                "added": [], "changed": [], "removed": []} for m in range(5)]
    events.append(_event("ub1", now - timedelta(days=5), "B1"))
    events.append(_event("ua1", now - timedelta(days=40), "OLD"))
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in sorted(events, key=lambda e: e["timestamp"])) + "\n",
        encoding="utf-8")
    yield now
    ev._cache.clear()


def _rows(html):
    return re.findall(r">(A\d\d|B1|OLD)<", html)


def test_default_window_is_thirty_days_paged_by_fifty(history, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    html = client.get("/changes").text
    assert "共 61 条变更 · 第 1/2 页" in html
    assert len(_rows(html)) == 50 and "OLD" not in html
    assert "下一页" in html and "上一页" not in html

    page2 = client.get("/partials/changes?page=2").text
    assert len(_rows(page2)) == 11 and "B1" in page2
    assert 'hx-get="/partials/changes?page=2"' in page2  # SSE refresh keeps the page


def test_date_range_and_account_survive_chip_clicks(history, web_client, login_as):
    now = history
    start = (now - timedelta(days=6)).astimezone(BJ).strftime("%Y-%m-%d")
    end = (now - timedelta(days=4)).astimezone(BJ).strftime("%Y-%m-%d")
    html = login_as(web_client, *ADMIN).get(f"/changes?start_date={start}&end_date={end}").text
    assert "共 1 条变更" in html and _rows(html) == ["B1"]
    assert f'href="/changes?account=ua1&amp;start_date={start}&amp;end_date={end}"' in html
    assert f'hx-get="/partials/changes?start_date={start}&amp;end_date={end}"' in html
    assert "清除（最近 30 天）" in html

    html = login_as(web_client, *ADMIN).get(f"/changes?account=ub1&start_date={start}&end_date={end}").text
    assert 'value="ub1"' in html  # the date form keeps the account
    assert _rows(html) == ["B1"]


def test_group_user_only_sees_their_own(history, web_client, login_as):
    html = login_as(web_client, *GROUP_B_USER).get("/changes").text
    assert "共 1 条变更" in html and _rows(html) == ["B1"]


def test_bad_page_and_date_values_fall_back(history, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    assert "第 2/2 页" in client.get("/changes?page=99").text
    assert "第 1/2 页" in client.get("/changes?page=zero").text
    assert "共 61 条变更" in client.get("/changes?start_date=not-a-date").text
