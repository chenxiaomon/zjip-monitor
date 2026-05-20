"""Notification dispatchers: DingTalk, WeChat Work, Email.

Only channels enabled in config/settings.yaml are used.
Only called when DiffResult.has_changes is True.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import smtplib
import time
import urllib.parse
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
import yaml
from loguru import logger

from .differ import DiffResult, RecordChange
from .scraper import decode_status

_ROOT = Path(__file__).parent.parent
_SETTINGS_FILE = _ROOT / "config" / "settings.yaml"


def _load_notify_cfg() -> dict:
    if _SETTINGS_FILE.exists():
        data = yaml.safe_load(_SETTINGS_FILE.read_text(encoding="utf-8")) or {}
        return data.get("notify", {})
    return {}


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _format_message(diff: DiffResult, fetch_time: datetime) -> str:
    """Build the notification text (Markdown)."""
    ts = fetch_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "## 📢 浙江省数据知识产权平台 - 状态更新",
        f"**公司：** {diff.company}",
        "",
    ]

    if diff.changed:
        lines.append(f"### ✅ 状态变更（{len(diff.changed)} 条）")
        for c in diff.changed:
            lines.append(f"- `{c.reg_no}` {c.name[:25]}")
            lines.append(f"  {c.old_status} → **{c.new_status}**")
        lines.append("")

    if diff.added:
        lines.append(f"### 🆕 新增登记（{len(diff.added)} 条）")
        for r in diff.added:
            no = r.get("dataRegNo", "")
            name = r.get("dataRegName", "")[:30]
            status = decode_status(r.get("dataRegAuditStatus"))
            lines.append(f"- `{no}` {name}（{status}）")
        lines.append("")

    if diff.removed:
        lines.append(f"### ⚠️  记录消失（{len(diff.removed)} 条，请核查）")
        for r in diff.removed:
            no = r.get("dataRegNo", "")
            name = r.get("dataRegName", "")[:30]
            lines.append(f"- `{no}` {name}")
        lines.append("")

    lines.append(f"抓取时间：{ts}")
    return "\n".join(lines)


def _format_plain_text(diff: DiffResult, fetch_time: datetime) -> str:
    """Plain-text version for email body / fallback."""
    ts = fetch_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "浙江省数据知识产权平台 - 状态更新",
        f"公司：{diff.company}",
        "",
    ]
    if diff.changed:
        lines.append(f"状态变更（{len(diff.changed)} 条）：")
        for c in diff.changed:
            lines.append(f"  - {c.reg_no} {c.name[:25]}")
            lines.append(f"    {c.old_status} → {c.new_status}")
    if diff.added:
        lines.append(f"新增登记（{len(diff.added)} 条）：")
        for r in diff.added:
            lines.append(f"  - {r.get('dataRegNo', '')} {r.get('dataRegName', '')[:30]}")
    if diff.removed:
        lines.append(f"记录消失（{len(diff.removed)} 条，请核查）：")
        for r in diff.removed:
            lines.append(f"  - {r.get('dataRegNo', '')} {r.get('dataRegName', '')[:30]}")
    lines += ["", f"抓取时间：{ts}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DingTalk
# ---------------------------------------------------------------------------

def _dingtalk_sign(secret: str) -> tuple[str, str]:
    """Return (timestamp_ms, sign) for DingTalk HMAC-SHA256 signing."""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    sig = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(b64encode(sig))
    return ts, sign


def send_dingtalk(diff: DiffResult, fetch_time: datetime, cfg: dict) -> bool:
    """Send a DingTalk group robot message.

    cfg keys:
        webhook:  Full webhook URL (required)
        secret:   Signing secret for HMAC (optional; omit if webhook has no secret)
        enabled:  bool (checked by caller)

    Returns True on success.
    """
    webhook: str = cfg.get("webhook", "").strip()
    if not webhook:
        logger.error("DingTalk webhook URL is empty — check settings.yaml")
        return False

    url = webhook
    secret: str = cfg.get("secret", "").strip()
    if secret:
        ts, sign = _dingtalk_sign(secret)
        url = f"{webhook}&timestamp={ts}&sign={sign}"

    md_text = _format_message(diff, fetch_time)
    title = f"数知产权状态更新 - {diff.company} - {diff.summary()}"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": md_text,
        },
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info(f"[{diff.company}] DingTalk notification sent ✓")
            return True
        else:
            logger.error(
                f"[{diff.company}] DingTalk API error: "
                f"errcode={result.get('errcode')} errmsg={result.get('errmsg')}"
            )
            return False
    except Exception as exc:
        logger.error(f"[{diff.company}] DingTalk send failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# WeChat Work (企业微信)
# ---------------------------------------------------------------------------

def send_wechat(diff: DiffResult, fetch_time: datetime, cfg: dict) -> bool:
    """Send a WeChat Work group robot message.

    cfg keys:
        webhook:  Full webhook URL (required)
        enabled:  bool (checked by caller)
    """
    webhook: str = cfg.get("webhook", "").strip()
    if not webhook:
        logger.error("WeChat Work webhook URL is empty — check settings.yaml")
        return False

    md_text = _format_message(diff, fetch_time)
    payload = {"msgtype": "markdown", "markdown": {"content": md_text}}

    try:
        resp = requests.post(webhook, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info(f"[{diff.company}] WeChat Work notification sent ✓")
            return True
        else:
            logger.error(
                f"[{diff.company}] WeChat Work API error: "
                f"errcode={result.get('errcode')} errmsg={result.get('errmsg')}"
            )
            return False
    except Exception as exc:
        logger.error(f"[{diff.company}] WeChat Work send failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Email (SMTP)
# ---------------------------------------------------------------------------

def send_email(diff: DiffResult, fetch_time: datetime, cfg: dict) -> bool:
    """Send an email notification via SMTP.

    cfg keys:
        smtp_host, smtp_port, smtp_user, smtp_password,
        from_addr, to_addrs (list or comma-separated str)
        use_tls: bool (default true)
    """
    required = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "from_addr", "to_addrs")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        logger.error(f"Email config missing fields: {missing}")
        return False

    to_raw = cfg["to_addrs"]
    to_addrs: list[str] = (
        [a.strip() for a in to_raw.split(",")] if isinstance(to_raw, str) else list(to_raw)
    )

    subject = f"数知产权状态更新 - {diff.company} - {diff.summary()}"
    body = _format_plain_text(diff, fetch_time)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    use_tls: bool = cfg.get("use_tls", True)
    port: int = int(cfg["smtp_port"])

    try:
        if use_tls:
            server = smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=15)
        else:
            server = smtplib.SMTP(cfg["smtp_host"], port, timeout=15)
            server.starttls()
        with server:
            server.login(cfg["smtp_user"], cfg["smtp_password"])
            server.sendmail(cfg["from_addr"], to_addrs, msg.as_string())
        logger.info(f"[{diff.company}] Email sent to {to_addrs} ✓")
        return True
    except Exception as exc:
        logger.error(f"[{diff.company}] Email send failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def notify(diff: DiffResult, fetch_time: Optional[datetime] = None) -> None:
    """Send notifications for *diff* on all enabled channels.

    Silently returns if diff.has_changes is False (caller should check first,
    but this is a safe guard).
    """
    if not diff.has_changes:
        return

    if fetch_time is None:
        fetch_time = datetime.now(timezone.utc)

    cfg = _load_notify_cfg()

    dingtalk_cfg = cfg.get("dingtalk", {})
    if dingtalk_cfg.get("enabled"):
        send_dingtalk(diff, fetch_time, dingtalk_cfg)

    wechat_cfg = cfg.get("wechat", {})
    if wechat_cfg.get("enabled"):
        send_wechat(diff, fetch_time, wechat_cfg)

    email_cfg = cfg.get("email", {})
    if email_cfg.get("enabled"):
        send_email(diff, fetch_time, email_cfg)
