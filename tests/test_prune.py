"""Data retention tests for main._prune_data()."""

import time

from src import main


def test_prune_keeps_recent_snapshots_per_company(tmp_path, monkeypatch):
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", snaps)
    monkeypatch.setattr(main, "_REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(main, "_EVENTS_FILE", tmp_path / "events.jsonl")

    for day in range(1, 41):  # 40 snapshots for company A, 5 for B
        (snaps / f"公司A_2026-07-{day:02d}-0900.json").write_text("{}")
    for day in range(1, 6):
        (snaps / f"公司B_2026-07-{day:02d}-0900.json").write_text("{}")

    main._prune_data()

    a = sorted(snaps.glob("公司A_*.json"))
    b = sorted(snaps.glob("公司B_*.json"))
    assert len(a) == main._SNAPSHOTS_KEEP_PER_COMPANY
    assert a[0].name == "公司A_2026-07-11-0900.json"  # oldest 10 removed
    assert len(b) == 5  # untouched


def test_prune_rotates_large_events_file(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(main, "_REPORTS_DIR", tmp_path / "reports")
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(main, "_EVENTS_FILE", events)
    events.write_text("x" * (main._EVENTS_ROTATE_BYTES + 1))

    main._prune_data()

    assert not events.exists()
    rotated = list(tmp_path.glob("events-*.jsonl"))
    assert len(rotated) == 1


def test_prune_removes_old_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", tmp_path / "snapshots")
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(main, "_REPORTS_DIR", reports)
    monkeypatch.setattr(main, "_EVENTS_FILE", tmp_path / "events.jsonl")

    old = reports / "report_old.html"
    new = reports / "report_new.html"
    old.write_text("x")
    new.write_text("x")
    stale = time.time() - (main._REPORTS_KEEP_DAYS + 1) * 86400
    import os
    os.utime(old, (stale, stale))

    main._prune_data()

    assert not old.exists()
    assert new.exists()
