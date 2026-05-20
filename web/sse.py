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
from sse_starlette.sse import EventSourceResponse

_EVENTS_FILE = Path(__file__).parent.parent / "data" / "events.jsonl"

router = APIRouter()

_subscribers: set[asyncio.Queue[str]] = set()


async def broadcast(data: str) -> None:
    """Push a raw JSON string to all connected SSE clients."""
    for q in list(_subscribers):
        await q.put(data)


async def start_watcher() -> None:
    """Background task: tail events.jsonl and push new lines to subscribers."""
    last_size = _EVENTS_FILE.stat().st_size if _EVENTS_FILE.exists() else 0
    while True:
        await asyncio.sleep(2)
        if not _EVENTS_FILE.exists():
            continue
        try:
            size = _EVENTS_FILE.stat().st_size
        except OSError:
            continue
        if size <= last_size:
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


async def _stream(request: Request) -> AsyncGenerator[dict, None]:
    q: asyncio.Queue[str] = asyncio.Queue()
    _subscribers.add(q)
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                data = await asyncio.wait_for(q.get(), timeout=25)
                yield {"event": "update", "data": data}
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": ""}
    finally:
        _subscribers.discard(q)


@router.get("/sse")
async def sse_endpoint(request: Request) -> EventSourceResponse:
    return EventSourceResponse(_stream(request))
