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
from .settings_cache import get_settings

_ROOT = Path(__file__).parent.parent
_SETTINGS_FILE = _ROOT / "config" / "settings.yaml"


def _load_notify_cfg() -> dict:
    return get_settings().get("notify", {})


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


# WeChat Work caps markdown content at 4096 bytes; keep headroom for the
# truncation notice. DingTalk's limit is higher but the same cap is safe.
_MD_BYTE_LIMIT = 3800


def _truncate_md(text: str, limit: int = _MD_BYTE_LIMIT) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    suffix = "\n\n……内容过长已截断，完整变更请查看控制台或报表"
    budget = limit - len(suffix.encode("utf-8"))
    out: list[str] = []
    size = 0
    for line in text.splitlines():
        s = len(line.encode("utf-8")) + 1
        if size + s > budget:
            break
        out.append(line)
        size += s
    return "\n".join(out) + suffix


def _company_lines(diff: DiffResult, markdown: bool) -> list[str]:
    """Per-company change bullet lines shared by batch formatters."""
    lines: list[str] = []
    for c in diff.changed:
        arrow = f"{c.old_status} → **{c.new_status}**" if markdown else f"{c.old_status} → {c.new_status}"
        lines.append(f"- `{c.reg_no}` {c.name[:25]}：{arrow}" if markdown
                     else f"  - {c.reg_no} {c.name[:25]}：{arrow}")
    for r in diff.added:
        no = r.get("dataRegNo", "")
        name = r.get("dataRegName", "")[:30]
        status = decode_status(r.get("dataRegAuditStatus"))
        lines.append(f"- 🆕 `{no}` {name}（{status}）" if markdown
                     else f"  - 新增 {no} {name}（{status}）")
    for r in diff.removed:
        no = r.get("dataRegNo", "")
        name = r.get("dataRegName", "")[:30]
        lines.append(f"- ⚠️ 记录消失 `{no}` {name}" if markdown
                     else f"  - 记录消失 {no} {name}")
    return lines


def _format_batch_message(diffs: dict[str, DiffResult], fetch_time: datetime) -> str:
    ts = fetch_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "## 📢 浙江省数据知识产权平台 - 状态更新",
        f"**本轮巡检共 {len(diffs)} 家公司有变更**",
        "",
    ]
    for company, diff in diffs.items():
        lines.append(f"### {company}（{diff.summary()}）")
        lines.extend(_company_lines(diff, markdown=True))
        lines.append("")
    lines.append(f"抓取时间：{ts}")
    return "\n".join(lines)


def _format_batch_plain(diffs: dict[str, DiffResult], fetch_time: datetime) -> str:
    ts = fetch_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "浙江省数据知识产权平台 - 状态更新",
        f"本轮巡检共 {len(diffs)} 家公司有变更",
        "",
    ]
    for company, diff in diffs.items():
        lines.append(f"{company}（{diff.summary()}）：")
        lines.extend(_company_lines(diff, markdown=False))
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


def _send_dingtalk_text(title: str, md_text: str, cfg: dict, tag: str) -> bool:
    """Low-level DingTalk markdown sender (tag is used for log lines only)."""
    webhook: str = cfg.get("webhook", "").strip()
    if not webhook:
        logger.error("DingTalk webhook URL is empty — check settings.yaml")
        return False

    url = webhook
    secret: str = cfg.get("secret", "").strip()
    if secret:
        ts, sign = _dingtalk_sign(secret)
        url = f"{webhook}&timestamp={ts}&sign={sign}"

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
            logger.info(f"[{tag}] DingTalk notification sent ✓")
            return True
        else:
            logger.error(
                f"[{tag}] DingTalk API error: "
                f"errcode={result.get('errcode')} errmsg={result.get('errmsg')}"
            )
            return False
    except Exception as exc:
        logger.error(f"[{tag}] DingTalk send failed: {exc}")
        return False


def send_dingtalk(diff: DiffResult, fetch_time: datetime, cfg: dict) -> bool:
    """Send a DingTalk group robot message for one company's diff.

    cfg keys:
        webhook:  Full webhook URL (required)
        secret:   Signing secret for HMAC (optional; omit if webhook has no secret)
        enabled:  bool (checked by caller)

    Returns True on success.
    """
    md_text = _format_message(diff, fetch_time)
    title = f"数知产权状态更新 - {diff.company} - {diff.summary()}"
    return _send_dingtalk_text(title, md_text, cfg, tag=diff.company)


# ---------------------------------------------------------------------------
# WeChat Work (企业微信)
# ---------------------------------------------------------------------------

def _send_wechat_text(md_text: str, cfg: dict, tag: str) -> bool:
    """Low-level WeChat Work markdown sender."""
    webhook: str = cfg.get("webhook", "").strip()
    if not webhook:
        logger.error("WeChat Work webhook URL is empty — check settings.yaml")
        return False

    payload = {"msgtype": "markdown", "markdown": {"content": md_text}}

    try:
        resp = requests.post(webhook, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info(f"[{tag}] WeChat Work notification sent ✓")
            return True
        else:
            logger.error(
                f"[{tag}] WeChat Work API error: "
                f"errcode={result.get('errcode')} errmsg={result.get('errmsg')}"
            )
            return False
    except Exception as exc:
        logger.error(f"[{tag}] WeChat Work send failed: {exc}")
        return False


def send_wechat(diff: DiffResult, fetch_time: datetime, cfg: dict) -> bool:
    """Send a WeChat Work group robot message for one company's diff.

    cfg keys:
        webhook:  Full webhook URL (required)
        enabled:  bool (checked by caller)
    """
    return _send_wechat_text(_format_message(diff, fetch_time), cfg, tag=diff.company)


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
    subject = f"数知产权状态更新 - {diff.company} - {diff.summary()}"
    body = _format_plain_text(diff, fetch_time)
    return _send_email_text(subject, body, cfg, tag=diff.company)


def _send_email_text(subject: str, body: str, cfg: dict, tag: str) -> bool:
    """Low-level SMTP sender."""
    required = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "from_addr", "to_addrs")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        logger.error(f"Email config missing fields: {missing}")
        return False

    to_raw = cfg["to_addrs"]
    to_addrs: list[str] = (
        [a.strip() for a in to_raw.split(",")] if isinstance(to_raw, str) else list(to_raw)
    )

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
        logger.info(f"[{tag}] Email sent to {to_addrs} ✓")
        return True
    except Exception as exc:
        logger.error(f"[{tag}] Email send failed: {exc}")
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


def notify_batch(diffs: dict[str, DiffResult], fetch_time: Optional[datetime] = None) -> None:
    """Send ONE aggregated message per channel for a whole run's changes.

    Called by run_all() after all accounts finish, instead of one message per
    company inside the scan loop — avoids webhook rate limits (DingTalk:
    20 msg/min) and stops slow SMTP from blocking the next account's scan.
    """
    with_changes = {c: d for c, d in diffs.items() if d.has_changes}
    if not with_changes:
        return
    if fetch_time is None:
        fetch_time = datetime.now(timezone.utc)

    if len(with_changes) == 1:
        # Single company — keep the familiar per-company message format
        notify(next(iter(with_changes.values())), fetch_time)
        return

    cfg = _load_notify_cfg()
    tag = f"批量 {len(with_changes)} 家"
    title = f"数知产权状态更新 - {len(with_changes)} 家公司有变更"
    md_text = _truncate_md(_format_batch_message(with_changes, fetch_time))

    dingtalk_cfg = cfg.get("dingtalk", {})
    if dingtalk_cfg.get("enabled"):
        _send_dingtalk_text(title, md_text, dingtalk_cfg, tag)

    wechat_cfg = cfg.get("wechat", {})
    if wechat_cfg.get("enabled"):
        _send_wechat_text(md_text, wechat_cfg, tag)

    email_cfg = cfg.get("email", {})
    if email_cfg.get("enabled"):
        # Email has no size limit — send the full plain-text version
        _send_email_text(title, _format_batch_plain(with_changes, fetch_time), email_cfg, tag)
