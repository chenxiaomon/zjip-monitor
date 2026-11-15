"""archive/schedule.py: the Mac pulls by itself after each production scan."""

import plistlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from archive import index, pull, schedule
from archive.config import ArchiveConfig
from archive.lock import archive_lock

REPO = Path(__file__).resolve().parent.parent
NOON = datetime(2026, 11, 15, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def cfg(tmp_path):
    return ArchiveConfig(server="root@h", remote="/opt/z", archive_dir=tmp_path / "arc", ssh_cmd="ssh")


# --- times -----------------------------------------------------------------------

def test_beijing_times_under_both_los_angeles_offsets():
    # PST (UTC-8): 10:30 → 18:30, 19:30 → 03:30; PDT (UTC-7): 19:30 and 04:30
    assert schedule.local_times(tz=ZoneInfo("America/Los_Angeles"), year=2026) == [
        (3, 30), (4, 30), (18, 30), (19, 30)]


def test_a_zone_without_daylight_saving_runs_twice_a_day():
    assert schedule.local_times(tz=ZoneInfo("Asia/Shanghai"), year=2026) == [(10, 30), (19, 30)]
    assert schedule.local_times(tz=ZoneInfo("Europe/London"), year=2026) == [(2, 30), (3, 30), (11, 30), (12, 30)]


# --- the LaunchAgent -------------------------------------------------------------

def _plist(cfg, tmp_path, **kw):
    return schedule.plist(cfg, python=Path("/Users/x/.local/share/uv/python/bin/python3.12"),
                          repo=Path("/Users/x/Desktop/zjip-monitor"),
                          site_packages=Path("/Users/x/Desktop/zjip-monitor/.venv/lib/python3.12/site-packages"),
                          times=[(3, 30), (18, 30)], path_env="/usr/bin:/bin", **kw)


def test_the_agent_definition(cfg, tmp_path):
    data = _plist(cfg, tmp_path)
    assert data["Label"] == "org.zjip-monitor.archive"
    python, isolated, unbuffered, dash_c, code = data["ProgramArguments"]
    assert "Desktop" not in python and (isolated, unbuffered, dash_c) == ("-I", "-u", "-c")
    assert "Desktop" not in data["WorkingDirectory"]
    assert data["StandardOutPath"] == data["StandardErrorPath"] == str(cfg.archive_dir / "schedule.log")
    assert data["StartCalendarInterval"] == [{"Hour": 3, "Minute": 30}, {"Hour": 18, "Minute": 30}]
    assert data["RunAtLoad"] is True and data["ProcessType"] == "Background"
    assert data["EnvironmentVariables"]["PATH"] == "/usr/bin:/bin"
    compile(code, "<bootstrap>", "exec")
    plistlib.dumps(data)  # serialisable


def test_the_bootstrap_says_why_it_could_not_start(tmp_path):
    """A refused Desktop or a missing venv must show up, not fail silently."""
    code = schedule.bootstrap_code(tmp_path / "no-repo", tmp_path / "no-site", notifier="/usr/bin/true")
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == schedule.EXIT_BOOTSTRAP
    assert "本机归档没能启动" in proc.stderr and "完全磁盘访问" in proc.stderr


def test_the_bootstrap_starts_auto(tmp_path):
    site = Path(next(p for p in sys.path if p.endswith("site-packages")))
    env = tmp_path / "archive.env"
    env.write_text(f"ARCHIVE_DIR={tmp_path / 'arc'}\nSERVER=nobody@127.0.0.1\nSSH_CMD=/usr/bin/false\n", encoding="utf-8")
    code = schedule.bootstrap_code(REPO, site, env_file=env, notifier="/usr/bin/true")
    # stop at the door: no rsync, no notification on the developer's screen
    hook = "try:\n    code = main("
    assert hook in code
    code = code.replace(hook, "import archive.schedule as s\n"
                        "s.run_scheduled = lambda cfg, **k: print('auto reached', cfg.archive_dir) or 0\n" + hook)
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-500:]
    assert f"auto reached {tmp_path / 'arc'}" in proc.stdout  # the env file given at install was read


class FakeLaunchctl:
    def __init__(self, print_out="state = not running\nruns = 3\nlast exit code = 0\n", rc=0):
        self.calls, self.print_out, self.rc = [], print_out, rc

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(argv, 0 if self.print_out else 113, stdout=self.print_out, stderr="")
        rc = self.rc if argv[1] == "bootstrap" else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom" if rc else "")


def test_install_status_uninstall(cfg, tmp_path, capsys):
    target = tmp_path / "LaunchAgents" / "org.zjip-monitor.archive.plist"
    fake = FakeLaunchctl()
    assert schedule.install(cfg, runner=fake, target=target) == 0
    assert [c[1] for c in fake.calls] == ["bootout", "bootstrap"]
    assert fake.calls[1][-1] == str(target)
    data = plistlib.loads(target.read_bytes())
    assert data["StartCalendarInterval"]
    out = capsys.readouterr().out
    assert "已安装定时归档" in out and "完全磁盘访问" in out

    (cfg.archive_dir / "pull.log").write_text(
        "2026-11-15T02:30:00+00:00\troot@h\ttransferred=3\tok\tnotices=1\tvia=schedule\n"
        "2026-11-15T03:00:00+00:00\troot@h\ttransferred=0\tok\tnotices=0\tvia=manual\n", encoding="utf-8")
    assert schedule.status(cfg, runner=fake, target=target) == 0
    out = capsys.readouterr().out
    assert "runs 3" in out and "notices=1\tvia=schedule" in out and "via=manual" not in out

    assert schedule.uninstall(runner=fake, target=target) == 0
    assert not target.exists()
    assert schedule.status(cfg, runner=fake, target=target) == 1


def test_install_reports_a_failed_bootstrap(cfg, tmp_path, capsys):
    assert schedule.install(cfg, runner=FakeLaunchctl(rc=5), target=tmp_path / "a.plist") == 1
    assert "bootstrap 失败" in capsys.readouterr().out


def test_status_notices_a_vanished_interpreter(cfg, tmp_path, capsys):
    target = tmp_path / "a.plist"
    data = _plist(cfg, tmp_path)
    target.write_bytes(plistlib.dumps(data))
    schedule.status(cfg, runner=FakeLaunchctl(print_out=""), target=target)
    out = capsys.readouterr().out
    assert "不在了" in out and "launchd 里没有它" in out


def test_notifications_carry_text_as_an_argument():
    calls = []
    schedule.notify('一份"新"通知书 & end run', runner=lambda argv, **kw: calls.append(argv))
    (argv,) = calls
    assert argv[0] == "/usr/bin/osascript"
    assert argv[-2:] == ['一份"新"通知书 & end run', "zjip 本机归档"]
    assert not any("通知书" in a for a in argv[:-2])  # never inside the script


# --- one scheduled run -------------------------------------------------------------

@pytest.fixture
def fake_pull(cfg, monkeypatch):
    """rsync replaced: each call may drop files into the archive; codes per pass."""
    state = {"calls": [], "codes": {}, "drops": {}}

    def fake_run(argv, **kw):
        notices_pass = "--exclude=国家局登记证书*" in argv
        key = "notices" if notices_pass else argv[-1].rstrip("/").rsplit("/", 1)[-1]
        state["calls"].append(key)
        for rel, body in state["drops"].pop(key, []):
            f = cfg.archive_dir / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(body)
        code = state["codes"].get(key, 0)
        return subprocess.CompletedProcess(argv, code, stdout="Number of files transferred: 1\n", stderr="down")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    monkeypatch.setattr(index, "run", lambda c: state["calls"].append("index") or 0)
    cfg.archive_dir.mkdir(parents=True)
    return state


def test_notices_first_then_the_rest_then_index(cfg, fake_pull, capsys):
    fake_pull["drops"] = {"notices": [("attachments/甲/R1/《补正通知书》.pdf", b"n1"),
                                      ("attachments/乙/R9/《公示通知书》.pdf", b"n2")]}
    said = []
    assert schedule.run_scheduled(cfg, notify_fn=said.append, sleep=lambda s: None, now=NOON) == 0
    assert fake_pull["calls"] == ["notices", "attachments", "snapshots", "events", "audit", "index"]
    assert said == ["新归档通知书 2 份（乙 1、甲 1）"]
    line = (cfg.archive_dir / "pull.log").read_text(encoding="utf-8").splitlines()[-1]
    assert line.endswith("\tok\tnotices=2\tvia=schedule")


def test_a_quiet_run_says_nothing(cfg, fake_pull):
    said = []
    assert schedule.run_scheduled(cfg, notify_fn=said.append, sleep=lambda s: None, now=NOON) == 0
    assert said == []


def test_a_failed_pull_is_retried_then_reported_and_index_still_runs(cfg, fake_pull):
    fake_pull["codes"] = {"snapshots": 12}
    fake_pull["drops"] = {"notices": [("attachments/甲/R1/《补正通知书》.pdf", b"n1")]}
    said, slept = [], []
    assert schedule.run_scheduled(cfg, notify_fn=said.append, sleep=slept.append, now=NOON) == 1
    assert fake_pull["calls"].count("snapshots") == 3 and slept == [60, 300]
    assert fake_pull["calls"][-1] == "index"
    (msg,) = said
    assert msg.startswith("本机归档失败：snapshots 没拉下来（已重试 2 次）") and "新通知书 1 份已拉到" in msg
    assert "failed: snapshots" in (cfg.archive_dir / "pull.log").read_text(encoding="utf-8")


def test_an_index_failure_is_reported(cfg, fake_pull, monkeypatch):
    def broken(c):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(index, "run", broken)
    said = []
    assert schedule.run_scheduled(cfg, notify_fn=said.append, sleep=lambda s: None, now=NOON) == 1
    assert said and "入库失败" in said[0] and "database is locked" in said[0]


def test_a_busy_archive_is_waited_for_then_skipped(cfg, fake_pull, monkeypatch):
    import multiprocessing as mp

    from tests.test_archive_lock import _hold

    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold, args=(cfg.archive_dir, False, ready, release))
    p.start()
    try:
        assert ready.wait(20)
        said = []
        assert schedule.run_scheduled(cfg, notify_fn=said.append, sleep=lambda s: None, lock_wait=0.01, now=NOON) == 1
        assert fake_pull["calls"] == []
        assert said and said[0].startswith("本机归档这次没跑：另一个归档命令正在运行")
    finally:
        release.set()
        p.join(10)
    with archive_lock(cfg.archive_dir, what="x"):
        pass


@pytest.mark.parametrize("hm, skip", [((9, 0), True), ((9, 30), True), ((9, 59), True), ((10, 0), False),
                                      ((10, 30), False), ((18, 30), True), ((19, 30), False), ((3, 0), False)])
def test_runs_inside_a_scan_hour_are_skipped(cfg, fake_pull, hm, skip):
    now = datetime(2026, 7, 15, *hm, tzinfo=ZoneInfo("Asia/Shanghai"))
    said = []
    assert schedule.run_scheduled(cfg, notify_fn=said.append, sleep=lambda s: None, now=now) == 0
    assert (fake_pull["calls"] == []) is skip and said == []


def test_the_summer_extra_runs_are_the_ones_that_skip():
    la = ZoneInfo("America/Los_Angeles")
    shanghai = ZoneInfo("Asia/Shanghai")
    summer = [datetime(2026, 7, 15, h, m, tzinfo=la) for h, m in schedule.local_times(tz=la, year=2026)]
    winter = [datetime(2026, 1, 15, h, m, tzinfo=la) for h, m in schedule.local_times(tz=la, year=2026)]
    assert sorted(f"{t.astimezone(shanghai):%H:%M}" for t in summer if not schedule.in_scan_window(t)) == ["10:30", "19:30"]
    assert sorted(f"{t.astimezone(shanghai):%H:%M}" for t in winter if not schedule.in_scan_window(t)) == [
        "10:30", "11:30", "19:30", "20:30"]


def test_an_unexpected_error_is_said(cfg, fake_pull, monkeypatch):
    def broken(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pull, "log_line", broken)
    said = []
    assert schedule.run_scheduled(cfg, notify_fn=said.append, sleep=lambda s: None, now=NOON) == 1
    assert said and said[0].startswith("本机归档出错：OSError") and "No space left" in said[0]


def test_each_pass_backs_up_into_its_own_folder(cfg, monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(next(a for a in argv if a.startswith("--backup-dir=")) if "--backup" in argv else None)
        return subprocess.CompletedProcess(argv, 0, stdout="Number of files transferred: 0\n", stderr="")
    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    monkeypatch.setattr(index, "run", lambda c: 0)
    cfg.archive_dir.mkdir(parents=True)
    schedule.run_scheduled(cfg, notify_fn=lambda m: None, sleep=lambda s: None, now=NOON)
    notices_dir, full_dir = seen[0], seen[1]
    assert notices_dir.endswith("-notices/attachments") and not full_dir.endswith("-notices/attachments")
    assert notices_dir.rsplit("/", 1)[0] != full_dir.rsplit("/", 1)[0]  # .overwritten/<stamp> differs


def test_install_honours_the_env_variable(cfg, tmp_path, monkeypatch):
    env = tmp_path / "other.env"
    env.write_text("SERVER=x@y\n", encoding="utf-8")
    monkeypatch.setenv("ZJIP_ARCHIVE_ENV", str(env))
    target = tmp_path / "a.plist"
    assert schedule.install(cfg, runner=FakeLaunchctl(), target=target) == 0
    code = plistlib.loads(target.read_bytes())["ProgramArguments"][-1]
    assert f"'--env', {str(env.resolve())!r}" in code
