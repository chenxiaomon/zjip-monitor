"""SSE broadcast and /sse endpoint.

The daemon writes to data/events.jsonl; this module tails that file and pushes
new lines to all connected browser clients via Server-Sent Events.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from web.auth import allowed_usernames

_EVENTS_FILE = Path(__file__).parent.parent / "data" / "events.jsonl"

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


async def start_watcher() -> None:
    """Background task: tail events.jsonl and push new lines to subscribers.

    Survives file deletion/rotation (re-reads from the start when the file
    shrinks) and any unexpected error (logs and keeps polling) — it must
    never die silently, or the UI stops updating until a restart.
    """
    try:
        last_size = _EVENTS_FILE.stat().st_size
    except OSError:
        last_size = 0
    while True:
        await asyncio.sleep(2)
        try:
            if not _EVENTS_FILE.exists():
                last_size = 0
                continue
            try:
                size = _EVENTS_FILE.stat().st_size
            except OSError:
                continue
            if size < last_size:
                last_size = 0  # file truncated or rotated — start over
            if size == last_size:
                continue
            try:
                with _EVENTS_FILE.open("r", encoding="utf-8") as f:
                    f.seek(last_size)
                    new_content = f.read()
                    last_size = f.tell()
            except OSError:
                continue
            for line in new_content.splitlines():
                line = line.strip()
                if line:
                    await broadcast(line)
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
                yield {"event": "ping", "data": ""}
    finally:
        _subscribers.discard(q)


@router.get("/sse")
async def sse_endpoint(request: Request) -> EventSourceResponse:
    return EventSourceResponse(_stream(request))
