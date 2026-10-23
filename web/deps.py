"""Shared dependencies: Jinja2 templates, status colors, time formatter."""

from __future__ import annotations

import asyncio
import json as _json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Coroutine, Iterable
import zlib

from markupsafe import Markup

from fastapi.templating import Jinja2Templates

# Single definition lives next to AUDIT_STATUS/decode_status so page
# rendering and snapshot diffing can never disagree about a status code.
from src.scraper import AUDIT_STATUS, int_or_none  # noqa: F401  (re-exported for routes/templates)
from src.settings_cache import get_settings
from web.services import users as _users
from web.services.users import ROLE_LABELS

_HERE = Path(__file__).parent
_SHANGHAI_TZ = timezone(timedelta(hours=8))

templates = Jinja2Templates(directory=_HERE / "templates")

# Single shared pool for all blocking work (scans, relogins, report runs).
# One instance so lifespan shutdown can cancel queued work in one place.
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="worker")

# Keep strong references to fire-and-forget tasks so they aren't GC'd mid-run
_bg_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task

# Status colours per dataRegAuditStatus code (all 17 statuses) — the ONE
# source for the page pills, the dashboard distribution bar and the settings
# legend: status_css() below renders them into <head>, so no stylesheet carries
# a hand-copied table any more. Six families: blue=审核中, orange=需处理,
# violet=公示/发证, green=成功, red=驳回/失败, gray=撤回/终止/失效. Each family
# gives the pastel pill (light + dark) and the family name the distribution
# bar colours by; the bar's solid swatches live in ui.css as --sf-<family>,
# validated for colour-vision deficiency in the order green, blue, orange,
# violet, red (gray is the de-emphasis bucket and sits last).
def _family(name: str, bg: str, text: str, dark_bg: str, dark_text: str) -> dict[str, str]:
    return {"family": name, "bg": bg, "text": text, "dark_bg": dark_bg, "dark_text": dark_text}


_C_BLUE = _family("blue", "#E6F1FB", "#0C447C", "#1e3a5c", "#93c5fd")
_C_ORANGE = _family("orange", "#FAEEDA", "#854F0B", "#452d10", "#fbbf24")
_C_PURPLE = _family("violet", "#EEEDFE", "#3C3489", "#2e2760", "#c4b5fd")
_C_GREEN = _family("green", "#EAF3DE", "#27500A", "#1a3810", "#86efac")
_C_RED = _family("red", "#FCEBEB", "#791F1F", "#3f1010", "#f87171")
_C_GRAY = _family("gray", "#F1EFE8", "#444441", "#2a2f36", "#9ca3af")
STATUS_COLORS: dict[int, dict[str, str]] = {
    1:  _C_BLUE,    # 待审核
    2:  _C_ORANGE,  # 待补正
    3:  _C_PURPLE,  # 公示中
    4:  _C_ORANGE,  # 异议
    5:  _C_GRAY,    # 主动撤回
    6:  _C_RED,     # 不予登记
    7:  _C_GREEN,   # 登记成功
    8:  _C_GRAY,    # 终止登记
    9:  _C_GRAY,    # 登记撤销
    10: _C_GRAY,    # 未续展失效
    11: _C_GRAY,    # 视为撤回
    12: _C_BLUE,    # 待审核
    13: _C_GREEN,   # 续展登记
    14: _C_RED,     # 批量登记失败
    15: _C_GRAY,    # 撤回审查
    16: _C_GRAY,    # 撤回补正
    17: _C_PURPLE,  # 待发证
}
FAMILY_ORDER = ("green", "blue", "orange", "violet", "red", "gray")


def _build_status_css() -> str:
    light, dark = [], []
    for code, c in list(STATUS_COLORS.items()) + [("x", _C_GRAY)]:  # x = unknown code fallback
        light.append(f".s-pill-{code}{{background:{c['bg']};color:{c['text']}}}")
        dark.append(f"html.dark .s-pill-{code}{{background:{c['dark_bg']};color:{c['dark_text']}}}")
    return "".join(light) + "".join(dark)


_STATUS_CSS = Markup(_build_status_css())


def status_css() -> Markup:
    """The .s-pill-N rules for every page's <head> (light, then html.dark)."""
    return _STATUS_CSS


def status_distribution(snapshots: Iterable[dict]) -> dict[str, Any]:
    """Part-to-whole of the visible records by status, for the dashboard bar.

    Segments are ordered by colour family (FAMILY_ORDER) and then by code, so
    same-family statuses sit together and the adjacent-pair colour checks hold.
    """
    counts: dict[int, int] = {}
    for snap in snapshots:
        for code, n in (snap.get("status_counts") or {}).items():
            counts[code] = counts.get(code, 0) + int(n or 0)
    total = sum(counts.values())
    items = []
    for code, n in counts.items():
        if n <= 0:
            continue
        family = STATUS_COLORS.get(code, _C_GRAY)["family"]
        items.append({
            "code": code,
            "label": AUDIT_STATUS.get(code, f"状态{code}"),
            "count": n,
            "family": family,
            "pct": round(n * 100 / total) if total else 0,
        })
    items.sort(key=lambda it: (FAMILY_ORDER.index(it["family"]), str(it["code"]).zfill(3)))
    return {"total": total, "segments": items}


# Account-dot severity, shared by dashboard / api / view (was three copies).
# red = needs urgent action; amber = needs correction.
_DOT_RED_CODES = {6, 4, 8, 14}    # 不予登记 / 异议 / 终止登记 / 批量登记失败
_DOT_AMBER_CODES = {2, 16}        # 待补正 / 撤回补正


def account_dot(status_counts: dict[int, int]) -> str:
    if any(status_counts.get(c, 0) > 0 for c in _DOT_RED_CODES):
        return "red"
    if any(status_counts.get(c, 0) > 0 for c in _DOT_AMBER_CODES):
        return "amber"
    return "green"


def in_year(ms: int | None, year: int | None) -> bool:
    """True if the ms-timestamp falls in `year` (Asia/Shanghai); no filter when None."""
    if year is None:
        return True
    if not ms:
        return False
    return datetime.fromtimestamp(ms / 1000, tz=_SHANGHAI_TZ).year == year


def current_year() -> int:
    return datetime.now(_SHANGHAI_TZ).year


def _fmt_time(ts_str: str | None) -> str:
    """Format ISO timestamp as 'MM-DD HH:MM' in Asia/Shanghai (UTC+8)."""
    if not ts_str:
        return "—"
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(_SHANGHAI_TZ)
        return local.strftime("%m-%d %H:%M")
    except Exception:
        return ts_str[:16]


def _fmt_time_short(ts_str: str | None) -> str:
    """Format as 'HH:MM' only."""
    if not ts_str:
        return "—"
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(_SHANGHAI_TZ)
        return local.strftime("%H:%M")
    except Exception:
        return ts_str[11:16] if len(ts_str) >= 16 else "—"


def _fmt_datetime(ts_str: str | None) -> str:
    """Format an ISO timestamp as 'YYYY-MM-DD HH:MM' in Asia/Shanghai — for
    histories that span years, where 'MM-DD HH:MM' would be ambiguous."""
    if not ts_str:
        return "—"
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts_str[:16]


def status_class(code) -> str:
    """The .s-pill-<N> suffix for a raw status code ("7" and 7 alike); x = unknown."""
    c = int_or_none(code)
    return str(c) if c in AUDIT_STATUS else "x"


def record_url(username: str, reg_no: str) -> str:
    """The detail drawer for one record (web/routes/records.py)."""
    from urllib.parse import urlencode

    return "/partials/record?" + urlencode({"account": username or "", "reg_no": reg_no or ""})


def _tojson(v: object, indent: int = 2) -> str:
    return _json.dumps(v, ensure_ascii=False, indent=indent)


def zjip_base_url() -> str:
    """Platform base URL from config/settings.yaml (mtime-cached)."""
    return get_settings().get("base_url") or "https://www.zjip.org.cn"


def detail_url(reg_no: str) -> str:
    """Official per-record detail page on the zjip platform (login required)."""
    return f"{zjip_base_url()}/dregister/detail/{reg_no}"


def get_groups() -> dict[str, str]:
    """分组 key → 中文名，来自 config/settings.yaml（mtime 缓存，改名即时生效）。"""
    groups = get_settings().get("groups") or {}
    return {str(k): str(v) for k, v in groups.items()}


def group_label(key: str | None) -> str:
    if not key:
        return "未分组"
    return get_groups().get(key, key)


def owner_label(username: str | None, group: str = "") -> str:
    """负责人 as shown on the page; flags an owner who can no longer see the
    company (deleted, disabled, or not in the company's group any more)."""
    if not username:
        return ""
    rec = _users.get(username)
    if rec is None:
        return f"{username}（已删除）"
    name = rec["display_name"] or username
    if rec["disabled"]:
        return f"{name}（已停用）"
    if rec["role"] != "admin" and group not in rec["groups"]:
        return f"{name}（看不到该分组）"
    return name


def owner_choices(group: str) -> list[tuple[str, str]]:
    """Who may be made responsible for a company in `group`: enabled users
    who can see it (admins, or members of that group)."""
    found = _users.load()
    return [
        (name, rec["display_name"] or name)
        for name, rec in sorted(found.items())
        if not rec["disabled"] and (rec["role"] == "admin" or (group and group in rec["groups"]))
    ]


def view_scope(request) -> dict:
    """web.auth.view_scope for templates (the topbar's 我负责的 / 全部)."""
    # Lazy: web.auth reads .env and logs at import; deps must not pull that in.
    from web.auth import view_scope as _view_scope

    return _view_scope(request)


# Deadline pill modifiers (ui.css .pill-*; src/todo.urgency_of): overdue solid red,
# ≤3 days red, ≤7 amber, further out green, deadline unknown grey.
URGENCY_CLASSES: dict[str, str] = {
    "overdue": "pill-danger-strong",
    "red": "pill-danger",
    "amber": "pill-warn",
    "normal": "pill-good",
    "unknown": "pill-neutral",
}


def urgency_class(urgency: str | None) -> str:
    return URGENCY_CLASSES.get(urgency or "unknown", URGENCY_CLASSES["unknown"])


def due_label(days_left: int | None, deadline: str | None) -> str:
    """'截止 10-12 · 剩 16 天' / '今天到期' / '已逾期 3 天' / '截止日期未知'."""
    if not deadline:
        return "截止日期未知"
    when = deadline[5:] if len(deadline) >= 10 else deadline
    if days_left is None:
        return f"截止 {when}"
    if days_left < 0:
        return f"截止 {when} · 已逾期 {-days_left} 天"
    if days_left == 0:
        return f"截止 {when} · 今天到期"
    return f"截止 {when} · 剩 {days_left} 天"


def cert_label(days_left: int, valid_to: str) -> str:
    """'2027-03-01 到期 · 剩 120 天' / '已过期 3 天，未续展'."""
    if days_left < 0:
        return f"已过期 {-days_left} 天，未续展"
    return f"{valid_to} 到期 · 剩 {days_left} 天"


# The page templates and the mini program's JSON use the same words, so
# they live here rather than in a template.
NOTICE_KIND_LABELS: dict[str, str] = {
    "补正": "补正通知书", "公示": "公示通知书", "公告": "登记公告",
    "不予登记": "不予登记通知书", "异议": "异议通知书",
}

# Why the last scan of an account did not succeed (src/status_store status).
SCAN_KIND_LABELS: dict[str, str] = {
    "partial": "部分抓取", "login_failed": "登录失败", "captcha_required": "需验证码",
    "platform_error": "平台暂时不可用", "scrape_failed": "抓取失败",
    "shrink_refused": "数量骤减·未保存",
    "timeout": "巡检超时", "error": "巡检异常",
}


def scan_kind_label(status: str | None) -> str:
    return SCAN_KIND_LABELS.get(status or "", "巡检失败")


templates.env.globals["urgency_class"] = urgency_class
templates.env.globals["due_label"] = due_label
templates.env.globals["cert_label"] = cert_label
templates.env.globals["NOTICE_KIND_LABELS"] = NOTICE_KIND_LABELS
templates.env.globals["scan_kind_label"] = scan_kind_label
templates.env.filters["fmt_time"] = _fmt_time
templates.env.filters["fmt_time_short"] = _fmt_time_short
templates.env.filters["fmt_datetime"] = _fmt_datetime
templates.env.globals["record_url"] = record_url
templates.env.globals["status_class"] = status_class
templates.env.filters["tojson"] = _tojson
templates.env.filters["group_label"] = group_label
templates.env.filters["role_label"] = lambda role: ROLE_LABELS.get(role, role or "")
templates.env.globals["detail_url"] = detail_url
templates.env.globals["get_groups"] = get_groups
templates.env.globals["owner_label"] = owner_label
templates.env.globals["owner_choices"] = owner_choices
templates.env.globals["view_scope"] = view_scope


# ---------------------------------------------------------------------------
# Static assets are local (no CDN) and versioned by content: the ?v= hash
# changes whenever a file changes, so browsers may cache them for a year and a
# deploy still shows up at once (web.app sets the Cache-Control headers).
_STATIC_DIR = _HERE / "static"
_ASSET_FILES = ("ui.css", "ui.js", "icons.svg", "htmx.min.js", "htmx-sse.js")


def _asset_version() -> str:
    crc = 0
    for name in _ASSET_FILES:
        path = _STATIC_DIR / name
        if path.exists():
            crc = zlib.crc32(path.read_bytes(), crc)
    return f"{crc & 0xFFFFFFFF:08x}"


ASSET_VERSION = _asset_version()


def static_url(name: str) -> str:
    return f"/static/{name}?v={ASSET_VERSION}"


def icon(name: str, cls: str = "") -> Markup:
    """An inline <svg> showing symbol i-<name> of web/static/icons.svg (Tabler outline set).

    Stroke colour follows the surrounding text (currentColor); size via .ic-* or
    the parent's font-size. Names are template literals, never user input.
    """
    classes = f"ic {cls}" if cls else "ic"
    href = f"/static/icons.svg?v={ASSET_VERSION}#i-{name}"
    return Markup(f'<svg class="{classes}" aria-hidden="true"><use href="{href}" xlink:href="{href}"/></svg>')


templates.env.globals["icon"] = icon
templates.env.globals["static_url"] = static_url
templates.env.globals["status_css"] = status_css
