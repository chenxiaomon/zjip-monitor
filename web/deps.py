"""Shared dependencies: Jinja2 templates, status colors, time formatter."""

from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Coroutine

from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent
_SHANGHAI_TZ = timezone(timedelta(hours=8))

templates = Jinja2Templates(directory=_HERE / "templates")

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
    """Platform base URL from config/settings.yaml (cached at first use)."""
    global _zjip_base
    if _zjip_base is None:
        try:
            import yaml

            settings_file = _HERE.parent / "config" / "settings.yaml"
            data = yaml.safe_load(settings_file.read_text(encoding="utf-8")) or {}
            _zjip_base = data.get("base_url") or "https://www.zjip.org.cn"
        except Exception:
            _zjip_base = "https://www.zjip.org.cn"
    return _zjip_base


_zjip_base: str | None = None


def detail_url(reg_no: str) -> str:
    """Official per-record detail page on the zjip platform (login required)."""
    return f"{zjip_base_url()}/dregister/detail/{reg_no}"


def get_groups() -> dict[str, str]:
    """分组 key → 中文名，来自 config/settings.yaml（不缓存，改名即时生效）。"""
    try:
        import yaml

        settings_file = _HERE.parent / "config" / "settings.yaml"
        data = yaml.safe_load(settings_file.read_text(encoding="utf-8")) or {}
        groups = data.get("groups") or {}
        return {str(k): str(v) for k, v in groups.items()}
    except Exception:
        return {}


def group_label(key: str | None) -> str:
    if not key:
        return "未分组"
    return get_groups().get(key, key)


templates.env.filters["fmt_time"] = _fmt_time
templates.env.filters["fmt_time_short"] = _fmt_time_short
templates.env.filters["tojson"] = _tojson
templates.env.filters["group_label"] = group_label
templates.env.globals["detail_url"] = detail_url
templates.env.globals["get_groups"] = get_groups
