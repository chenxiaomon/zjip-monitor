"""Ops alerts for the console banner — the only alerting channel that is on.

DingTalk, WeChat Work and email are all `enabled: false` in production, so a
problem that only reaches a webhook reaches nobody, and a problem that only
reaches logs/run_*.log reaches nobody either (an operator who is not already
suspicious never opens them). This module turns facts the scan has already
written down into a short list of 中文 alerts the dashboard shows at the top
of every page:

    accounts_status.json  → 本轮失败账号过多 / 长时间没有成功巡检
    disk_stats.json       → 磁盘告急 / 附件目录超量
    state.json            → 巡检进程卡死（2026-09-04 真实卡了 28 小时）
    pause.flag            → 定时巡检已暂停
    accounts.enc          → 公司名脱敏后重名（快照/附件会互相覆盖）
    快照 + .sync.json facts → 补正即将到期 / 已逾期（逾期视为撤回登记申请）

Two callers, two jobs:

    collect_alerts(usernames)  the web process, on every dashboard refresh.
                               Pure reads through mtime-cached readers plus at
                               most one statvfs — no tree walk, no scan.
    refresh_and_log()          run_daemon.py, once per round. Same list, plus
                               activation/recovery bookkeeping in
                               data/control/ops_alerts.json so a standing
                               alert is logged (and pushed) once instead of
                               twice a day forever.

Nothing here may raise: the banner matters most exactly when the disk is full
or a file came back corrupt, and it is rendered inside a web request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from . import control, diskstat, notifier, status_store, todo
from .config_loader import Account, ConfigError, load_accounts
from .fsutil import atomic_write_text
from .runtime_settings import OpsAlertSettings, attachment_settings, disk_settings, ops_alert_settings

_ROOT = Path(__file__).parent.parent
# Module constant so tests can point the bookkeeping at a temp file.
_STATE_FILE = _ROOT / "data" / "control" / "ops_alerts.json"

_MIB = 1024 * 1024
_SHANGHAI_TZ = timezone(timedelta(hours=8))

# Banner order: the operator must not have to scroll past an FYI to find the
# thing that is on fire.
_LEVEL_ORDER = {"critical": 0, "warning": 1, "info": 2}

# status_store values that mean "this account is fine this round". Anything
# else (login_failed / platform_error / captcha_required / scrape_failed /
# shrink_refused / timeout / error) counts as a failure; None = never scanned.
_HEALTHY_STATUSES = {None, "ok", "partial"}

# How many company names a single alert spells out before it starts counting.
_NAME_LIST_MAX = 5


@dataclass(frozen=True)
class Alert:
    """One banner line. `key` is the dedup identity across runs — it must stay
    stable while the wording changes, since ops_alerts.json is keyed on it."""

    key: str
    level: str          # critical | warning | info
    title: str          # what happened
    detail: str         # the numbers: measured value + the threshold it crossed
    hint: str           # what the operator should do about it
    # Usernames this alert is about; empty for system-wide alerts (disk, wedge,
    # pause), which every user may see. collect_alerts() has already filtered
    # on it — this is here so callers can re-check without re-deriving it.
    accounts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO timestamp written by this project; None when unusable.

    Timestamps are written with datetime.now(timezone.utc).isoformat(), but a
    hand-edited or pre-2026 file may carry a naive one — read those as UTC
    rather than crashing the whole banner on a TypeError."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _fmt_local(dt: datetime | None) -> str:
    """Asia/Shanghai wall clock — the server runs on UTC, the operator doesn't."""
    if dt is None:
        return "未知"
    return dt.astimezone(_SHANGHAI_TZ).strftime("%m-%d %H:%M")


def _safe_company_key(name: str) -> str:
    """The on-disk directory name for a company (snapshots/attachments).

    Same expression as config_loader/attachments_svc: two companies mapping to
    one key share a directory and overwrite each other's history."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def _join_names(names: list[str]) -> str:
    if len(names) <= _NAME_LIST_MAX:
        return "、".join(names)
    return "、".join(names[:_NAME_LIST_MAX]) + f" 等 {len(names)} 个"


def _display_name(username: str, entry: dict) -> str:
    return str(entry.get("company") or username)


def _visible_entries(usernames: set[str] | None) -> dict[str, dict]:
    """Account status rows this viewer may be told about (None = admin)."""
    entries = status_store.get_all()
    if usernames is None:
        return entries
    return {u: e for u, e in entries.items() if u in usernames and isinstance(e, dict)}


def _safe_read(read: Callable[[], Any], fallback: Any, what: str) -> Any:
    """One source failing must not blank the checks that read the others — a
    corrupt accounts_status.json is no reason to hide 磁盘告急."""
    try:
        return read()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"运维告警：{what}读取失败（{exc!r}）— 相关检查已跳过")
        return fallback


# ---------------------------------------------------------------------------
# The checks. Each returns one Alert or None and owns its own thresholds.
# ---------------------------------------------------------------------------

def _check_failed_accounts(entries: dict[str, dict], limit: int) -> Alert | None:
    """1. 本轮失败账号过多 — settings.yaml → ops_alerts.min_failed_accounts."""
    failed = [(u, e) for u, e in entries.items() if e.get("status") not in _HEALTHY_STATUSES]
    # `not failed` is not redundant: a settings.yaml that somehow yields a
    # limit of 0 must not raise a banner saying "0 个账号失败".
    if not failed or len(failed) < limit:
        return None
    names = [_display_name(u, e) for u, e in failed]
    return Alert(
        key="failed_accounts",
        level="critical",
        title=f"本轮巡检有 {len(failed)} 个账号失败",
        detail=(
            f"失败账号数 {len(failed)} 已达阈值 "
            f"ops_alerts.min_failed_accounts={limit}：{_join_names(names)}。"
        ),
        hint="请到「账号」页逐个查看错误：登录失败需更新密码，平台报错一般下轮自动恢复。",
        accounts=[u for u, _ in failed],
    )


def _check_no_recent_success(entries: dict[str, dict], stale_hours: float) -> Alert | None:
    """2. 长时间没有成功巡检 — 整体停摆，不是单个账号的「数据可能过期」。"""
    if not entries:
        # Nothing has ever been scanned in this deployment: a fresh install,
        # not a stalled one. The account cards already say 尚未巡检.
        return None
    times = [t for t in (_parse_iso(e.get("last_success_at")) for e in entries.values()) if t]
    hint = (
        "请确认 daemon 在运行（systemctl status zjip-daemon），"
        "或在「账号」页手动巡检一次确认平台是否可达。"
    )
    if not times:
        return Alert(
            key="no_recent_success",
            level="critical",
            title="没有任何账号巡检成功过",
            detail=(
                f"{len(entries)} 个账号都没有成功巡检记录，"
                f"页面上的数据全部来自快照或为空（阈值 ops_alerts.stale_hours={stale_hours:g} 小时）。"
            ),
            hint=hint,
        )
    latest = max(times)
    hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600
    if hours <= stale_hours:
        return None
    return Alert(
        key="no_recent_success",
        level="critical",
        title=f"已有 {hours:.0f} 小时没有任何账号巡检成功",
        detail=(
            f"最近一次成功巡检在 {_fmt_local(latest)}（{hours:.0f} 小时前），"
            f"超过 ops_alerts.stale_hours={stale_hours:g} 小时 — 整体巡检可能已停摆。"
        ),
        hint=hint,
    )


def _check_correction_due(accounts: list[Account], warn_days: int) -> Alert | None:
    """8. 补正即将到期 — settings.yaml → ops_alerts.correction_warn_days.

    The deadline comes from the 补正通知书 itself (src/todo); a record whose
    notice was never parsed has no deadline and cannot raise this alert."""
    due = [
        c for c in todo.build(accounts)["corrections"]
        if c["days_left"] is not None and c["days_left"] <= warn_days
    ]
    if not due:
        return None
    overdue = [c for c in due if c["days_left"] < 0]
    urgent = [c for c in due if 0 <= c["days_left"] <= 2]
    names = [f"{c['company']} {c['reg_no']}（{c['deadline'][5:]}）" for c in due]
    if overdue:
        title = f"{len(overdue)} 条补正已过截止日期"
        if len(due) > len(overdue):
            title += f"，另 {len(due) - len(overdue)} 条 {warn_days} 天内到期"
    else:
        title = f"{len(due)} 条补正 {warn_days} 天内到期"
    return Alert(
        key="correction_due",
        level="critical" if (overdue or urgent) else "warning",
        title=title,
        detail=f"逾期未答复的，平台视为撤回登记申请：{_join_names(names)}。",
        hint="请尽快在平台提交补正材料；截止日期以通知书为准（仪表盘「待办」可看要点）。",
        accounts=[c["username"] for c in due],
    )


def _check_disk_low(stats: dict[str, Any] | None) -> Alert | None:
    """3. 磁盘告急 — settings.yaml → disk.min_free_mb."""
    measured_at = None
    if isinstance(stats, dict) and "low_disk" in stats:
        if not stats.get("low_disk"):
            return None
        root = stats.get("root") if isinstance(stats.get("root"), dict) else {}
        free_mb = float(root.get("free_mb") or 0.0)
        min_free_mb = float(stats.get("min_free_mb") or disk_settings().min_free_mb)
        measured_at = _parse_iso(stats.get("collected_at"))
    else:
        # No snapshot yet (first boot, or the run that would have written it is
        # the one that is stuck): one statvfs, which is what the guard itself
        # uses — never a tree walk from inside a web request.
        check = diskstat.check_free()
        if not check.low:
            return None
        free_mb, min_free_mb = check.free_mb, check.min_free_mb
    when = f"（{_fmt_local(measured_at)} 测得）" if measured_at else "（实时测得）"
    return Alert(
        key="disk_low",
        level="critical",
        title="磁盘剩余空间告急",
        detail=(
            f"数据分区剩余 {free_mb:.0f}MB{when}，低于 disk.min_free_mb={min_free_mb:g}MB — "
            "附件下载已暂停，只抓列表数据。"
        ),
        hint="请清理 data/attachments/ 或 logs/ 后再跑一轮巡检；空间恢复后附件下载自动继续。",
    )


def _check_attachments_over_limit(stats: dict[str, Any] | None) -> Alert | None:
    """4. 附件目录超量 — settings.yaml → attachments.max_total_mb.

    Only ever answered from disk_stats.json: measuring it means walking
    data/attachments/, which is the scan's job, not a web request's."""
    if not isinstance(stats, dict) or not stats.get("attachments_over_limit"):
        return None
    dirs = stats.get("dirs") if isinstance(stats.get("dirs"), dict) else {}
    att = dirs.get("attachments") if isinstance(dirs.get("attachments"), dict) else {}
    used_mb = float(att.get("bytes") or 0) / _MIB
    limit_bytes = stats.get("attachments_max_total_bytes")
    limit_mb = (
        float(limit_bytes) / _MIB
        if isinstance(limit_bytes, (int, float)) and not isinstance(limit_bytes, bool)
        else attachment_settings().max_total_mb
    )
    return Alert(
        key="attachments_over_limit",
        level="warning",
        title="附件目录已超量",
        detail=(
            f"data/attachments/ 已占用 {used_mb:.0f}MB，超过 "
            f"attachments.max_total_mb={limit_mb:g}MB。"
        ),
        hint=(
            "系统不会自动删除任何附件（公告与证书都是法律文书），"
            "请人工清理 data/attachments/ 下不再需要的目录。"
        ),
    )


def _check_run_wedged() -> Alert | None:
    """5. 巡检进程卡死 — a live holder with a dead heartbeat still owns the
    global run lock, so every later round returns 已有巡检在进行中."""
    state = control.read_state() or {}
    if state.get("status") != "stale" or not state.get("holder_alive"):
        return None
    heartbeat = _parse_iso(state.get("heartbeat_at"))
    return Alert(
        key="run_wedged",
        level="critical",
        title="巡检进程卡死",
        detail=(
            f"巡检进程 pid={state.get('pid')} 还活着但已停止心跳"
            f"（最后心跳 {_fmt_local(heartbeat)}，run_id={state.get('run_id')}）— "
            "它占着全局巡检锁，之后每一轮都会被跳过。"
        ),
        hint="请重启巡检服务：systemctl restart zjip-daemon（daemon 自检也会在 10 分钟内自行退出重启）。",
    )


def _check_daemon_paused() -> Alert | None:
    """6. 定时巡检已暂停 — an operator action, so it is an FYI, not an alarm."""
    if not control.is_daemon_paused():
        return None
    return Alert(
        key="daemon_paused",
        level="info",
        title="定时巡检已暂停",
        detail="data/control/pause.flag 存在，daemon 到点会跳过巡检（手动巡检不受影响）。",
        hint="需要恢复时在「设置」页点「恢复定时巡检」。",
    )


def _check_company_collision(usernames: set[str] | None) -> Alert | None:
    """7. 公司名脱敏后重名 — used to be a one-off log line at decrypt time,
    which a daemon that has been up for weeks no longer shows anywhere."""
    try:
        accounts = load_accounts()
    except ConfigError as exc:
        # Already logged loudly wherever it actually blocks work (web.auth,
        # run_all); the banner just has nothing to say in that case.
        logger.debug(f"运维告警：账号读取失败，重名检查跳过（{exc}）")
        return None

    by_key: dict[str, list[Account]] = {}
    for acc in accounts:
        by_key.setdefault(_safe_company_key(acc.company), []).append(acc)

    collisions: list[tuple[str, list[Account]]] = []
    for key, accs in sorted(by_key.items()):
        if len(accs) < 2:
            continue
        visible = accs if usernames is None else [a for a in accs if a.username in usernames]
        # A collision spanning two groups must not spell the other group's
        # company name out on this user's banner. Showing it with one name
        # removed would be unreadable anyway, so a group user sees the alert
        # only when both colliding companies are its own; the admin sees all.
        if len(visible) < 2:
            continue
        collisions.append((key, visible))
    if not collisions:
        return None

    parts = [f"{'、'.join(a.company for a in accs)}（目录名 {key}）" for key, accs in collisions]
    return Alert(
        key="company_name_collision",
        level="warning",
        title="公司名称脱敏后重名",
        detail=f"以下公司在磁盘上用同一个目录名：{'；'.join(parts)} — 它们的快照与附件会互相覆盖。",
        hint="请在「账号」页把其中一家的公司名称改成不同写法（目录名只保留字母、数字、- 和 _）。",
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect_alerts(usernames: set[str] | None = None) -> list[Alert]:
    """Every ops alert currently standing, most severe first.

    `usernames=None` is the admin view. Given a set, only those accounts are
    counted — a group user must never learn from the banner that another
    group's company failed. System-wide alerts (disk, wedge, pause) carry no
    account name and stay visible to everyone.
    """
    entries = _safe_read(lambda: _visible_entries(usernames), {}, "账号状态")
    settings = _safe_read(ops_alert_settings, OpsAlertSettings(), "ops_alerts 配置")
    stats = _safe_read(diskstat.read_stats, None, "disk_stats.json")
    accounts = _safe_read(
        lambda: [a for a in load_accounts() if usernames is None or a.username in usernames],
        [], "账号配置",
    )

    checks: tuple[Callable[[], Alert | None], ...] = (
        lambda: _check_failed_accounts(entries, settings.min_failed_accounts),
        lambda: _check_no_recent_success(entries, settings.stale_hours),
        lambda: _check_correction_due(accounts, settings.correction_warn_days),
        lambda: _check_disk_low(stats),
        lambda: _check_attachments_over_limit(stats),
        _check_run_wedged,
        _check_daemon_paused,
        lambda: _check_company_collision(usernames),
    )

    alerts: list[Alert] = []
    for check in checks:
        # One broken check (an unreadable file, a surprise type) must not cost
        # the operator the other six.
        try:
            alert = check()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"运维告警检查失败（该项已跳过）: {exc!r}")
            continue
        if alert is not None:
            alerts.append(alert)
    # sorted() is stable, so same-level alerts keep the order above.
    return sorted(alerts, key=lambda a: _LEVEL_ORDER.get(a.level, len(_LEVEL_ORDER)))


# ---------------------------------------------------------------------------
# Activation / recovery bookkeeping — data/control/ops_alerts.json
# ---------------------------------------------------------------------------

def _read_active() -> dict[str, dict]:
    """Which alerts were already standing at the previous refresh.

    A missing or corrupt file reads as "none": re-logging a standing alert is
    noise, losing a recovery line is a gap — neither is worth an exception on
    a box whose disk just filled up."""
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning(f"ops_alerts.json 读取失败（按「当前无告警」处理）: {exc!r}")
        return {}
    alerts = data.get("alerts") if isinstance(data, dict) else None
    if not isinstance(alerts, dict):
        return {}
    return {k: v for k, v in alerts.items() if isinstance(v, dict)}


def _write_active(active: dict[str, dict]) -> None:
    try:
        atomic_write_text(
            _STATE_FILE,
            json.dumps({"version": 1, "alerts": active}, ensure_ascii=False, indent=2),
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping is never fatal
        logger.warning(f"ops_alerts.json 写入失败（非致命）: {exc!r}")


def _log_for(level: str):
    return {
        "critical": logger.critical,
        "warning": logger.warning,
        "info": logger.info,
    }.get(level, logger.warning)


def refresh_and_log() -> list[Alert]:
    """Re-evaluate every alert, log the transitions, push what is new.

    Called once per round by run_daemon.py. Only *newly activated* alerts are
    logged and pushed; a standing alert stays silent until it clears, at which
    point one 已恢复 line is logged. Returns the full admin-wide list (never
    raises — the caller is a scan's finalizer).
    """
    try:
        alerts = collect_alerts()
        current = {a.key: a for a in alerts}
        previous = _read_active()

        new = [a for a in alerts if a.key not in previous]
        for a in new:
            _log_for(a.level)(f"运维告警[{a.key}]：{a.title} — {a.detail} 处理建议：{a.hint}")
        for key, old in previous.items():
            if key not in current:
                logger.info(f"运维告警[{key}]：{old.get('title') or key} — 已恢复")

        now = datetime.now(timezone.utc).isoformat()
        _write_active({
            key: {
                # Keep the original activation time so a future "已持续 N 小时"
                # can be read straight off the file.
                "activated_at": previous.get(key, {}).get("activated_at") or now,
                "level": a.level,
                "title": a.title,
                "detail": a.detail,
            }
            for key, a in current.items()
        })

        if new:
            # No-op today (all three channels are disabled); the moment one is
            # turned on in settings.yaml these start going out, deduped the
            # same way the log lines are.
            try:
                notifier.notify_ops(new)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"运维告警推送失败（非致命）: {exc!r}")
        return alerts
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"运维告警刷新失败（非致命）: {exc!r}")
        return []
