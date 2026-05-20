"""Settings page: cron schedule, login params, status code map."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

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
        },
    )


@router.post("/settings/schedule", response_class=HTMLResponse)
def save_schedule(
    request: Request,
    crons: list[str] = Form(default=[]),
) -> HTMLResponse:
    data = load_settings()
    cleaned = [c.strip() for c in crons if c.strip()]
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
    token_max_age_hours: int = Form(8),
    headless: str = Form("on"),
) -> HTMLResponse:
    data = load_settings()
    data.setdefault("login", {}).update({
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
