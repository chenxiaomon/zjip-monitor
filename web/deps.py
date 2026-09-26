"""Shared dependencies: Jinja2 templates, status colors, time formatter."""

from __future__ import annotations

import asyncio
import json as _json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Coroutine

from fastapi.templating import Jinja2Templates

# Single definition lives next to AUDIT_STATUS/decode_status so page
# rendering and snapshot diffing can never disagree about a status code.
from src.scraper import int_or_none  # noqa: F401  (re-exported for routes/templates)
from src.settings_cache import get_settings

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

# Status pill colors per dataRegAuditStatus code (all 17 statuses).
# Palette groups: blue=审核中, orange=需处理, purple=公示/发证, green=成功,
# red=驳回/失败, gray=撤回/终止/失效.
_C_BLUE = {"bg": "#E6F1FB", "text": "#0C447C"}
_C_ORANGE = {"bg": "#FAEEDA", "text": "#854F0B"}
_C_PURPLE = {"bg": "#EEEDFE", "text": "#3C3489"}
_C_GREEN = {"bg": "#EAF3DE", "text": "#27500A"}
_C_RED = {"bg": "#FCEBEB", "text": "#791F1F"}
_C_GRAY = {"bg": "#F1EFE8", "text": "#444441"}
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


# Deadline pills (src/todo.urgency_of): overdue and ≤3 days red, ≤7 amber,
# further out green, deadline unknown grey.
URGENCY_CLASSES: dict[str, str] = {
    "overdue": "bg-red-100 dark:bg-red-950/60 text-red-700 dark:text-red-300",
    "red": "bg-red-50 dark:bg-red-950/40 text-red-600 dark:text-red-400",
    "amber": "bg-amber-50 dark:bg-amber-950/40 text-amber-700 dark:text-amber-400",
    "normal": "bg-green-50 dark:bg-green-950/40 text-green-700 dark:text-green-400",
    "unknown": "bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400",
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


templates.env.globals["urgency_class"] = urgency_class
templates.env.globals["due_label"] = due_label
templates.env.filters["fmt_time"] = _fmt_time
templates.env.filters["fmt_time_short"] = _fmt_time_short
templates.env.filters["tojson"] = _tojson
templates.env.filters["group_label"] = group_label
templates.env.globals["detail_url"] = detail_url
templates.env.globals["get_groups"] = get_groups
