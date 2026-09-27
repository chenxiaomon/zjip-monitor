"""scripts/backfill_daily_stats.py: seed the trend file from the snapshots on disk."""

import json
from datetime import date

import pytest

from scripts import backfill_daily_stats as script
from src import control, daily_stats


@pytest.fixture
def snaps(tmp_path, monkeypatch, isolated_control_dir, isolated_settings):
    monkeypatch.setattr(script, "_SNAPSHOTS_DIR", tmp_path)
    monkeypatch.setattr(script, "setup_logging", lambda name: None)
    isolated_settings.write({"retention": {"snapshots_keep_per_company": 3}})

    def write(prefix, stamp, company, codes):
        records = [{"dataRegAuditStatus": c, "dataRegApplyTime": 1767225600000} for c in codes]  # 2026-01-01 BJ
        (tmp_path / f"{prefix}_{stamp}.json").write_text(
            json.dumps({"company": company, "records": records}, ensure_ascii=False), encoding="utf-8")

    return write


def _totals(day):
    return {c: sum(n for b in buckets.values() for n in b.values())
            for c, buckets in daily_stats.read_days()[day]["companies"].items()}


def test_days_carry_forward_and_the_window_starts_where_coverage_is_complete(snaps):
    # 甲 has a full set (3 = keep): older files may have been pruned → its first day limits the window.
    snaps("甲", "2026-09-20-0100", "甲", [7])
    snaps("甲", "2026-09-21-1000", "甲", [7, 7])
    snaps("甲", "2026-09-23-0100", "甲", [7, 7, 2])   # no 09-22 snapshot → carries 09-21's numbers
    # 乙 has fewer files than keep: its whole history is here and it does not limit the window.
    snaps("乙", "2026-09-18-0100", "乙", [1])
    snaps("乙", "2026-09-22-2000", "乙", [1, 1])      # 04:00 Beijing on 09-23

    counts = script.backfill(today=date(2026, 9, 27))
    assert counts == {"companies": 2, "files_read": 5, "unreadable": 0, "window_start": "2026-09-20",
                      "days_found": 4, "days_added": 4, "dry_run": False}
    assert sorted(daily_stats.read_days()) == ["2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23"]
    assert _totals("2026-09-20") == {"甲": 1, "乙": 1}
    assert _totals("2026-09-22") == {"甲": 2, "乙": 1}
    assert _totals("2026-09-23") == {"甲": 3, "乙": 2}
    assert daily_stats.read_days()["2026-09-23"]["source"] == "backfill"


def test_recorded_days_are_kept_and_dry_run_writes_nothing(snaps):
    snaps("甲", "2026-09-20-0100", "甲", [7])
    snaps("甲", "2026-09-21-0100", "甲", [7, 7])
    daily_stats._write({"2026-09-21": {"at": "live", "companies": {"甲": {"2026": {"7": 99}}}}})

    assert script.backfill(dry_run=True, today=date(2026, 9, 27))["days_added"] == 1
    assert list(daily_stats.read_days()) == ["2026-09-21"]

    assert script.backfill(today=date(2026, 9, 27))["days_added"] == 1
    assert daily_stats.read_days()["2026-09-21"]["at"] == "live"
    assert "2026-09-20" in daily_stats.read_days()


def test_unreadable_files_are_counted_not_fatal(snaps, tmp_path):
    snaps("甲", "2026-09-20-0100", "甲", [7])
    (tmp_path / "甲_2026-09-21-0100.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")  # not a snapshot name → ignored
    counts = script.backfill(today=date(2026, 9, 27))
    assert counts["unreadable"] == 1 and counts["days_added"] == 1


def test_no_snapshots_is_a_no_op(snaps):
    assert script.backfill()["days_added"] == 0
    assert not daily_stats._STATS_FILE.exists()


def test_main_needs_the_lock_heartbeats_and_prints_json(snaps, monkeypatch, capsys):
    snaps("甲", "2026-09-20-0100", "甲", [7])
    holder = control.try_acquire(kind="run_all", trigger="daemon")
    try:
        assert script.main([]) == 2
    finally:
        holder.finish("done")
    assert "error" in json.loads(capsys.readouterr().out)

    beats = []
    real = control.CancelToken.heartbeat
    monkeypatch.setattr(control.CancelToken, "heartbeat", lambda self, **p: (beats.append(1), real(self, **p)))
    assert script.main(["--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["days_found"] >= 1
    assert beats, "long backfills must keep the run's heartbeat alive"
    assert control.read_state()["status"] == "done"
