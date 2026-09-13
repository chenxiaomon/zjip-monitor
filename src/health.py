"""Machine-readable health snapshot behind the public GET /health endpoint.

deploy.sh polls `/health?strict=1` after every restart (15 tries × 2s, wanting
200) and dumps the JSON when it gets 503, so this module answers exactly one
question: *did what we just deployed come up, and is it still doing its job?*
It reports five independent facts — which build is running, how long the web
process has been up, whether scans are still succeeding, whether the last run
is wedged, and whether the disk still has room — and folds them into a flat
list of pass/fail checks a shell script can branch on.

Two constraints shape everything here:

1. PUBLIC. The box has no domain; nginx serves plain HTTP on port 80 to the
   whole internet, and /health is deliberately outside the login gate (an
   auth-walled probe cannot distinguish "wedged" from "down" without putting
   credentials in the prober). So the payload carries ONLY counts,
   timestamps, status codes and configured thresholds — never a company name,
   username, password, token, webhook or filesystem path. That is also why a
   failed collector is reported by its exception CLASS name and nothing else:
   repr(FileNotFoundError) carries an absolute path.

2. NEVER RAISE. A probe that crashes is indistinguishable from a dead
   service, and a 500 here would fail a deploy that actually succeeded. Every
   collector is isolated: a failing one degrades its own block to null and
   fails its own check, and the snapshot is still produced.

No FastAPI import — the collector is plain stdlib + src.*, so it unit-tests
directly and the daemon can reuse it later. Sibling modules are imported as
MODULES (``control.read_state()``, not ``read_state()``) so that redirecting
those modules at temp paths, as the tests do, takes effect here too.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from . import control, diskstat, status_store
from .config_loader import load_accounts
from .runtime_settings import DiskSettings, OpsAlertSettings, disk_settings, ops_alert_settings

_ROOT = Path(__file__).parent.parent

# Written by deploy.sh on the server (REMOTE/RELEASE), one line of four
# space-separated fields: <40-char sha> <branch> <UTC ISO-Z> <deployer host>.
# It is excluded from rsync and is absent on a box that has never been
# deployed with the current script — which is NOT a health problem.
_RELEASE_FILE = _ROOT / "RELEASE"
_RELEASE_FIELDS = 4
_SHA_DISPLAY_CHARS = 12

_MIB = 1024 * 1024

# status_store entries that were created but never scanned carry status=None.
_NEVER_SCANNED = "never"
# Hard cap on any status string echoed back: the file is ours, but this is a
# public endpoint and a hand-edited accounts_status.json must not be able to
# push arbitrary text through it.
_STATUS_KEY_MAX_CHARS = 32

# Process start, recorded once at import. monotonic() drives the uptime so an
# NTP step (the box runs UTC and has been corrected before) cannot produce a
# negative or wildly inflated age.
_STARTED_AT = datetime.now(timezone.utc)
_STARTED_MONOTONIC = time.monotonic()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _parse_iso(value: Any) -> datetime | None:
    """ISO-8601 string -> aware UTC datetime; None for anything unparsable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        # Python 3.10 (dev) cannot parse a trailing "Z"; 3.12 (production) can.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Everything we write is UTC; a naive stamp is an older/hand-edited file.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat(timespec="seconds")


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def _safe(name: str, errors: dict[str, str], fn: Callable[[], Any]) -> Any:
    """Run one collector; on failure record its exception class and return None."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — see module docstring: never raise
        errors[name] = type(exc).__name__
        logger.warning(f"/health: 采集 {name} 失败（已降级）: {exc!r}")
        return None


def _stale_hours() -> float:
    """ops_alerts.stale_hours, falling back to its default if settings break."""
    try:
        return ops_alert_settings().stale_hours
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"/health: 读取 ops_alerts.stale_hours 失败，使用默认值: {exc!r}")
        return OpsAlertSettings().stale_hours


def _min_free_mb() -> float:
    try:
        return disk_settings().min_free_mb
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"/health: 读取 disk.min_free_mb 失败，使用默认值: {exc!r}")
        return DiskSettings().min_free_mb


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def _collect_version() -> dict[str, Any] | None:
    """Parse RELEASE. None when absent or malformed — never an error.

    A missing RELEASE only means "not deployed by this deploy.sh yet", which
    is the state of the server right now; letting that degrade the endpoint
    would make every fresh box fail its own post-deploy probe.
    """
    try:
        raw = _RELEASE_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    stripped = raw.strip()
    line = stripped.splitlines()[0] if stripped else ""
    parts = line.split()
    if len(parts) != _RELEASE_FIELDS:
        logger.warning("/health: RELEASE 文件格式不符（应为 4 段空格分隔），版本信息按缺失处理")
        return None
    sha, branch, deployed_at, deployed_by = parts
    return {
        # The short sha is what an operator compares against `git log`; the
        # full 40 chars add nothing and make the JSON harder to eyeball.
        "sha": sha[:_SHA_DISPLAY_CHARS],
        "branch": branch,
        "deployed_at": deployed_at,
        "deployed_by": deployed_by,
    }


def _collect_web() -> dict[str, Any]:
    return {
        "started_at": _iso(_STARTED_AT),
        "uptime_seconds": int(max(time.monotonic() - _STARTED_MONOTONIC, 0.0)),
    }


def _collect_run() -> dict[str, Any] | None:
    """Last/current scan run, from data/control/state.json.

    Only the fields a probe can act on. state.json also carries run_id, kind,
    progress (which names the company being scanned) and error text — none of
    that may cross a public boundary.
    """
    state = control.read_state()
    if not isinstance(state, dict):
        return None
    pid = state.get("pid")
    return {
        "status": state.get("status"),
        "trigger": state.get("trigger"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        # read_state() rewrites a dead holder's status to "stale" and sets
        # holder_alive: a LIVE process with a dead heartbeat still owns
        # run.lock, so no further scan can start until it is restarted.
        "wedged": state.get("status") == "stale" and bool(state.get("holder_alive")),
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
    }


def _collect_scans(errors: dict[str, str]) -> dict[str, Any]:
    """Aggregate per-account status into counts only — never per-account rows."""
    accounts: int | None = None
    try:
        accounts = len(load_accounts())
    except Exception as exc:  # noqa: BLE001 — ConfigError, OSError, decrypt failures
        errors["accounts"] = type(exc).__name__
        logger.warning(f"/health: 读取账号配置失败: {exc!r}")

    entries: dict[str, Any] = {}
    try:
        entries = status_store.get_all()
    except Exception as exc:  # noqa: BLE001
        errors["status_store"] = type(exc).__name__
        logger.warning(f"/health: 读取账号状态失败: {exc!r}")

    counts: dict[str, int] = {}
    paused = 0
    latest: datetime | None = None
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("status") or _NEVER_SCANNED)[:_STATUS_KEY_MAX_CHARS]
        counts[key] = counts.get(key, 0) + 1
        if entry.get("login_paused"):
            paused += 1
        dt = _parse_iso(entry.get("last_success_at"))
        if dt is not None and (latest is None or dt > latest):
            latest = dt

    hours: float | None = None
    if latest is not None:
        # Clamp at 0: a stamp from the future (clock step) should read "just
        # now" rather than as a negative age that silently passes the threshold.
        age = (datetime.now(timezone.utc) - latest).total_seconds()
        hours = round(max(age, 0.0) / 3600, 2)

    return {
        "accounts": accounts,
        "last_success_at": _iso(latest),
        "hours_since_success": hours,
        "status_counts": dict(sorted(counts.items())),
        "paused": paused,
    }


def _collect_disk(errors: dict[str, str]) -> dict[str, Any]:
    """Live free space (cheap statvfs) + the last run's disk_stats.json."""
    free_mb: int | None = None
    percent_used: float | None = None
    min_free_mb = _min_free_mb()
    low = False
    try:
        # check_free() is the exact call the attachment guard makes, so
        # /health and "downloads were skipped" can never disagree; free_space
        # over the guard's own path adds percent_used, which check_free drops.
        check = diskstat.check_free()
        space = diskstat.free_space(diskstat._guard_path())
        free_mb = round(check.free_mb)
        percent_used = space.percent_used
        min_free_mb = check.min_free_mb
        low = check.low
    except Exception as exc:  # noqa: BLE001
        errors["disk_free"] = type(exc).__name__
        logger.warning(f"/health: 读取磁盘剩余空间失败: {exc!r}")

    stats: Any = None
    try:
        stats = diskstat.read_stats()
    except Exception as exc:  # noqa: BLE001
        errors["disk_stats"] = type(exc).__name__
        logger.warning(f"/health: 读取 disk_stats.json 失败: {exc!r}")

    # Attachment size comes from the snapshot the previous run wrote, never
    # from a live walk: data/attachments/ is the tree that grows without
    # bound, and du-ing it on an unauthenticated endpoint would hand anyone a
    # way to pin the single vCPU. Absent snapshot = null, not an error.
    attachments_mb: int | None = None
    attachments_over_limit = False
    stats_collected_at: str | None = None
    if isinstance(stats, dict):
        size = ((stats.get("dirs") or {}).get("attachments") or {}).get("bytes")
        if isinstance(size, (int, float)) and not isinstance(size, bool):
            attachments_mb = int(size / _MIB)
        attachments_over_limit = bool(stats.get("attachments_over_limit"))
        collected = stats.get("collected_at")
        stats_collected_at = collected if isinstance(collected, str) else None

    return {
        "free_mb": free_mb,
        "percent_used": percent_used,
        "min_free_mb": min_free_mb,
        "low": low,
        "attachments_mb": attachments_mb,
        "attachments_over_limit": attachments_over_limit,
        "stats_collected_at": stats_collected_at,
    }


# ---------------------------------------------------------------------------
# Checks — one dict per named condition, all of them fed into `status`
# ---------------------------------------------------------------------------

def _accounts_check(scans: dict | None, errors: dict[str, str]) -> dict[str, Any]:
    reason = errors.get("accounts") or errors.get("scans")
    if scans is None or scans.get("accounts") is None:
        return _check("accounts_configured", False, f"账号配置读取失败（{reason or '未知错误'}）")
    total = scans["accounts"]
    if total <= 0:
        return _check("accounts_configured", False, "账号配置中没有任何账号")
    return _check("accounts_configured", True, f"已配置 {total} 个账号")


def _recent_success_check(
    scans: dict | None, errors: dict[str, str], stale_hours: float
) -> dict[str, Any]:
    if scans is None:
        return _check("recent_success", False, f"账号状态读取失败（{errors.get('scans') or '未知错误'}）")
    if "status_store" in errors:
        return _check("recent_success", False, f"账号状态读取失败（{errors['status_store']}）")
    hours = scans.get("hours_since_success")
    if hours is None:
        return _check("recent_success", False, "尚无任何账号巡检成功的记录")
    if hours > stale_hours:
        return _check(
            "recent_success", False,
            f"距上次巡检成功已 {hours:.1f} 小时，超过阈值 {stale_hours:g} 小时",
        )
    return _check(
        "recent_success", True,
        f"距上次巡检成功 {hours:.1f} 小时（阈值 {stale_hours:g} 小时）",
    )


def _run_check(run: dict | None, errors: dict[str, str]) -> dict[str, Any]:
    if "run" in errors:
        return _check("run_not_wedged", False, f"巡检运行状态读取失败（{errors['run']}）")
    if run is None:
        return _check("run_not_wedged", True, "暂无巡检运行记录")
    if run.get("wedged"):
        return _check(
            "run_not_wedged", False,
            "上一轮巡检进程卡死：心跳已停但进程仍持有运行锁，需重启 zjip-daemon",
        )
    return _check("run_not_wedged", True, f"最近一轮巡检状态：{run.get('status')}")


def _disk_check(disk: dict | None, errors: dict[str, str]) -> dict[str, Any]:
    reason = errors.get("disk_free") or errors.get("disk")
    if disk is None or disk.get("free_mb") is None:
        return _check("disk_free", False, f"磁盘剩余空间读取失败（{reason or '未知错误'}）")
    if disk["low"]:
        return _check(
            "disk_free", False,
            f"磁盘剩余 {disk['free_mb']}MB 低于下限 {disk['min_free_mb']:g}MB，附件下载已暂停",
        )
    used = disk.get("percent_used")
    used_text = "未知" if used is None else f"{used}%"
    return _check(
        "disk_free", True,
        f"磁盘剩余 {disk['free_mb']}MB（下限 {disk['min_free_mb']:g}MB，已用 {used_text}）",
    )


def _attachments_check(disk: dict | None, errors: dict[str, str]) -> dict[str, Any]:
    if disk is None:
        return _check("attachments_size", False, f"附件占用读取失败（{errors.get('disk') or '未知错误'}）")
    if "disk_stats" in errors:
        return _check("attachments_size", False, f"附件占用读取失败（{errors['disk_stats']}）")
    if disk.get("attachments_mb") is None:
        return _check("attachments_size", True, "尚无磁盘统计快照（下一轮巡检结束后生成）")
    if disk["attachments_over_limit"]:
        return _check(
            "attachments_size", False,
            f"附件目录已占用 {disk['attachments_mb']}MB，超过配置上限，需人工清理",
        )
    return _check("attachments_size", True, f"附件目录占用 {disk['attachments_mb']}MB，未超上限")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def collect_health() -> dict[str, Any]:
    """One health snapshot. Safe to call from any process; never raises."""
    errors: dict[str, str] = {}
    version = _safe("version", errors, _collect_version)
    run = _safe("run", errors, _collect_run)
    scans = _safe("scans", errors, lambda: _collect_scans(errors))
    disk = _safe("disk", errors, lambda: _collect_disk(errors))
    stale_hours = _stale_hours()

    checks = [
        _accounts_check(scans, errors),
        _recent_success_check(scans, errors, stale_hours),
        _run_check(run, errors),
        _disk_check(disk, errors),
        _attachments_check(disk, errors),
    ]
    return {
        # Degraded = any failing check. Callers branch on this single word;
        # the checks array says which one and why.
        "status": "degraded" if any(not c["ok"] for c in checks) else "ok",
        "version": version,
        "web": _collect_web(),
        "run": run,
        "scans": scans,
        "disk": disk,
        "checks": checks,
    }
