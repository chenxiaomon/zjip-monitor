"""SSE broadcast and /sse endpoint.

The daemon writes to data/events.jsonl; this module tails that file and pushes
new lines to all connected browser clients via Server-Sent Events. It also
watches data/control/state.json so a page learns within seconds that a scan
started or ended — including the daemon's scheduled ones, which no browser
request ever triggers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from web import auth
from web.auth import allowed_usernames

_DATA_DIR = Path(__file__).parent.parent / "data"
_EVENTS_FILE = _DATA_DIR / "events.jsonl"
_STATE_FILE = _DATA_DIR / "control" / "state.json"

router = APIRouter()

_subscribers: set[asyncio.Queue[str]] = set()

# Bounded so one stalled browser connection cannot grow memory forever;
# a full queue just drops events for that client (it re-syncs on refresh).
_QUEUE_MAX = 200


async def broadcast(data: str) -> None:
    """Push a raw JSON string to all connected SSE clients."""
    for q in list(_subscribers):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass


def _run_key() -> tuple | None:
    """What identifies a run *transition*: not the file's mtime — the heartbeat
    rewrites state.json for every account, and a dashboard rebuild per
    account would be 25 rebuilds per scan."""
    try:
        state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    return (state.get("run_id"), state.get("status"), state.get("finished_at"))


async def _tick(cursor: dict) -> None:
    """One poll: new event lines and run transitions → broadcast.

    `cursor` carries the position between polls: {"events_size", "run_key"}.
    """
    if not _EVENTS_FILE.exists():
        cursor["events_size"] = 0
    else:
        try:
            size = _EVENTS_FILE.stat().st_size
        except OSError:
            size = None
        if size is not None:
            if size < cursor["events_size"]:
                cursor["events_size"] = 0  # file truncated or rotated — start over
            if size != cursor["events_size"]:
                try:
                    with _EVENTS_FILE.open("r", encoding="utf-8") as f:
                        f.seek(cursor["events_size"])
                        new_content = f.read()
                        cursor["events_size"] = f.tell()
                except OSError:
                    new_content = ""
                for line in new_content.splitlines():
                    line = line.strip()
                    if line:
                        await broadcast(line)

    key = _run_key()
    if key != cursor.get("run_key"):
        cursor["run_key"] = key
        if key is not None:
            # No `account` field: every viewer may know a scan started/ended.
            await broadcast(json.dumps({"kind": "run", "status": key[1]}))


async def start_watcher() -> None:
    """Background task: poll every 2s, forever.

    Survives file deletion/rotation and any unexpected error (logs and keeps
    polling) — it must never die silently, or the UI stops updating until a
    restart.
    """
    try:
        events_size = _EVENTS_FILE.stat().st_size
    except OSError:
        events_size = 0
    cursor = {"events_size": events_size, "run_key": _run_key()}
    while True:
        await asyncio.sleep(2)
        try:
            await _tick(cursor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"SSE watcher error (retrying): {exc!r}")
            await asyncio.sleep(5)


def _visible_to(allowed: set[str] | None, data: str) -> bool:
    """分组账号只收到自己组内账号的事件；无 account 字段的控制消息全员可见。"""
    if allowed is None:
        return True
    try:
        event = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return False
    account = event.get("account")
    if account is None:
        return True
    return account in allowed


def _still_signed_in(request: Request) -> bool:
    """Re-check a cookie session mid-stream. The stream outlives the request
    that authenticated it, so without this a disabled user — or one whose
    password was reset — would keep receiving events until they closed the
    tab. Refreshes request.state.user so a groups change narrows or widens
    what the stream lets through. Basic-Auth callers have no cookie to
    re-check; their credential is the .env admin's."""
    user = getattr(request.state, "user", None)
    if user is None:
        return False
    if user.get("via") != "cookie":
        return True
    fresh = auth.user_from_cookie(request.cookies.get(auth.COOKIE_NAME))
    if fresh is None:
        return False
    request.state.user = fresh
    return True


async def _stream(request: Request) -> AsyncGenerator[dict, None]:
    allowed = allowed_usernames(request)
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAX)
    _subscribers.add(q)
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                data = await asyncio.wait_for(q.get(), timeout=25)
                if _visible_to(allowed, data):
                    yield {"event": "update", "data": data}
            except asyncio.TimeoutError:
                # The browser's EventSource reconnects on its own; that
                # request then meets the login redirect and stops.
                if not _still_signed_in(request):
                    break
                allowed = allowed_usernames(request)
                yield {"event": "ping", "data": ""}
    finally:
        _subscribers.discard(q)


@router.get("/sse")
async def sse_endpoint(request: Request) -> EventSourceResponse:
    return EventSourceResponse(_stream(request))
