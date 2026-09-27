"""Read session (token cache) files from data/sessions/."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SESSIONS_DIR = Path(__file__).parent.parent.parent / "data" / "sessions"


def _safe_name(account: str) -> str:
    safe = re.sub(r"[^\w@.-]", "_", account)
    h = hashlib.sha1(account.encode()).hexdigest()[:6]
    return f"{safe}_{h}"


def get_session(username: str) -> dict | None:
    """Return session dict for *username* or None if not cached."""
    path = _SESSIONS_DIR / f"{_safe_name(username)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def token_status(session: dict | None) -> dict:
    """Compute display status from a session dict.

    Returns dict with keys:
        state:     "valid" | "expiring" | "expired" | "none"
        label:     human-readable Chinese string
        css:       .pill-* modifier class for the badge (web/static/ui.css)
    """
    if session is None:
        return {"state": "none", "label": "未缓存", "css": "pill-neutral"}

    expires_str = session.get("expires_at", "")
    try:
        expires = datetime.fromisoformat(expires_str)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return {"state": "none", "label": "格式错误", "css": "pill-neutral"}

    now = datetime.now(timezone.utc)
    delta = expires - now
    hours_left = delta.total_seconds() / 3600

    if hours_left > 1:
        h = int(hours_left)
        return {
            "state": "valid",
            "label": f"有效 · {h}h 后到期",
            "css": "pill-good",
        }
    if hours_left > 0:
        m = int(delta.total_seconds() / 60)
        return {
            "state": "expiring",
            "label": f"即将到期 · {m}min",
            "css": "pill-warn",
        }
    ago_h = int(-hours_left)
    ago_label = f"{ago_h}h 前" if ago_h > 0 else "刚刚"
    return {
        "state": "expired",
        "label": f"已过期 · {ago_label}",
        "css": "pill-danger",
    }
