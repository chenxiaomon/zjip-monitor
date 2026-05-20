"""Notify page: notification channel config + push log."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from web.deps import templates
from web.services.events import read_events
from web.services.settings_svc import load_settings, save_settings

router = APIRouter()

_CHANNELS = ("dingtalk", "wechat", "email")


def _notify_ctx() -> dict:
    cfg = load_settings().get("notify", {})
    # Recent events with changes (proxy for push log)
    all_events = read_events(hours=24 * 30, limit=100)
    push_log = [
        e for e in all_events
        if e.get("changed") or e.get("added") or e.get("removed")
    ][:30]
    any_enabled = any(cfg.get(ch, {}).get("enabled") for ch in _CHANNELS)
    return {"cfg": cfg, "push_log": push_log, "any_enabled": any_enabled}


@router.get("/notify", response_class=HTMLResponse)
def notify_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "notify.html", _notify_ctx())


@router.post("/notify/dingtalk", response_class=HTMLResponse)
def save_dingtalk(
    request: Request,
    enabled: str = Form("off"),
    webhook: str = Form(""),
    secret: str = Form(""),
) -> HTMLResponse:
    data = load_settings()
    data.setdefault("notify", {}).setdefault("dingtalk", {})
    data["notify"]["dingtalk"].update({
        "enabled": enabled == "on",
        "webhook": webhook.strip(),
        "secret": secret.strip(),
    })
    save_settings(data)
    return templates.TemplateResponse(
        request, "_partials/notify_channel.html",
        {"channel": "dingtalk", "ch_cfg": data["notify"]["dingtalk"], "saved": True},
    )


@router.post("/notify/wechat", response_class=HTMLResponse)
def save_wechat(
    request: Request,
    enabled: str = Form("off"),
    webhook: str = Form(""),
) -> HTMLResponse:
    data = load_settings()
    data.setdefault("notify", {}).setdefault("wechat", {})
    data["notify"]["wechat"].update({"enabled": enabled == "on", "webhook": webhook.strip()})
    save_settings(data)
    return templates.TemplateResponse(
        request, "_partials/notify_channel.html",
        {"channel": "wechat", "ch_cfg": data["notify"]["wechat"], "saved": True},
    )


@router.post("/notify/email", response_class=HTMLResponse)
def save_email(
    request: Request,
    enabled: str = Form("off"),
    smtp_host: str = Form(""),
    smtp_port: str = Form("465"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    from_addr: str = Form(""),
    to_addrs: str = Form(""),
    use_tls: str = Form("on"),
) -> HTMLResponse:
    data = load_settings()
    data.setdefault("notify", {}).setdefault("email", {})
    data["notify"]["email"].update({
        "enabled": enabled == "on",
        "smtp_host": smtp_host.strip(),
        "smtp_port": int(smtp_port or 465),
        "smtp_user": smtp_user.strip(),
        "smtp_password": smtp_password.strip(),
        "from_addr": from_addr.strip(),
        "to_addrs": to_addrs.strip(),
        "use_tls": use_tls == "on",
    })
    save_settings(data)
    return templates.TemplateResponse(
        request, "_partials/notify_channel.html",
        {"channel": "email", "ch_cfg": data["notify"]["email"], "saved": True},
    )
