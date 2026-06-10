"""FastAPI entry point with HTTP Basic Auth, SSE, and static files."""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from web.routes import accounts, api, changes, dashboard, notify, records, reports, settings, view
from web.sse import router as sse_router, start_watcher

_HERE = Path(__file__).parent
load_dotenv(_HERE.parent / ".env")

_WEB_USER = os.getenv("WEB_USER", "admin")
_WEB_PASS = os.getenv("WEB_PASS", "changeme")


@asynccontextmanager
async def _lifespan(app: FastAPI):  # type: ignore[type-arg]
    task = asyncio.create_task(start_watcher())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="数智通监控", lifespan=_lifespan)


@app.middleware("http")
async def _basic_auth(request: Request, call_next) -> Response:  # type: ignore[return]
    auth = request.headers.get("Authorization", "")
    authed = False
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            username, _, password = decoded.partition(":")
            user_ok = secrets.compare_digest(username.encode(), _WEB_USER.encode())
            pass_ok = secrets.compare_digest(password.encode(), _WEB_PASS.encode())
            authed = user_ok and pass_ok
        except Exception:
            pass
    if (request.url.path == "/view" or request.url.path.startswith("/view/")
            or request.url.path.startswith("/static/")
            or request.url.path in {"/api/status", "/api/records", "/favicon.ico", "/view/partial"}):
        return await call_next(request)
    if authed:
        return await call_next(request)
    return Response(
        "Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="shuzhitong"'},
    )


app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> RedirectResponse:
    return RedirectResponse(url="/static/favicon.svg")

app.include_router(sse_router)
app.include_router(dashboard.router)
app.include_router(changes.router)
app.include_router(accounts.router)
app.include_router(records.router)
app.include_router(notify.router)
app.include_router(reports.router)
app.include_router(settings.router)
app.include_router(view.router)
app.include_router(api.router)
