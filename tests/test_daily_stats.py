"""src/daily_stats.py: per-day counts for the trend lines, and the finalizer hook."""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import control, daily_stats, main

BJ = timezone(timedelta(hours=8))


def _ms(y, m, d):
    return int(datetime(y, m, d, 10, tzinfo=BJ).timestamp() * 1000)


def _acc(company):
    return SimpleNamespace(company=company, username=company)


RECORDS = [
    {"dataRegAuditStatus": 7, "dataRegApplyTime": _ms(2026, 1, 5)},
    {"dataRegAuditStatus": "7", "dataRegApplyTime": _ms(2025, 12, 31)},
    {"dataRegAuditStatus": 2, "dataRegApplyTime": None},
    {"dataRegAuditStatus": None, "dataRegApplyTime": _ms(2026, 1, 5)},      # no status → not counted
    {"dataRegAuditStatus": 1, "dataRegApplyTime": 10 ** 15},                # out of range → "none"
    {"dataRegAuditStatus": 1, "dataRegApplyTime": int(datetime(2025, 12, 31, 20, tzinfo=timezone.utc).timestamp() * 1000)},  # 2026 in Beijing
    "junk",
]


def test_count_records_follows_the_dashboard_rules():
    assert daily_stats.count_records(RECORDS) == {
        "2026": {"7": 1, "1": 1},
        "2025": {"7": 1},
        "none": {"2": 1, "1": 1},
    }
    assert daily_stats.count_records(None) == {} and daily_stats.count_records("x") == {}


def test_record_round_writes_today_and_a_later_round_replaces_it():
    snaps = {"甲": {"records": RECORDS[:2]}, "乙": {"records": RECORDS[2:3]}}
    now = datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc)
    assert daily_stats.record_round([_acc("甲"), _acc("乙"), _acc("丙")], snaps.get, now=now) is True
    days = daily_stats.read_days()
    assert list(days) == ["2026-09-27"]
    assert days["2026-09-27"]["companies"] == {"甲": {"2026": {"7": 1}, "2025": {"7": 1}}, "乙": {"none": {"2": 1}}}

    snaps["甲"] = {"records": RECORDS[:1]}
    assert daily_stats.record_round([_acc("甲"), _acc("乙")], snaps.get, now=now + timedelta(hours=9))
    assert daily_stats.read_days()["2026-09-27"]["companies"]["甲"] == {"2026": {"7": 1}}
    assert len(daily_stats.read_days()) == 1


def test_an_unreadable_snapshot_keeps_the_last_known_counts():
    day1 = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)
    daily_stats.record_round([_acc("甲")], {"甲": {"records": RECORDS[:2]}}.get, now=day1)
    daily_stats.record_round([_acc("甲")], lambda c: None, now=day1 + timedelta(days=1))
    days = daily_stats.read_days()
    assert days["2026-09-27"]["companies"]["甲"] == days["2026-09-26"]["companies"]["甲"]


def test_nothing_to_count_leaves_the_file_alone():
    assert daily_stats.record_round([_acc("甲")], lambda c: None) is False
    assert not daily_stats._STATS_FILE.exists()


def test_old_days_are_trimmed():
    now = datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc)
    old = (now - timedelta(days=daily_stats.KEEP_DAYS + 5)).date().isoformat()
    kept = (now - timedelta(days=daily_stats.KEEP_DAYS - 1)).date().isoformat()
    daily_stats._write({old: {"at": "x", "companies": {}}, kept: {"at": "x", "companies": {}}})
    daily_stats.record_round([_acc("甲")], {"甲": {"records": []}}.get, now=now)
    assert sorted(daily_stats.read_days()) == [kept, "2026-09-27"]


def test_a_write_failure_never_raises(monkeypatch):
    def full_disk(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(daily_stats, "atomic_write_text", full_disk)
    assert daily_stats.record_round([_acc("甲")], {"甲": {"records": RECORDS}}.get) is False


def test_a_corrupt_file_is_moved_aside_not_lost():
    daily_stats._STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    daily_stats._STATS_FILE.write_text("{not json", encoding="utf-8")
    assert daily_stats.record_round([_acc("甲")], {"甲": {"records": RECORDS}}.get)
    asides = list(daily_stats._STATS_FILE.parent.glob("daily_stats.json.bad-*"))
    assert len(asides) == 1 and asides[0].read_text(encoding="utf-8") == "{not json"


def test_read_is_cached_until_the_next_write():
    daily_stats.record_round([_acc("甲")], {"甲": {"records": RECORDS}}.get)
    first = daily_stats.read_days()
    assert daily_stats.read_days() is first
    daily_stats.record_round([_acc("乙")], {"乙": {"records": RECORDS}}.get)
    assert daily_stats.read_days() is not first
    assert list(daily_stats.read_days().values())[-1]["companies"].keys() == {"乙"}
    assert not list(daily_stats._STATS_FILE.parent.glob("*.tmp"))


def test_merge_days_only_fills_gaps():
    daily_stats._write({"2026-09-20": {"at": "live", "companies": {"甲": {"2026": {"7": 9}}}}})
    added = daily_stats.merge_days({
        "2026-09-20": {"at": "backfill", "companies": {"甲": {"2026": {"7": 1}}}},
        "2026-09-19": {"at": "backfill", "companies": {"甲": {"2026": {"7": 1}}}},
        "bogus": {"at": "x", "companies": {}},
    }, today=date(2026, 9, 27))
    assert added == 1
    days = daily_stats.read_days()
    assert days["2026-09-20"]["at"] == "live" and "2026-09-19" in days and "bogus" not in days
    assert daily_stats.merge_days({"2026-09-18": {"at": "b", "companies": {}}}, dry_run=True) == 1
    assert "2026-09-18" not in daily_stats.read_days()


def test_series_sums_the_wanted_companies_and_honours_the_year():
    daily_stats._write({
        "2026-09-25": {"at": "x", "companies": {
            "甲": {"2026": {"7": 2, "2": 1}, "2025": {"7": 1}, "none": {"1": 1}},
            "乙": {"2026": {"7": 5}},
        }},
        "2026-09-27": {"at": "x", "companies": {"甲": {"2026": {"7": 3, "2": 1}}}},
        "2026-06-01": {"at": "x", "companies": {"甲": {"2026": {"7": 1}}}},  # outside 30 days
    })
    today = date(2026, 9, 27)
    assert daily_stats.series({"甲"}, None, 30, today=today) == [
        {"date": "2026-09-25", "total": 5, "success": 3, "pending": 1},
        {"date": "2026-09-27", "total": 4, "success": 3, "pending": 1},
    ]
    assert daily_stats.series({"甲", "乙"}, 2026, 30, today=today)[0] == {
        "date": "2026-09-25", "total": 8, "success": 7, "pending": 1}
    assert daily_stats.series(set(), None, 30, today=today)[0]["total"] == 0
    assert len(daily_stats.series({"甲"}, None, 120, today=today)) == 3
    assert daily_stats.first_date() == "2026-06-01"


# ---------------------------------------------------------------------------
# The finalizer hook: every way out of a round writes today's counts, under the lock
# ---------------------------------------------------------------------------

def _ok(account):
    return {"account": account.username, "company": account.company,
            "records": [], "snapshot_path": None, "error": None}


@pytest.fixture
def stats_calls(isolated_control_dir, isolated_settings, monkeypatch):
    calls: list[bool] = []

    def probe():
        probe_ctx = control.try_acquire(kind="probe", trigger="test")
        if probe_ctx is not None:
            probe_ctx.finish("done")
        calls.append(probe_ctx is None)

    accounts = [SimpleNamespace(company=f"公司{i}", username=f"user{i}", password="x", contact="", group="")
                for i in range(2)]
    monkeypatch.setattr(main, "load_accounts", lambda: accounts)
    monkeypatch.setattr(main, "_prune_data", lambda: None)
    monkeypatch.setattr(main, "_record_daily_stats", probe)
    monkeypatch.setattr(main, "generate_report", lambda *a, **k: None)
    monkeypatch.setattr(main, "notify_batch", lambda *a, **k: None)
    monkeypatch.setattr(main.random, "uniform", lambda a, b: 0.0)
    return SimpleNamespace(calls=calls, accounts=accounts)


def test_every_exit_records_the_day_while_holding_the_lock(stats_calls, monkeypatch):
    from src.config_loader import ConfigError
    from src.exceptions import RunCancelled

    monkeypatch.setattr(main, "scan_account_and_notify", lambda account, cancel=None, **kw: _ok(account))
    main.run_all(trigger="test")
    main.run_single(stats_calls.accounts[0], trigger="test")

    def cancelled(account, cancel=None, **kw):
        raise RunCancelled("stop")

    monkeypatch.setattr(main, "scan_account_and_notify", cancelled)
    main.run_all(trigger="test")
    main.run_single(stats_calls.accounts[0], trigger="test")

    def boom(account, cancel=None, **kw):
        raise ValueError("boom")

    monkeypatch.setattr(main, "scan_account_and_notify", boom)
    with pytest.raises(ValueError):
        main.run_single(stats_calls.accounts[0], trigger="test")

    def bad_accounts():
        raise ConfigError("unreadable")

    monkeypatch.setattr(main, "load_accounts", bad_accounts)
    main.run_all(trigger="test")

    assert stats_calls.calls == [True] * 6


def test_a_stats_failure_never_blocks_the_lock_release(isolated_control_dir, isolated_settings, monkeypatch):
    def broken():
        raise RuntimeError("stats exploded")

    monkeypatch.setattr(main, "load_accounts", lambda: [SimpleNamespace(company="甲", username="u", password="x", contact="", group="")])
    monkeypatch.setattr(main, "_prune_data", lambda: None)
    monkeypatch.setattr(main, "_record_daily_stats", broken)
    monkeypatch.setattr(main, "generate_report", lambda *a, **k: None)
    monkeypatch.setattr(main, "notify_batch", lambda *a, **k: None)
    monkeypatch.setattr(main, "scan_account_and_notify", lambda account, cancel=None, **kw: _ok(account))
    main.run_all(trigger="test")
    assert control.read_state()["status"] == "done"
    ctx = control.try_acquire(kind="probe", trigger="test")
    assert ctx is not None
    ctx.finish("done")


def test_the_hook_counts_every_accounts_latest_snapshot(isolated_snapshots, monkeypatch):
    isolated_snapshots.write("甲公司", RECORDS[:2])
    monkeypatch.setattr(main, "load_accounts", lambda: [_acc("甲公司"), _acc("没快照公司")])
    main._record_daily_stats()
    (entry,) = daily_stats.read_days().values()
    assert entry["companies"] == {"甲公司": {"2026": {"7": 1}, "2025": {"7": 1}}}


def test_the_hook_skips_quietly_when_accounts_cannot_be_read(monkeypatch):
    from src.config_loader import ConfigError

    def bad():
        raise ConfigError("bad key")

    monkeypatch.setattr(main, "load_accounts", bad)
    main._record_daily_stats()
    assert not daily_stats._STATS_FILE.exists()
