"""Settings page: pause, cron schedule (hot-reloaded by the daemon), login
params, alert/disk thresholds, status code map."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from src import control
from src.login import DEFAULT_LOGIN_METHOD, LOGIN_METHODS
from src.runtime_settings import attachment_settings, disk_settings, ops_alert_settings
from src.scraper import AUDIT_STATUS
from src.status_store import _max_failures
from web.deps import STATUS_COLORS, templates
from web.services import audit_log, settings_svc
from web.services.settings_svc import REFUSED_HINT, SettingsUnreadable, load_settings, save_settings

router = APIRouter()

_TIMEZONE = "Asia/Shanghai"
_SHANGHAI = timezone(timedelta(hours=8))
_WEEKDAYS = "一二三四五六日"
_DEFAULT_CRONS = ["0 9 * * *", "0 18 * * *"]  # run_daemon.py's fallback, shown when none is saved


def _changes(before: dict, after: dict) -> dict[str, list]:
    """{key: [old, new]} for what a save actually changed — the operation
    log's detail. Only for sections without secrets."""
    return {
        k: [before.get(k), after.get(k)]
        for k in sorted(set(before) | set(after))
        if before.get(k) != after.get(k)
    }


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------

def _saved_crons(cfg: dict) -> list[str]:
    schedule = cfg.get("schedule")
    crons = schedule.get("crons") if isinstance(schedule, dict) else None
    return [str(c) for c in crons] if isinstance(crons, list) else []


def next_runs(crons: list[str], n: int = 5, now: datetime | None = None) -> list[dict]:
    """The next `n` fire times across all expressions, computed by the same
    APScheduler parser the daemon uses — so an expression that means
    something other than intended (APScheduler counts weekdays from 0 =
    Monday, not Sunday) shows the real days here."""
    now = now or datetime.now(_SHANGHAI)
    times: list[datetime] = []
    for expr in crons:
        try:
            trigger = CronTrigger.from_crontab(expr, timezone=_TIMEZONE)
        except ValueError:
            continue
        prev, cursor = None, now
        for _ in range(n):
            fire = trigger.get_next_fire_time(prev, cursor)
            if fire is None:
                break
            times.append(fire)
            prev, cursor = fire, fire + timedelta(seconds=1)
    times = sorted(set(times))[:n]
    return [{"when": t.strftime("%m-%d %H:%M"), "weekday": _WEEKDAYS[t.weekday()]} for t in times]


def _daemon_status(saved: list[str]) -> dict:
    state = control.read_schedule_state()
    effective = saved or _DEFAULT_CRONS
    if state is None:
        return {"kind": "unknown"}
    kind = "adopted" if [str(c) for c in state["crons"]] == effective else "pending"
    return {"kind": kind, "crons": state["crons"], "loaded_at": state.get("loaded_at"), "source": state.get("source")}


def _schedule_ctx(crons: list[str], *, saved: bool = False, error: str = "") -> dict:
    return {
        "crons": crons,
        "saved": saved,
        "error": error,
        "next_runs": next_runs(crons or _DEFAULT_CRONS),
        "daemon": _daemon_status(crons),
        "default_crons": _DEFAULT_CRONS,
    }


# ---------------------------------------------------------------------------
# Thresholds: the six alert/disk knobs worth tuning from the page
# ---------------------------------------------------------------------------

# (section, key, label, kind, lower, lower_exclusive, upper, unit, help)
# Lower bounds are runtime_settings' own; the upper bounds are sanity caps the
# YAML itself does not enforce (a disk threshold above the disk's size would
# stop attachment downloads for good).
THRESHOLDS = (
    ("ops_alerts", "min_failed_accounts", "失败账号告警", "int", 1, False, 100, "家",
     "本轮巡检失败的账号达到这个数就在横幅告警"),
    ("ops_alerts", "stale_hours", "无成功巡检告警", "num", 0, True, 720, "小时",
     "所有账号都超过这么久没有成功巡检就告警；应不短于两次定时巡检的最长间隔"),
    ("ops_alerts", "correction_warn_days", "补正到期提醒", "int", 0, False, 60, "天",
     "补正截止日期不足这么多天时在横幅提醒"),
    ("login", "max_consecutive_failures", "连续登录失败暂停", "int", 0, False, 20, "次",
     "同一账号连续被平台拒绝这么多次后暂停定时巡检，防止锁号；0 表示从不暂停"),
    ("disk", "min_free_mb", "磁盘剩余下限", "num", 0, False, 51200, "MB",
     "剩余空间低于这个值时停止下载附件；横幅在下一轮巡检后更新"),
    ("attachments", "max_total_mb", "附件目录上限", "num", 0, True, 102400, "MB",
     "附件目录超过这个大小时告警，不会自动删除；横幅在下一轮巡检后更新"),
)


def _effective_thresholds() -> dict[str, float | int]:
    ops, disk, att = ops_alert_settings(), disk_settings(), attachment_settings()
    return {
        "min_failed_accounts": ops.min_failed_accounts,
        "stale_hours": ops.stale_hours,
        "correction_warn_days": ops.correction_warn_days,
        "max_consecutive_failures": _max_failures(),
        "min_free_mb": disk.min_free_mb,
        "max_total_mb": att.max_total_mb,
    }


def _fmt_number(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _parse_threshold(raw: str, kind: str, lower: float, exclusive: bool, upper: float) -> tuple[float | int | None, str]:
    text = (raw or "").strip()
    if not text:
        return None, "不能为空"
    if kind == "int":
        if not re.fullmatch(r"[+-]?\d+", text):
            return None, "请填写整数"
        value = int(text)
    else:
        try:
            value = float(text)
        except ValueError:
            return None, "请填写数字"
        if not math.isfinite(value):
            return None, "请填写数字"
        if value.is_integer():
            value = int(value)
    if value < lower or (exclusive and value == lower):
        return None, f"必须{'大于' if exclusive else '不小于'} {_fmt_number(lower)}"
    if value > upper:
        return None, f"不能超过 {_fmt_number(upper)}"
    return value, ""


def _thresholds_ctx(values: dict | None = None, errors: dict | None = None, *, saved: bool = False,
                    error: str = "") -> dict:
    effective = _effective_thresholds()
    fields = []
    for section, key, label, kind, lower, exclusive, upper, unit, help_ in THRESHOLDS:
        fields.append({
            "name": key, "label": label, "unit": unit, "help": help_,
            "value": (values or {}).get(key, _fmt_number(effective[key])),
            "error": (errors or {}).get(key, ""),
            "step": "1" if kind == "int" else "any",
        })
    return {"fields": fields, "saved": saved, "error": error}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    cfg = load_settings()
    login = cfg.get("login") if isinstance(cfg.get("login"), dict) else {}
    scraper = cfg.get("scraper") if isinstance(cfg.get("scraper"), dict) else {}
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "cfg": cfg,
            **_schedule_ctx(_saved_crons(cfg)),
            "login": login,
            "scraper": scraper,
            "thresholds": _thresholds_ctx(),
            "AUDIT_STATUS": AUDIT_STATUS,
            "STATUS_COLORS": STATUS_COLORS,
            "paused": control.is_daemon_paused(),
        },
    )


@router.post("/settings/pause", response_class=HTMLResponse)
def toggle_pause(request: Request) -> HTMLResponse:
    if control.is_daemon_paused():
        control.resume_daemon()
    else:
        control.pause_daemon()
    audit_log.record_request(request, "settings.pause", detail={"paused": control.is_daemon_paused()})
    return templates.TemplateResponse(
        request,
        "_partials/settings_pause.html",
        {"paused": control.is_daemon_paused()},
    )


@router.post("/settings/schedule", response_class=HTMLResponse)
def save_schedule(
    request: Request,
    crons: list[str] = Form(default=[]),
) -> HTMLResponse:
    cleaned: list[str] = []
    for c in crons:
        c = " ".join(c.split())
        if c and c not in cleaned:
            cleaned.append(c)

    def page(**kw) -> HTMLResponse:
        if kw.get("error"):
            audit_log.nothing_changed(request)
        return templates.TemplateResponse(request, "_partials/settings_schedule.html", _schedule_ctx(cleaned, **kw))

    if not cleaned:
        return page(error="至少保留一个时间点。想暂时不巡检，请用上方的「暂停定时巡检」。")
    # Validate before persisting — the daemon skips what it cannot parse.
    invalid = []
    for expr in cleaned:
        try:
            CronTrigger.from_crontab(expr)
        except ValueError:
            invalid.append(expr)
    if invalid:
        return page(error=f"非法 cron 表达式：{'、'.join(invalid)}（格式：分 时 日 月 周，如 0 9 * * *）")

    try:
        with settings_svc.editing() as data:
            schedule = settings_svc.section(data, "schedule")
            old_crons = _saved_crons(data)
            schedule["crons"] = cleaned
            save_settings(data)
    except SettingsUnreadable as exc:
        audit_log.record_request(request, "settings.schedule", ok=False, detail={"error": str(exc)})
        return page(error=f"{exc}。{REFUSED_HINT}")
    audit_log.record_request(request, "settings.schedule", detail={"crons": [old_crons, cleaned]})
    return templates.TemplateResponse(
        request, "_partials/settings_schedule.html", _schedule_ctx(_saved_crons(load_settings()), saved=True),
    )


@router.post("/settings/login", response_class=HTMLResponse)
def save_login(
    request: Request,
    method: str = Form("auto"),
    token_max_age_hours: int = Form(12),
    # Absent from the form when unticked: the default must mean "off".
    headless: str = Form("off"),
) -> HTMLResponse:
    if method not in LOGIN_METHODS:
        method = DEFAULT_LOGIN_METHOD
    token_max_age_hours = min(72, max(1, token_max_age_hours))
    try:
        with settings_svc.editing() as data:
            login = settings_svc.section(data, "login")
            before = dict(login)
            login.update({
                "method": method,
                "token_max_age_hours": token_max_age_hours,
                "headless": headless == "on",
            })
            save_settings(data)
    except SettingsUnreadable as exc:
        audit_log.record_request(request, "settings.login", ok=False, detail={"error": str(exc)})
        cfg = load_settings()
        login = cfg.get("login") if isinstance(cfg.get("login"), dict) else {}
        return templates.TemplateResponse(
            request, "_partials/settings_login.html",
            {"login": login, "saved": False, "error": f"{exc}。{REFUSED_HINT}"},
        )
    audit_log.record_request(request, "settings.login", detail=_changes(before, login))
    cfg = load_settings()
    return templates.TemplateResponse(
        request,
        "_partials/settings_login.html",
        {"login": cfg.get("login", {}), "saved": True},
    )


@router.post("/settings/thresholds", response_class=HTMLResponse)
async def save_thresholds(request: Request) -> HTMLResponse:
    form = await request.form()
    submitted = {key: str(form.get(key, "")) for _, key, *_ in THRESHOLDS}
    values, errors = {}, {}
    for section, key, _label, kind, lower, exclusive, upper, _unit, _help in THRESHOLDS:
        value, err = _parse_threshold(submitted[key], kind, lower, exclusive, upper)
        if err:
            errors[key] = err
        else:
            values[(section, key)] = value

    def page(**kw) -> HTMLResponse:
        return templates.TemplateResponse(request, "_partials/settings_thresholds.html", {"thresholds": _thresholds_ctx(**kw)})

    if errors:
        audit_log.nothing_changed(request)
        return page(values=submitted, errors=errors, error="有 %d 项需要修改，本次没有保存" % len(errors))
    changed: dict[str, list] = {}
    try:
        with settings_svc.editing() as data:
            for (section, key), value in values.items():
                target = settings_svc.section(data, section)
                if target.get(key) != value:
                    changed[f"{section}.{key}"] = [target.get(key), value]
                target[key] = value
            save_settings(data)
    except SettingsUnreadable as exc:
        audit_log.record_request(request, "settings.thresholds", ok=False, detail={"error": str(exc)})
        return page(values=submitted, error=f"{exc}。{REFUSED_HINT}")
    audit_log.record_request(request, "settings.thresholds", detail=changed)
    return page(saved=True)
