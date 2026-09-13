"""Data retention tests for main._prune_data().

Limits come from settings.yaml → retention.* (src/runtime_settings.py); the
tests pin the DEFAULTS, i.e. the literals the code used before they became
configurable.
"""

import time

import pytest

from src import main
from src.runtime_settings import RetentionSettings

_DEFAULTS = RetentionSettings()


@pytest.fixture(autouse=True)
def _defaults_only(isolated_settings):
    """The temp settings.yaml has no `retention` section, so _prune_data()
    runs on the defaults — a developer's real config cannot leak in."""


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
    assert len(a) == _DEFAULTS.snapshots_keep_per_company
    assert a[0].name == "公司A_2026-07-11-0900.json"  # oldest 10 removed
    assert len(b) == 5  # untouched


def test_prune_rotates_large_events_file(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(main, "_REPORTS_DIR", tmp_path / "reports")
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(main, "_EVENTS_FILE", events)
    events.write_text("x" * (_DEFAULTS.events_rotate_bytes + 1))

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
    stale = time.time() - (_DEFAULTS.reports_keep_days + 1) * 86400
    import os
    os.utime(old, (stale, stale))

    main._prune_data()

    assert not old.exists()
    assert new.exists()


def test_prune_bounds_rotated_event_logs(tmp_path, monkeypatch):
    """Rotated events-*.jsonl had no retention at all — 5 MB blocks piled up
    forever on a root partition that has been as full as 96%."""
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(main, "_REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(main, "_EVENTS_FILE", tmp_path / "events.jsonl")

    for day in range(1, 10):  # 9 rotations, oldest first
        (tmp_path / f"events-202601{day:02d}-000000.jsonl").write_text("x")

    main._prune_data()

    kept = sorted(p.name for p in tmp_path.glob("events-*.jsonl"))
    assert len(kept) == _DEFAULTS.events_rotated_keep
    assert kept[0] == "events-20260104-000000.jsonl"  # the 3 oldest are gone


def test_rotation_and_pruning_happen_in_one_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(main, "_REPORTS_DIR", tmp_path / "reports")
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(main, "_EVENTS_FILE", events)
    events.write_text("x" * (_DEFAULTS.events_rotate_bytes + 1))
    for day in range(1, 7):
        (tmp_path / f"events-202601{day:02d}-000000.jsonl").write_text("x")

    main._prune_data()

    # 6 old + 1 fresh rotation, trimmed back to the keep limit
    assert len(list(tmp_path.glob("events-*.jsonl"))) == _DEFAULTS.events_rotated_keep
    assert not events.exists()
