"""Settings page: cron schedule, login params, status code map."""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from src import control
from src.login import DEFAULT_LOGIN_METHOD, LOGIN_METHODS
from src.scraper import AUDIT_STATUS
from web.deps import STATUS_COLORS, templates
from web.services.settings_svc import load_settings, save_settings

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    cfg = load_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "cfg": cfg,
            "crons": cfg.get("schedule", {}).get("crons", []),
            "login": cfg.get("login", {}),
            "scraper": cfg.get("scraper", {}),
            "AUDIT_STATUS": AUDIT_STATUS,
            "STATUS_COLORS": STATUS_COLORS,
            "saved": False,
            "paused": control.is_daemon_paused(),
        },
    )


@router.post("/settings/pause", response_class=HTMLResponse)
def toggle_pause(request: Request) -> HTMLResponse:
    if control.is_daemon_paused():
        control.resume_daemon()
    else:
        control.pause_daemon()
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
    cleaned = [c.strip() for c in crons if c.strip()]

    # Validate before persisting — a bad expression stored here would crash
    # the daemon at its next startup.
    invalid = []
    for expr in cleaned:
        try:
            CronTrigger.from_crontab(expr)
        except ValueError:
            invalid.append(expr)
    if invalid:
        return templates.TemplateResponse(
            request,
            "_partials/settings_schedule.html",
            {
                "crons": cleaned,
                "saved": False,
                "error": f"非法 cron 表达式：{'、'.join(invalid)}（格式：分 时 日 月 周，如 0 9 * * *）",
            },
        )

    data = load_settings()
    data.setdefault("schedule", {})["crons"] = cleaned
    save_settings(data)
    cfg = load_settings()
    return templates.TemplateResponse(
        request,
        "_partials/settings_schedule.html",
        {"crons": cfg.get("schedule", {}).get("crons", []), "saved": True},
    )


@router.post("/settings/login", response_class=HTMLResponse)
def save_login(
    request: Request,
    method: str = Form("auto"),
    token_max_age_hours: int = Form(12),
    headless: str = Form("on"),
) -> HTMLResponse:
    if method not in LOGIN_METHODS:
        method = DEFAULT_LOGIN_METHOD
    data = load_settings()
    data.setdefault("login", {}).update({
        "method": method,
        "token_max_age_hours": token_max_age_hours,
        "headless": headless == "on",
    })
    save_settings(data)
    cfg = load_settings()
    return templates.TemplateResponse(
        request,
        "_partials/settings_login.html",
        {"login": cfg.get("login", {}), "saved": True},
    )
