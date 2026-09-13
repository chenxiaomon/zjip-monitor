"""web/services/events.py — the read side of data/events.jsonl.

The writer rotates the log once it grows past a threshold. The reader used to
open only the live file, so every rotation silently emptied the 变更 page and
the 通知 page of their 30-day history while the data sat on disk.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from web.services import events as ev


@pytest.fixture
def events_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ev, "_EVENTS_FILE", tmp_path / "events.jsonl")
    ev._cache.clear()
    yield tmp_path
    ev._cache.clear()


def _event(account, *, hours_ago=1, added=None, changed=None, removed=None):
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "account": account,
        "company": f"{account}-公司",
        "timestamp": ts.isoformat(),
        "added": added or [],
        "changed": changed or [],
        "removed": removed or [],
    }


def _write(path, events):
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )


def test_rotated_files_are_still_readable(events_dir):
    _write(events_dir / "events-20260101-000000.jsonl", [_event("old", hours_ago=48)])
    _write(events_dir / "events.jsonl", [_event("new", hours_ago=1)])

    accounts = [e["account"] for e in ev.read_events(hours=24 * 30)]
    assert accounts == ["new", "old"]  # newest first, across files


def test_rotated_files_are_ordered_newest_first(events_dir):
    _write(events_dir / "events-20260101-000000.jsonl", [_event("r1", hours_ago=72)])
    _write(events_dir / "events-20260202-000000.jsonl", [_event("r2", hours_ago=48)])
    _write(events_dir / "events.jsonl", [_event("live", hours_ago=1)])

    assert [e["account"] for e in ev.read_events(hours=24 * 30)] == ["live", "r2", "r1"]


def test_only_rotated_files_present_still_read(events_dir):
    """Right after a rotation there is no live file at all."""
    _write(events_dir / "events-20260101-000000.jsonl", [_event("r1", hours_ago=2)])
    assert [e["account"] for e in ev.read_events(hours=24)] == ["r1"]


def test_time_window_stops_the_walk(events_dir):
    _write(events_dir / "events-20260101-000000.jsonl", [_event("too-old", hours_ago=100)])
    _write(events_dir / "events.jsonl", [_event("recent", hours_ago=1)])

    assert [e["account"] for e in ev.read_events(hours=24)] == ["recent"]


def test_allow_list_is_applied_before_the_limit(events_dir):
    """A busy account the user may not see must not crowd that user's own
    changes out of the newest-N window."""
    noisy = [_event("other", hours_ago=1) for _ in range(50)]
    mine = [_event("mine", hours_ago=2)]
    _write(events_dir / "events.jsonl", mine + noisy)  # mine is the oldest line

    assert ev.read_events(hours=24, limit=10, accounts={"mine"}) != []
    assert [e["account"] for e in ev.read_events(hours=24, limit=10, accounts={"mine"})] == ["mine"]

    # Without the push-down the 10 newest events are all "other"
    unfiltered = ev.read_events(hours=24, limit=10)
    assert all(e["account"] == "other" for e in unfiltered)


def test_account_filter_and_corrupt_lines(events_dir):
    path = events_dir / "events.jsonl"
    _write(path, [_event("a"), _event("b")])
    with path.open("a", encoding="utf-8") as f:
        f.write("{not json\n\n")

    assert [e["account"] for e in ev.read_events(hours=24, account="a")] == ["a"]
    assert len(ev.read_events(hours=24)) == 2  # corrupt + blank lines skipped


def test_change_rows_flatten_across_files(events_dir):
    _write(
        events_dir / "events-20260101-000000.jsonl",
        [_event("a", hours_ago=48, added=[{"dataRegNo": "R9", "dataRegAuditStatus": 1}])],
    )
    _write(
        events_dir / "events.jsonl",
        [
            _event(
                "a",
                hours_ago=1,
                changed=[{
                    "reg_no": "R1", "name": "n",
                    "old_status_code": 1, "new_status_code": 7,
                    "old_status": "待审核", "new_status": "登记成功",
                }],
                removed=[{"dataRegNo": "R2", "dataRegAuditStatus": 3}],
            )
        ],
    )

    rows = ev.read_change_rows(hours=24 * 30)
    kinds = [(r["type"], r["reg_no"]) for r in rows]
    assert kinds == [("changed", "R1"), ("removed", "R2"), ("added", "R9")]
    assert rows[2]["new_status"] == "待审核"  # decoded from the rotated file


def test_cache_is_invalidated_by_appends(events_dir):
    path = events_dir / "events.jsonl"
    _write(path, [_event("a")])
    assert len(ev.read_events(hours=24)) == 1

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_event("b"), ensure_ascii=False) + "\n")
    assert len(ev.read_events(hours=24)) == 2
