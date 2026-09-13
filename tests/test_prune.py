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


# ---------------------------------------------------------------------------
# Disk guard additions: strict snapshot grouping, .part sweep, disk_stats.json
# ---------------------------------------------------------------------------

@pytest.fixture
def prune_tree(tmp_path, monkeypatch):
    """main + diskstat pointed at one temp data tree."""
    from src import diskstat

    snaps = tmp_path / "snapshots"
    reports = tmp_path / "reports"
    attachments = tmp_path / "attachments"
    for d in (snaps, reports, attachments):
        d.mkdir()
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", snaps)
    monkeypatch.setattr(main, "_REPORTS_DIR", reports)
    monkeypatch.setattr(main, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(main, "_ATTACHMENTS_DIR", attachments)
    monkeypatch.setattr(diskstat, "_SNAPSHOTS_DIR", snaps)
    monkeypatch.setattr(diskstat, "_REPORTS_DIR", reports)
    monkeypatch.setattr(diskstat, "_ATTACHMENTS_DIR", attachments)
    monkeypatch.setattr(diskstat, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(diskstat, "_STATS_FILE", tmp_path / "control" / "disk_stats.json")
    return tmp_path


def _age(path, seconds: float) -> None:
    import os

    old = time.time() - seconds
    os.utime(path, (old, old))


def test_prune_skips_snapshot_files_without_a_timestamp_suffix(prune_tree):
    """The old blind `stem[:-16]` slice grouped `公司A_manual-export-x` (19
    chars) under `公司A`, so an odd file could push a real snapshot out of
    the keep window — or, with a different name, be deleted itself."""
    snaps = prune_tree / "snapshots"
    for day in range(1, 32):  # 31 real snapshots, one over the keep limit
        (snaps / f"公司A_2026-07-{day:02d}-0900.json").write_text("{}")
    irregular = [
        snaps / "公司A_manual-export-x.json",       # 19-char stem: old code keyed it as 公司A
        snaps / "公司A_2026-07-01-0900_copy.json",  # timestamp not at the end
        snaps / "notes.json",
        snaps / "公司A_2026-7-1-900.json",          # wrong digit widths
    ]
    for f in irregular:
        f.write_text("{}")

    main._prune_data()

    real = sorted(f for f in snaps.glob("公司A_2026-??-??-????.json"))
    assert len(real) == _DEFAULTS.snapshots_keep_per_company
    assert real[0].name == "公司A_2026-07-02-0900.json"  # exactly the oldest real one went
    for f in irregular:
        assert f.exists(), f.name


def test_prune_groups_by_full_prefix_even_with_underscores(prune_tree):
    snaps = prune_tree / "snapshots"
    for day in range(1, 33):
        (snaps / f"甲_乙_公司_2026-07-{day:02d}-0900.json").write_text("{}")
    for day in range(1, 4):
        (snaps / f"甲_2026-07-{day:02d}-0900.json").write_text("{}")

    main._prune_data()

    assert len(list(snaps.glob("甲_乙_公司_*.json"))) == _DEFAULTS.snapshots_keep_per_company
    assert len(list(snaps.glob("甲_2026-*.json"))) == 3


def test_prune_removes_only_stale_part_files(prune_tree):
    att = prune_tree / "attachments"
    stale_part = att / "公司A" / "R1" / "通知书.pdf.part"
    fresh_part = att / "公司A" / "R2" / "证书.pdf.part"
    old_pdf = att / "公司A" / "R1" / "通知书.pdf"
    for f in (stale_part, fresh_part, old_pdf):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    _age(stale_part, main._PART_STALE_SECONDS + 60)
    _age(old_pdf, main._PART_STALE_SECONDS + 60)
    _age(fresh_part, main._PART_STALE_SECONDS - 60)

    main._prune_data()

    assert not stale_part.exists()
    assert fresh_part.exists()  # may still be mid-download
    assert old_pdf.exists()     # never touch finished files, however old


def test_prune_writes_disk_stats(prune_tree):
    from src import diskstat

    snaps = prune_tree / "snapshots"
    for day in range(1, 36):
        (snaps / f"公司A_2026-07-{day:02d}-0900.json").write_text("{}")
    (snaps / "odd.json").write_text("{}")

    main._prune_data()

    stats_file = prune_tree / "control" / "disk_stats.json"
    assert stats_file.exists()
    stats = diskstat.read_stats()
    remaining = len(list(snaps.glob("*.json")))
    assert remaining == _DEFAULTS.snapshots_keep_per_company + 1
    assert stats["dirs"]["snapshots"]["files"] == remaining
    assert "root" in stats and "low_disk" in stats


def test_prune_survives_a_failing_stats_write(prune_tree, monkeypatch):
    from src import diskstat

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(diskstat, "atomic_write_text", boom)
    (prune_tree / "snapshots" / "公司A_2026-07-01-0900.json").write_text("{}")

    main._prune_data()  # must not raise
