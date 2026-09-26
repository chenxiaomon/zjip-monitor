"""Notify page: notification channel config + recent change events.

Two rules govern the secret fields (webhook URLs carry an access_token, the
DingTalk signing secret and the SMTP password are credentials):

  * they are never rendered back into the page, on GET or on POST;
  * an empty submission therefore means "leave unchanged", exactly like the
    account password field — clearing one needs its explicit 清除 checkbox.

Before this, POST redacted the fields but the form still rebound them, so the
inputs came back empty and a second click on 保存 wrote those empty strings
into settings.yaml. The channel stayed enabled with no webhook, every push
failed with one log line, and the page still looked healthy.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from web.deps import int_or_none, templates
from web.services.events import read_events
from web.services.settings_svc import load_settings, save_settings

router = APIRouter()

_CHANNELS = ("dingtalk", "wechat", "email")
_CHANNEL_LABELS = {"dingtalk": "钉钉", "wechat": "企业微信", "email": "邮件"}

# Never echoed back to the browser, on any request method.
_SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "dingtalk": ("webhook", "secret"),
    "wechat": ("webhook",),
    "email": ("smtp_password",),
}


def _display_cfg(channel: str, ch_cfg: dict) -> dict:
    """Channel config safe to render: secrets blanked, with a `<field>_set`
    flag so the form can say 「已保存，留空则不修改」 instead of looking empty."""
    shown = dict(ch_cfg or {})
    for field in _SECRET_FIELDS.get(channel, ()):
        shown[f"{field}_set"] = bool(str(shown.get(field, "")).strip())
        shown[field] = ""
    return shown


def _apply_secret(target: dict, field: str, submitted: str, clear: bool) -> None:
    """Empty submission = keep the stored value; 清除 = wipe it explicitly."""
    if clear:
        target[field] = ""
    elif submitted.strip():
        target[field] = submitted.strip()


def _notify_ctx(errors: dict[str, str] | None = None) -> dict:
    cfg = load_settings().get("notify", {})
    # These are CHANGE events, not push receipts. The page used to badge every
    # one of them 「已发送」 purely because some channel was enabled — so a
    # rate-limited webhook or a dead SMTP host still read as "通知正常".
    change_log = read_events(hours=24 * 30, limit=30, with_changes=True)
    enabled = [_CHANNEL_LABELS[ch] for ch in _CHANNELS if cfg.get(ch, {}).get("enabled")]
    return {
        "cfg": {ch: _display_cfg(ch, cfg.get(ch, {})) for ch in _CHANNELS},
        "change_log": change_log,
        "enabled_labels": enabled,
        "any_enabled": bool(enabled),
        "errors": errors or {},
    }


def _channel_response(
    request: Request, channel: str, ch_cfg: dict, *, saved: bool = False, error: str = ""
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_partials/notify_channel.html",
        {
            "channel": channel,
            "ch_cfg": _display_cfg(channel, ch_cfg),
            "saved": saved,
            "error": error,
        },
    )


@router.get("/notify", response_class=HTMLResponse)
def notify_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "notify.html", _notify_ctx())


@router.post("/notify/dingtalk", response_class=HTMLResponse)
def save_dingtalk(
    request: Request,
    enabled: str = Form("off"),
    webhook: str = Form(""),
    secret: str = Form(""),
    clear_webhook: str = Form("off"),
    clear_secret: str = Form("off"),
) -> HTMLResponse:
    data = load_settings()
    ch = data.setdefault("notify", {}).setdefault("dingtalk", {})
    merged = dict(ch)
    _apply_secret(merged, "webhook", webhook, clear_webhook == "on")
    _apply_secret(merged, "secret", secret, clear_secret == "on")
    merged["enabled"] = enabled == "on"

    if merged["enabled"] and not str(merged.get("webhook", "")).strip():
        return _channel_response(
            request, "dingtalk", ch, error="启用前必须填写 Webhook URL，否则推送会静默失败"
        )

    ch.update(merged)
    save_settings(data)
    return _channel_response(request, "dingtalk", ch, saved=True)


@router.post("/notify/wechat", response_class=HTMLResponse)
def save_wechat(
    request: Request,
    enabled: str = Form("off"),
    webhook: str = Form(""),
    clear_webhook: str = Form("off"),
) -> HTMLResponse:
    data = load_settings()
    ch = data.setdefault("notify", {}).setdefault("wechat", {})
    merged = dict(ch)
    _apply_secret(merged, "webhook", webhook, clear_webhook == "on")
    merged["enabled"] = enabled == "on"

    if merged["enabled"] and not str(merged.get("webhook", "")).strip():
        return _channel_response(
            request, "wechat", ch, error="启用前必须填写 Webhook URL，否则推送会静默失败"
        )

    ch.update(merged)
    save_settings(data)
    return _channel_response(request, "wechat", ch, saved=True)


# Mirrors the fields _send_email_text refuses to run without.
_EMAIL_REQUIRED = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "from_addr", "to_addrs")
_EMAIL_LABELS = {
    "smtp_host": "SMTP 主机", "smtp_port": "端口", "smtp_user": "用户名",
    "smtp_password": "密码", "from_addr": "发件人", "to_addrs": "收件人",
}


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
    clear_smtp_password: str = Form("off"),
) -> HTMLResponse:
    data = load_settings()
    ch = data.setdefault("notify", {}).setdefault("email", {})
    merged = dict(ch)
    merged.update({
        "enabled": enabled == "on",
        "smtp_host": smtp_host.strip(),
        # A non-numeric port used to raise ValueError → HTTP 500 on the page.
        "smtp_port": int_or_none(smtp_port) or 465,
        "smtp_user": smtp_user.strip(),
        "from_addr": from_addr.strip(),
        "to_addrs": to_addrs.strip(),
        "use_tls": use_tls == "on",
    })
    _apply_secret(merged, "smtp_password", smtp_password, clear_smtp_password == "on")

    if merged["enabled"]:
        missing = [_EMAIL_LABELS[k] for k in _EMAIL_REQUIRED if not str(merged.get(k, "")).strip()]
        if missing:
            return _channel_response(
                request, "email", ch,
                error=f"启用前必须填写：{'、'.join(missing)}，否则推送会静默失败",
            )

    ch.update(merged)
    save_settings(data)
    return _channel_response(request, "email", ch, saved=True)
