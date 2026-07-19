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
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from web.routes import accounts, api, attachments, changes, dashboard, notify, records, reports, settings, view
from web.sse import router as sse_router, start_watcher

_HERE = Path(__file__).parent
load_dotenv(_HERE.parent / ".env")

_WEB_USER = os.getenv("WEB_USER", "admin")
_WEB_PASS = os.getenv("WEB_PASS", "changeme")
_API_KEY = os.getenv("API_KEY", "")

if (not os.getenv("WEB_PASS") or _WEB_PASS == "changeme") and os.getenv("WEB_ALLOW_DEFAULT_PASS") != "1":
    raise RuntimeError(
        "WEB_PASS 未设置或仍为默认值 changeme，拒绝启动；"
        "请在 .env 中设置强口令，开发环境可设 WEB_ALLOW_DEFAULT_PASS=1 豁免"
    )

if not _API_KEY:
    logger.warning("API_KEY 未配置，/api/* 仅接受 Basic Auth 访问（小程序将无法直连）")


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
            or request.url.path == "/favicon.ico"):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        api_key = request.headers.get("X-API-Key", "")
        key_ok = bool(_API_KEY) and secrets.compare_digest(api_key.encode(), _API_KEY.encode())
        if key_ok or authed:
            return await call_next(request)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
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
app.include_router(attachments.router)
