"""src/diskstat.py — free-space probe, directory sizes and disk_stats.json.

The production root partition has run at 96% full; these are the numbers the
disk guard (skip attachment downloads) and the console banner act on, so the
arithmetic must match what an operator sees in `df -h`, and the collectors
must survive files disappearing under them (pruning and stats run back to
back on the same tree).
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from loguru import logger

from src import diskstat

MIB = 1024 * 1024


def _usage(*, total_mb: float, used_mb: float, free_mb: float):
    return shutil._ntuple_diskusage(int(total_mb * MIB), int(used_mb * MIB), int(free_mb * MIB))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A fake repository tree with every directory diskstat reports on."""
    root = tmp_path / "repo"
    data = root / "data"
    for d in ("attachments", "snapshots", "reports", "control"):
        (data / d).mkdir(parents=True)
    (root / "logs").mkdir()
    monkeypatch.setattr(diskstat, "_ROOT", root)
    monkeypatch.setattr(diskstat, "_DATA_DIR", data)
    monkeypatch.setattr(diskstat, "_ATTACHMENTS_DIR", data / "attachments")
    monkeypatch.setattr(diskstat, "_SNAPSHOTS_DIR", data / "snapshots")
    monkeypatch.setattr(diskstat, "_REPORTS_DIR", data / "reports")
    monkeypatch.setattr(diskstat, "_EVENTS_FILE", data / "events.jsonl")
    monkeypatch.setattr(diskstat, "_LOGS_DIR", root / "logs")
    monkeypatch.setattr(diskstat, "_STATS_FILE", data / "control" / "disk_stats.json")
    diskstat._read_cache = None
    yield root
    diskstat._read_cache = None


@pytest.fixture
def warnings():
    seen: list[str] = []
    sink = logger.add(
        lambda m: seen.append(m.record["message"]),
        level="WARNING",
        filter=lambda r: r["name"] == "src.diskstat",
    )
    yield seen
    logger.remove(sink)


# ---------------------------------------------------------------------------
# free_space / check_free
# ---------------------------------------------------------------------------

def test_free_space_reports_df_style_numbers(monkeypatch):
    # ext4 reserves ~5% for root: total ≠ used + free. `df` shows
    # used / (used + avail) — the 91% the operator reads on the server.
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(total_mb=1000, used_mb=850, free_mb=100))

    fs = diskstat.free_space("/")

    assert fs.total_bytes == 1000 * MIB
    assert fs.used_bytes == 850 * MIB
    assert fs.free_bytes == 100 * MIB
    assert fs.free_mb == 100
    assert fs.percent_used == pytest.approx(89.5, abs=0.05)


def test_free_space_falls_back_to_nearest_existing_parent(tmp_path, monkeypatch):
    asked: list[Path] = []

    def fake(p):
        asked.append(Path(p))
        return _usage(total_mb=10, used_mb=5, free_mb=5)

    monkeypatch.setattr(shutil, "disk_usage", fake)
    diskstat.free_space(tmp_path / "not" / "yet" / "created")

    assert asked == [tmp_path]


def test_check_free_flags_low_space(monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(total_mb=1000, used_mb=900, free_mb=100))

    check = diskstat.check_free(min_free_mb=500, path="/")

    assert check.low is True
    assert check.free_mb == 100
    assert check.min_free_mb == 500


def test_check_free_is_fine_above_the_floor(monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(total_mb=1000, used_mb=100, free_mb=900))
    assert diskstat.check_free(min_free_mb=500, path="/").low is False


def test_check_free_floor_comes_from_settings(isolated_settings, tree, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(total_mb=1000, used_mb=800, free_mb=200))

    assert diskstat.check_free().low is True  # default floor is 500 MB

    data = isolated_settings.read()
    data["disk"] = {"min_free_mb": 50}
    isolated_settings.write(data)
    check = diskstat.check_free()
    assert check.low is False
    assert check.min_free_mb == 50


def test_check_free_defaults_to_the_data_dir(tree, monkeypatch):
    asked: list[Path] = []

    def fake(p):
        asked.append(Path(p))
        return _usage(total_mb=10, used_mb=5, free_mb=5)

    monkeypatch.setattr(shutil, "disk_usage", fake)
    diskstat.check_free(min_free_mb=1)
    assert asked == [tree / "data"]


# ---------------------------------------------------------------------------
# dir_stats
# ---------------------------------------------------------------------------

def test_dir_stats_counts_recursively_without_following_symlinks(tmp_path):
    d = tmp_path / "att"
    (d / "a" / "deep").mkdir(parents=True)
    (d / "a" / "one.pdf").write_bytes(b"x" * 10)
    (d / "a" / "deep" / "two.pdf").write_bytes(b"y" * 20)
    (d / "top.json").write_bytes(b"z" * 5)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"h" * 1000)
    os.symlink(outside, d / "link_dir")            # a linked tree must not be descended
    os.symlink(outside / "huge.bin", d / "link.bin")  # nor a linked file counted

    stats = diskstat.dir_stats(d)

    assert stats == diskstat.DirStats(bytes=35, files=3)


def test_dir_stats_of_missing_dir_is_zero(tmp_path):
    assert diskstat.dir_stats(tmp_path / "nope") == diskstat.DirStats(bytes=0, files=0)


def test_dir_stats_ignores_files_that_vanish_mid_walk(tmp_path, monkeypatch):
    """Pruning runs right before the stats pass on the same tree."""
    d = tmp_path / "att"
    d.mkdir()
    (d / "keep.txt").write_bytes(b"abc")
    (d / "gone.txt").write_bytes(b"12345")

    real_scandir = os.scandir

    class _Listing:
        def __init__(self, entries):
            self._entries = entries

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, *exc):
            return False

    def scandir_then_delete(path):
        with real_scandir(path) as it:
            entries = list(it)
        # The directory listing already holds the entry; the file is deleted
        # before dir_stats gets to stat() it.
        for e in entries:
            if e.name == "gone.txt":
                Path(e.path).unlink()
        return _Listing(entries)

    monkeypatch.setattr(diskstat.os, "scandir", scandir_then_delete)

    assert diskstat.dir_stats(d) == diskstat.DirStats(bytes=3, files=1)


# ---------------------------------------------------------------------------
# collect / write_stats / read_stats
# ---------------------------------------------------------------------------

def _populate(root: Path) -> None:
    data = root / "data"
    (data / "attachments" / "公司A" / "R1").mkdir(parents=True)
    (data / "attachments" / "公司A" / "R1" / "通知书.pdf").write_bytes(b"p" * 300)
    (data / "attachments" / "公司A" / "R1" / ".sync.json").write_bytes(b"{}")
    (data / "snapshots" / "公司A_2026-09-01-0900.json").write_bytes(b"s" * 40)
    (data / "snapshots" / "公司A_2026-09-02-0900.json").write_bytes(b"s" * 60)
    (data / "reports" / "report_1.html").write_bytes(b"r" * 7)
    (data / "events.jsonl").write_bytes(b"e" * 11)
    (data / "events-20260101-000000.jsonl").write_bytes(b"e" * 9)
    (data / "control" / "state.json").write_bytes(b"{}")  # not a tracked dir
    (root / "logs" / "run_2026-09-13.log").write_bytes(b"l" * 13)


def test_collect_reports_every_tracked_directory(tree, isolated_settings, monkeypatch):
    _populate(tree)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(total_mb=1000, used_mb=200, free_mb=800))

    stats = diskstat.collect()

    assert stats["dirs"] == {
        "attachments": {"bytes": 302, "files": 2},
        "snapshots": {"bytes": 100, "files": 2},
        "reports": {"bytes": 7, "files": 1},
        "events": {"bytes": 20, "files": 2},
        "logs": {"bytes": 13, "files": 1},
    }
    assert stats["root"]["free_mb"] == 800
    assert stats["root"]["total_bytes"] == 1000 * MIB
    assert stats["low_disk"] is False
    assert stats["attachments_over_limit"] is False
    assert stats["min_free_mb"] == 500
    assert stats["attachments_max_total_bytes"] == 1024 * MIB
    collected = datetime.fromisoformat(stats["collected_at"])
    assert collected.utcoffset() is not None and collected.utcoffset().total_seconds() == 0


def test_collect_flags_low_disk(tree, isolated_settings, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(total_mb=1000, used_mb=950, free_mb=50))
    assert diskstat.collect()["low_disk"] is True


def test_collect_flags_attachments_over_limit(tree, isolated_settings, monkeypatch):
    _populate(tree)  # 302 bytes of attachments
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(total_mb=1000, used_mb=200, free_mb=800))
    assert diskstat.collect()["attachments_over_limit"] is False

    data = isolated_settings.read()
    data["attachments"] = {"max_total_mb": 0.0001}  # ≈ 104 bytes
    isolated_settings.write(data)
    stats = diskstat.collect()
    assert stats["attachments_over_limit"] is True
    assert stats["attachments_max_total_bytes"] == int(0.0001 * MIB)


def test_write_stats_round_trips_through_read_stats(tree, isolated_settings, monkeypatch):
    _populate(tree)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(total_mb=1000, used_mb=200, free_mb=800))

    written = diskstat.write_stats()

    stats_file = tree / "data" / "control" / "disk_stats.json"
    assert stats_file.exists()
    assert json.loads(stats_file.read_text(encoding="utf-8")) == written
    assert diskstat.read_stats() == written
    assert list(stats_file.parent.glob("*.tmp")) == []  # atomic write left nothing behind


def test_write_stats_warns_on_low_disk_and_oversized_attachments(
    tree, isolated_settings, monkeypatch, warnings
):
    _populate(tree)
    data = isolated_settings.read()
    data["attachments"] = {"max_total_mb": 0.0001}
    isolated_settings.write(data)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(total_mb=1000, used_mb=950, free_mb=50))

    diskstat.write_stats()

    assert len(warnings) == 2
    assert any("min_free_mb" in w for w in warnings)
    assert any("max_total_mb" in w for w in warnings)


def test_write_stats_never_raises(tree, monkeypatch, warnings):
    """ENOSPC while writing the report about ENOSPC is the expected case.

    The measurement still has to reach the caller: the ops banner is the one
    thing that can tell the operator why downloads stopped, and on a full
    disk the file it would normally read cannot be written."""

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(diskstat, "atomic_write_text", boom)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(total_mb=1000, used_mb=999, free_mb=1))

    stats = diskstat.write_stats()  # must not raise
    assert stats is not None and stats["low_disk"] is True
    assert not (tree / "data" / "control" / "disk_stats.json").exists()
    assert any("disk_stats.json" in w for w in warnings)


def test_write_stats_returns_none_when_measuring_itself_fails(tree, monkeypatch, warnings):
    monkeypatch.setattr(diskstat, "collect", lambda: (_ for _ in ()).throw(OSError("no statvfs")))
    assert diskstat.write_stats() is None
    assert any("disk_stats.json" in w for w in warnings)


def test_read_stats_is_cached_until_the_file_changes(tree):
    stats_file = tree / "data" / "control" / "disk_stats.json"
    stats_file.write_text(json.dumps({"v": 1}), encoding="utf-8")

    first = diskstat.read_stats()
    assert first == {"v": 1}
    assert diskstat.read_stats() is first  # same key → cached object

    stats_file.write_text(json.dumps({"v": 2, "changed": True}), encoding="utf-8")
    assert diskstat.read_stats() == {"v": 2, "changed": True}


def test_read_stats_returns_none_for_missing_or_broken_file(tree):
    stats_file = tree / "data" / "control" / "disk_stats.json"
    assert diskstat.read_stats() is None

    stats_file.write_text("{not json", encoding="utf-8")
    assert diskstat.read_stats() is None

    stats_file.write_text("[1, 2]", encoding="utf-8")  # valid JSON, wrong shape
    assert diskstat.read_stats() is None


def test_write_stats_invalidates_the_same_process_read_cache(tree, isolated_settings, monkeypatch):
    stats_file = tree / "data" / "control" / "disk_stats.json"
    stats_file.write_text(json.dumps({"stale": True}), encoding="utf-8")
    assert diskstat.read_stats() == {"stale": True}

    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(total_mb=1000, used_mb=200, free_mb=800))
    written = diskstat.write_stats()

    assert diskstat.read_stats() == written
