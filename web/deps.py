"""Shared dependencies: Jinja2 templates, status colors, time formatter."""

from __future__ import annotations

import json as _json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent

templates = Jinja2Templates(directory=_HERE / "templates")

# Status pill colors per dataRegAuditStatus code
STATUS_COLORS: dict[int, dict[str, str]] = {
    1:  {"bg": "#E6F1FB", "text": "#0C447C"},
    2:  {"bg": "#FAEEDA", "text": "#854F0B"},
    3:  {"bg": "#EEEDFE", "text": "#3C3489"},
    5:  {"bg": "#F1EFE8", "text": "#444441"},
    6:  {"bg": "#FCEBEB", "text": "#791F1F"},
    7:  {"bg": "#EAF3DE", "text": "#27500A"},
    11: {"bg": "#F1EFE8", "text": "#444441"},
}


def _fmt_time(ts_str: str | None) -> str:
    """Format ISO timestamp as 'MM-DD HH:MM' in Asia/Shanghai (UTC+8)."""
    if not ts_str:
        return "—"
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt + timedelta(hours=8)
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
        local = dt + timedelta(hours=8)
        return local.strftime("%H:%M")
    except Exception:
        return ts_str[11:16] if len(ts_str) >= 16 else "—"


def _tojson(v: object, indent: int = 2) -> str:
    return _json.dumps(v, ensure_ascii=False, indent=indent)


templates.env.filters["fmt_time"] = _fmt_time
templates.env.filters["fmt_time_short"] = _fmt_time_short
templates.env.filters["tojson"] = _tojson
