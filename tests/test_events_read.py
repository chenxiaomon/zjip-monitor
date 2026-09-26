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


def _change(reg_no="R1"):
    return [{"reg_no": reg_no, "name": "n", "old_status_code": 1, "new_status_code": 2,
             "old_status": "待审核", "new_status": "待补正"}]


def test_with_changes_skips_quiet_scans_before_the_cap(events_dir):
    """Every successful scan writes an event; only about one in ten carries a
    change. A cap counted in raw events made 'the last 30 days' about 5 days."""
    busy = [_event("a", hours_ago=3, changed=_change())]
    quiet = [_event("a", hours_ago=1) for _ in range(20)]
    _write(events_dir / "events.jsonl", busy + quiet)

    assert all(not e["changed"] for e in ev.read_events(hours=24, limit=5))
    got = ev.read_events(hours=24, limit=5, with_changes=True)
    assert len(got) == 1 and got[0]["changed"]


def test_explicit_window_since_until(events_dir):
    _write(events_dir / "events.jsonl", [
        _event("old", hours_ago=60), _event("mid", hours_ago=30), _event("new", hours_ago=1),
    ])
    now = datetime.now(timezone.utc)
    assert [e["account"] for e in ev.read_events(since=now - timedelta(hours=48))] == ["new", "mid"]
    assert [e["account"] for e in ev.read_events(since=now - timedelta(hours=48), until=now - timedelta(hours=10))] == ["mid"]
    assert [e["account"] for e in ev.read_events(hours=1000, limit=None)] == ["new", "mid", "old"]


def test_change_rows_cover_the_whole_window_and_cap_rows_not_events(events_dir):
    quiet = [_event("a", hours_ago=1) for _ in range(400)]
    changes = [_event("a", hours_ago=5, changed=_change("R1") + _change("R2")),
               _event("a", hours_ago=200, changed=_change("R3"))]
    _write(events_dir / "events.jsonl", changes[::-1] + quiet)

    rows = ev.read_change_rows(hours=24 * 30)
    assert [r["reg_no"] for r in rows] == ["R1", "R2", "R3"]
    assert [r["reg_no"] for r in ev.read_change_rows(hours=24 * 30, limit=1)] == ["R1"]
    assert [r["reg_no"] for r in ev.read_change_rows(hours=24 * 30, limit=2)] == ["R1", "R2"]


def test_cache_is_invalidated_by_appends(events_dir):
    path = events_dir / "events.jsonl"
    _write(path, [_event("a")])
    assert len(ev.read_events(hours=24)) == 1

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_event("b"), ensure_ascii=False) + "\n")
    assert len(ev.read_events(hours=24)) == 2
