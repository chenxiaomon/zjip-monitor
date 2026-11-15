"""Pull to this Mac on a schedule, so every notice lands here without anyone
remembering to run `python -m archive pull`.

    python -m archive schedule install     # launchd LaunchAgent, runs after each production scan
    python -m archive schedule status      # when it runs, how the last runs went
    python -m archive schedule uninstall
    python -m archive schedule test-notify # does a macOS notification come through?
    python -m archive auto                 # what the agent runs (also fine by hand)

A run: notices first (attachments without certificates), then everything
else, then index — each pull pass retried twice, index run even after a
failed pull. It speaks only when there is something to say: a macOS
notification for new notices or for a failure, nothing otherwise. Details go
to ARCHIVE_DIR/schedule.log, one line per pull to pull.log (via=schedule).

Times: production scans at 09:00 and 18:00 Beijing time; the agent runs at
10:30 and 19:30 Beijing. launchd counts in this Mac's own time zone, which
may keep daylight saving (America/Los_Angeles here), so install schedules the
Beijing times under both of its offsets: two harmless extra runs a day, and
no reinstall when the clocks change. A time missed while the Mac sleeps runs
at wake; RunAtLoad covers a Mac that was off, and gives a first run right
at install, while someone is there to answer a privacy prompt.

The repository lives on the Desktop, which macOS guards (TCC): the agent
starts the interpreter by its real path (outside Desktop) with a small
bootstrap that, should the Desktop be refused, says so in a notification
instead of failing silently. Grant the access once (the prompt, or Full Disk
Access for the printed interpreter) — or move the repository off Desktop.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import index, pull
from .config import ArchiveConfig
from .lock import ArchiveBusy, archive_lock

LABEL = "org.zjip-monitor.archive"
BEIJING_TIMES = ((10, 30), (19, 30))
LOG_NAME = "schedule.log"
TITLE = "zjip 本机归档"
LOCK_WAIT_SECONDS = 1800
RETRIES = 2
EXIT_BOOTSTRAP = 78  # EX_CONFIG: the agent could not even start

_BEIJING = ZoneInfo("Asia/Shanghai")
_REPO = Path(__file__).resolve().parent.parent


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def local_zone() -> ZoneInfo | None:
    """This Mac's zone with its daylight-saving rules (not just today's offset)."""
    if os.environ.get("TZ"):
        try:
            return ZoneInfo(os.environ["TZ"])
        except Exception:  # noqa: BLE001 — fall back to /etc/localtime
            pass
    target = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in target:
        try:
            return ZoneInfo(target.split("zoneinfo/", 1)[1])
        except Exception:  # noqa: BLE001
            return None
    return None


def local_times(beijing_times=BEIJING_TIMES, tz: ZoneInfo | None = None,
                year: int | None = None) -> list[tuple[int, int]]:
    """Beijing (hour, minute)s as this Mac's local clock reads them, under the
    zone's winter and summer offsets; duplicates dropped, sorted."""
    tz = tz or local_zone() or _BEIJING
    year = year or datetime.now().year
    out = set()
    for month in (1, 7):
        for hour, minute in beijing_times:
            t = datetime(year, month, 15, hour, minute, tzinfo=_BEIJING).astimezone(tz)
            out.add((t.hour, t.minute))
    return sorted(out)


def beijing_of(local: tuple[int, int], tz: ZoneInfo | None = None) -> str:
    """'10:30' / '10:30（夏令时）' — what a local time means in Beijing today."""
    tz = tz or local_zone() or _BEIJING
    today = datetime.now(tz)
    t = today.replace(hour=local[0], minute=local[1], second=0, microsecond=0).astimezone(_BEIJING)
    return f"{t:%H:%M}"


# --- notifications ---------------------------------------------------------------

def notify(message: str, title: str = TITLE, *, runner: Callable = subprocess.run) -> None:
    """A macOS notification. The text goes in as an argument, never into the
    AppleScript source, so no message can break or extend the script."""
    try:
        runner(["/usr/bin/osascript", "-e", "on run argv",
                "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e", "end run", message[:240], title],
               capture_output=True, timeout=30)
    except Exception as exc:  # noqa: BLE001 — a notification must never fail a run
        print(f"（通知没发出去：{exc}）")


# --- one scheduled run -------------------------------------------------------------

def _companies(paths: list[str], top: int = 3) -> str:
    counts: dict[str, int] = {}
    for p in paths:
        parts = p.split("/")
        if len(parts) > 1:
            counts[parts[1]] = counts.get(parts[1], 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    text = "、".join(f"{c} {n}" for c, n in ranked[:top])
    return text + (f" 等 {len(ranked)} 家" if len(ranked) > top else "")


def run_scheduled(cfg: ArchiveConfig, *, notify_fn: Callable[[str], None] = notify,
                  sleep: Callable[[float], None] = time.sleep, lock_wait: float = LOCK_WAIT_SECONDS) -> int:
    print(f"=== {datetime.now().astimezone():%Y-%m-%d %H:%M %Z} 定时归档开始", flush=True)
    root = cfg.archive_dir
    try:
        with archive_lock(root, what="定时归档 auto", wait=lock_wait, sleep=sleep):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            before = pull.notice_files(root)
            result = pull.pull_jobs(pull.plan(cfg, stamp=stamp, notices_only=True), tag="（通知书）",
                                    retries=RETRIES, sleep=sleep)
            result = result.merge(pull.pull_jobs(pull.plan(cfg, stamp=stamp), retries=RETRIES, sleep=sleep))
            fresh = pull.new_notices(root, before, cfg.db_path)
            pull.log_line(cfg, result, notices=len(fresh), via="schedule")
            index_error = None
            try:
                if index.run(cfg) != 0:
                    index_error = "index 没有完成"
            except Exception as exc:  # noqa: BLE001 — reported below, the pull already counted
                index_error = f"{type(exc).__name__}: {exc}"
                print(f"✗ 入库失败：{index_error}")
    except ArchiveBusy as exc:
        print(f"✗ {exc}，等了 {lock_wait / 60:.0f} 分钟，这次不跑")
        notify_fn(f"本机归档这次没跑：{exc}")
        return 1

    if fresh:
        print(f"新归档通知书 {len(fresh)} 份：" + "、".join(fresh[:20]))
    if result.failed:
        notify_fn(f"本机归档失败：{'、'.join(result.failed)} 没拉下来（已重试 {RETRIES} 次），详见 {root / LOG_NAME}"
                  + (f"。另有新通知书 {len(fresh)} 份已拉到" if fresh else ""))
    elif index_error:
        notify_fn(f"本机归档：文件已拉到，入库失败（{index_error[:80]}），详见 {root / LOG_NAME}")
    elif fresh:
        notify_fn(f"新归档通知书 {len(fresh)} 份（{_companies(fresh)}）")
    print(f"=== {datetime.now().astimezone():%H:%M} 结束", flush=True)
    return 1 if result.failed or index_error else 0


# --- the LaunchAgent ---------------------------------------------------------------

def bootstrap_code(repo: Path, site_packages: Path, env_file: Path | None = None,
                   notifier: str = "/usr/bin/osascript") -> str:
    """What the agent's interpreter runs (python -I -c): put the venv's packages
    and the repository on the path, then `archive auto`. If that much fails —
    the Desktop refused, the venv gone — say so in a notification."""
    args = ["--env", str(env_file)] if env_file else []
    return f"""\
import sys
try:
    import site
    site.addsitedir({str(site_packages)!r})
    sys.path.insert(0, {str(repo)!r})
    from archive.__main__ import main
except BaseException as exc:
    msg = "本机归档没能启动：%s: %s。若是访问「桌面」被拒，请在 系统设置→隐私与安全性→完全磁盘访问 里加入 %s" % (
        type(exc).__name__, exc, sys.executable)
    print(msg, file=sys.stderr)
    try:
        import subprocess
        subprocess.run([{notifier!r}, "-e", "on run argv",
                        "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                        "-e", "end run", msg[:240], {TITLE!r}], timeout=30)
    except BaseException:
        pass
    sys.exit({EXIT_BOOTSTRAP})
sys.exit(main({args!r} + ["auto"]))
"""


def plist(cfg: ArchiveConfig, *, python: Path, repo: Path, site_packages: Path, times: list[tuple[int, int]],
          path_env: str, env_file: Path | None = None) -> dict:
    log = str(cfg.archive_dir / LOG_NAME)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-I", "-c", bootstrap_code(repo, site_packages, env_file)],
        "WorkingDirectory": str(cfg.archive_dir),
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in times],
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "EnvironmentVariables": {"PATH": path_env, "LANG": "en_US.UTF-8", "PYTHONUNBUFFERED": "1"},
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _path_env() -> str:
    dirs = [str(Path(p).parent) for p in (shutil.which("rsync"), shutil.which("ssh")) if p]
    return ":".join(dict.fromkeys(dirs + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]))


def install(cfg: ArchiveConfig, *, env_file: Path | None = None, times=BEIJING_TIMES,
            runner: Callable = subprocess.run, target: Path | None = None) -> int:
    python = Path(os.path.realpath(sys.executable))
    site_packages = Path(sysconfig.get_paths()["purelib"])
    local = local_times(times)
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    data = plist(cfg, python=python, repo=_REPO, site_packages=site_packages, times=local,
                 path_env=_path_env(), env_file=env_file.resolve() if env_file else None)
    target = target or plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    runner(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    with target.open("wb") as fh:
        plistlib.dump(data, fh)
    proc = runner(["launchctl", "bootstrap", _domain(), str(target)], capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"✗ launchctl bootstrap 失败（{proc.returncode}）：{(proc.stderr or '').strip()}")
        return 1
    zone = local_zone()
    print(f"✓ 已安装定时归档 {LABEL}")
    print(f"  本机时区 {zone.key if zone else '未知'}；每天本地 "
          + "、".join(f"{h:02d}:{m:02d}" for h, m in local)
          + "（北京时间 " + "、".join(f"{h:02d}:{m:02d}" for h, m in times) + " 前后，冬夏令时各一套）")
    print(f"  解释器 {python}")
    print(f"  日志 {cfg.archive_dir / LOG_NAME}；每次拉取记入 pull.log（via=schedule）")
    print("  现在会先跑一次。第一次可能弹出：")
    print("   · 「python3.12 想访问“桌面”文件夹中的文件」→ 点「允许」（仓库在桌面上）")
    print("     没弹也没跑成：系统设置 → 隐私与安全性 → 完全磁盘访问，加入上面的解释器")
    print("   · 系统设置 → 通用 → 登录项与扩展 → 允许在后台：打开 python3.12")
    print("   · 通知以「脚本编辑器」的名义出现：系统设置 → 通知 → 脚本编辑器，允许通知")
    print("  稍后用 python -m archive schedule status 看结果")
    return 0


def uninstall(*, runner: Callable = subprocess.run, target: Path | None = None) -> int:
    target = target or plist_path()
    runner(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    if target.exists():
        target.unlink()
        print(f"✓ 已卸载定时归档（删除 {target}）")
    else:
        print("定时归档本来就没有安装")
    return 0


def _launchd_state(runner: Callable) -> dict[str, str]:
    proc = runner(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    if proc.returncode != 0:
        return {}
    out = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key in ("state", "last exit code", "runs", "pid"):
            out.setdefault(key, value.strip())
    return out


def status(cfg: ArchiveConfig, *, runner: Callable = subprocess.run, target: Path | None = None) -> int:
    target = target or plist_path()
    if not target.exists():
        print("定时归档没有安装：python -m archive schedule install")
        return 1
    with target.open("rb") as fh:
        data = plistlib.load(fh)
    times = [(e["Hour"], e["Minute"]) for e in data.get("StartCalendarInterval") or []]
    python = Path(data["ProgramArguments"][0])
    print(f"定时归档 {LABEL}：每天本地 " + "、".join(f"{h:02d}:{m:02d}（北京 {beijing_of((h, m))}）" for h, m in times))
    if not python.exists():
        print(f"✗ 解释器 {python} 不在了（uv 升级过 Python？）——重新运行 schedule install")
    state = _launchd_state(runner)
    if not state:
        print("✗ launchd 里没有它（重启后没加载？）——重新运行 schedule install")
    else:
        print("  launchd：" + "，".join(f"{k} {v}" for k, v in state.items()))
    try:
        lines = (cfg.archive_dir / "pull.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    runs = [ln for ln in lines if ln.endswith("\tvia=schedule")][-5:]
    print("  最近的定时拉取：" + ("" if runs else "还没有"))
    for ln in runs:
        print("   " + ln)
    return 0


def test_notify(*, notify_fn: Callable[[str], None] = notify) -> int:
    notify_fn("测试通知：定时归档能弹出通知")
    print("已发出一条测试通知；没看到的话，在 系统设置 → 通知 → 脚本编辑器 里允许通知")
    return 0

