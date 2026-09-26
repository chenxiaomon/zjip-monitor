"""web/sse.py: the watcher broadcasts new event lines and run transitions only."""

import asyncio
import json

import pytest

from web import sse


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    monkeypatch.setattr(sse, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(sse, "_STATE_FILE", tmp_path / "control" / "state.json")
    (tmp_path / "control").mkdir()
    q: asyncio.Queue = asyncio.Queue()
    sse._subscribers.add(q)
    yield tmp_path, q
    sse._subscribers.discard(q)


def _drain(q):
    out = []
    while not q.empty():
        out.append(json.loads(q.get_nowait()))
    return out


def _state(root, **fields):
    base = {"run_id": "r1", "status": "running", "finished_at": None, "progress": {}}
    (root / "control" / "state.json").write_text(json.dumps({**base, **fields}), encoding="utf-8")


def test_run_transitions_broadcast_but_heartbeats_do_not(watcher):
    root, q = watcher
    cursor = {"events_size": 0, "run_key": None}
    _state(root, progress={"account": "甲", "index": 1, "total": 3})
    asyncio.run(sse._tick(cursor))
    assert _drain(q) == [{"kind": "run", "status": "running"}]

    _state(root, progress={"account": "乙", "index": 2, "total": 3})  # heartbeat: same run, same status
    asyncio.run(sse._tick(cursor))
    assert _drain(q) == []

    _state(root, status="done", finished_at="2026-09-27T01:00:00+00:00")
    asyncio.run(sse._tick(cursor))
    assert _drain(q) == [{"kind": "run", "status": "done"}]


def test_event_lines_are_broadcast_from_the_cursor(watcher):
    root, q = watcher
    cursor = {"events_size": 0, "run_key": None}
    (root / "events.jsonl").write_text('{"account": "a"}\n', encoding="utf-8")
    asyncio.run(sse._tick(cursor))
    assert _drain(q) == [{"account": "a"}]

    with (root / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"account": "b"}\n')
    asyncio.run(sse._tick(cursor))
    assert _drain(q) == [{"account": "b"}]

    (root / "events.jsonl").write_text('{"account": "c"}\n', encoding="utf-8")  # rotated: smaller file
    asyncio.run(sse._tick(cursor))
    assert _drain(q) == [{"account": "c"}]


def test_corrupt_state_file_is_ignored(watcher):
    root, q = watcher
    cursor = {"events_size": 0, "run_key": ("r0", "done", "x")}
    (root / "control" / "state.json").write_text("{not json", encoding="utf-8")
    asyncio.run(sse._tick(cursor))
    assert _drain(q) == [] and cursor["run_key"] is None


def test_run_messages_reach_group_users():
    assert sse._visible_to({"ua1"}, json.dumps({"kind": "run", "status": "running"}))
    assert not sse._visible_to({"ua1"}, json.dumps({"account": "ub1"}))
