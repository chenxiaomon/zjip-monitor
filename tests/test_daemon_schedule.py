"""run_daemon: the schedule is hot-reloaded, and only a real, valid change reschedules."""

import pytest
import yaml
from apscheduler.schedulers.background import BackgroundScheduler

import run_daemon
from src import control


@pytest.fixture
def daemon(tmp_path, monkeypatch, isolated_control_dir):
    settings = tmp_path / "settings.yaml"
    monkeypatch.setattr(run_daemon, "_SETTINGS", settings)
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")  # never started: jobs stay pending

    def write(crons, **extra):
        data = {"schedule": {"crons": crons}, **extra}
        settings.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")

    return scheduler, settings, write


def _crons(scheduler):
    return sorted((j.id, str(j.trigger)) for j in scheduler.get_jobs() if j.id.startswith("cron:"))


def test_apply_replaces_only_the_scan_jobs_and_publishes_them(daemon):
    scheduler, _, _ = daemon
    scheduler.add_job(lambda: None, "interval", minutes=10, id="wedge-probe")
    run_daemon._apply_schedule(scheduler, ["0 9 * * *", "0 18 * * *"], "settings")
    assert [j for j, _ in _crons(scheduler)] == ["cron:0", "cron:1"]
    run_daemon._apply_schedule(scheduler, ["30 7 * * *"], "settings")
    assert [j for j, _ in _crons(scheduler)] == ["cron:0"]
    assert scheduler.get_job("wedge-probe") is not None
    state = control.read_schedule_state()
    assert state["crons"] == ["30 7 * * *"] and state["source"] == "settings"


def test_watch_reschedules_only_on_a_real_change(daemon):
    scheduler, settings, write = daemon
    write(["0 9 * * *", "0 18 * * *"])
    applied = run_daemon._apply_schedule(scheduler, run_daemon._load_crons(), "settings")
    watch = run_daemon._ScheduleWatch(scheduler, applied, run_daemon._settings_key())
    before = [j.id for j in scheduler.get_jobs()]

    watch()                                             # untouched file
    write(["0 9 * * *", "0 18 * * *"], login={"method": "http"})
    watch()                                             # another section saved: same crons
    assert [j.id for j in scheduler.get_jobs()] == before
    assert control.read_schedule_state()["crons"] == ["0 9 * * *", "0 18 * * *"]

    write(["15 8 * * *"])
    watch()
    assert watch.current == ["15 8 * * *"]
    assert len(_crons(scheduler)) == 1 and "hour='8'" in _crons(scheduler)[0][1]
    assert control.read_schedule_state()["crons"] == ["15 8 * * *"]


def test_watch_keeps_the_schedule_when_the_file_is_broken_or_all_invalid(daemon):
    scheduler, settings, write = daemon
    applied = run_daemon._apply_schedule(scheduler, ["0 9 * * *"], "settings")
    watch = run_daemon._ScheduleWatch(scheduler, applied, None)

    settings.write_text("schedule: [unclosed", encoding="utf-8")
    watch()
    assert watch.current == ["0 9 * * *"]

    write(["not a cron", "99 99 * * *"])
    watch()
    assert watch.current == ["0 9 * * *"]

    write(["bad", "0 20 * * *"])                         # partly valid: the valid part wins
    watch()
    assert watch.current == ["0 20 * * *"]


def test_an_empty_or_missing_schedule_means_the_defaults(daemon):
    scheduler, settings, write = daemon
    applied = run_daemon._apply_schedule(scheduler, ["0 12 * * *"], "settings")
    watch = run_daemon._ScheduleWatch(scheduler, applied, None)
    write([])
    watch()
    assert watch.current == run_daemon._DEFAULT_CRONS
    assert control.read_schedule_state()["source"] == "default"

    settings.unlink()
    assert run_daemon._load_crons() == run_daemon._DEFAULT_CRONS
    settings.write_text("schedule: null\n", encoding="utf-8")
    assert run_daemon._read_crons() == []


# --- a scheduled round and a colleague's single-account scan --------------------

@pytest.fixture
def round_env(monkeypatch, isolated_control_dir):
    calls, sleeps = [], []
    monkeypatch.setattr(run_daemon.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(run_daemon, "_refresh_ops_alerts", lambda: None)
    return calls, sleeps


def _holder(monkeypatch, **state):
    monkeypatch.setattr(run_daemon.control, "read_state", lambda: state)


def test_the_round_waits_for_a_single_account_scan(round_env, monkeypatch):
    calls, sleeps = round_env
    outcomes = [{"skipped": "already_running"}, {"skipped": "already_running"}, {"results": []}]
    monkeypatch.setattr(run_daemon, "run_all", lambda **kw: calls.append(kw) or outcomes.pop(0))
    _holder(monkeypatch, kind="scan_one", status="running")
    assert run_daemon._run_round() == {"results": []}
    assert len(calls) == 3 and len(sleeps) == 2


@pytest.mark.parametrize("state", [
    {"kind": "run_all", "status": "running"},   # another full round: skip as before
    {"kind": "scan_one", "status": "stale"},    # a wedged scan: the wedge probe handles it
])
def test_the_round_does_not_wait_for_anything_else(round_env, monkeypatch, state):
    calls, sleeps = round_env
    monkeypatch.setattr(run_daemon, "run_all", lambda **kw: calls.append(kw) or {"skipped": "already_running"})
    _holder(monkeypatch, **state)
    assert run_daemon._run_round()["skipped"]
    assert len(calls) == 1 and sleeps == []


def test_the_wait_is_bounded(round_env, monkeypatch):
    calls, sleeps = round_env
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(run_daemon.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(run_daemon, "run_all", lambda **kw: calls.append(kw) or {"skipped": "already_running"})
    _holder(monkeypatch, kind="scan_one", status="running")
    assert run_daemon._run_round()["skipped"]
    assert len(calls) <= run_daemon._SHORT_RUN_WAIT_SECONDS // 100 + 2
