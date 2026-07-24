"""FastAPI entry point with session-cookie auth, SSE, and static files."""

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

from web import auth
from web.routes import accounts, api, attachments, changes, dashboard, login, notify, records, reports, settings, view
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


# 管理员专属页面（分组账号访问返回 403）
_ADMIN_ONLY_PREFIXES = ("/settings", "/notify", "/reports", "/run-once")


def _basic_admin_ok(request: Request) -> bool:
    """Basic Auth fallback with admin credentials (for scripts/curl)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
        user_ok = secrets.compare_digest(username.encode(), _WEB_USER.encode())
        pass_ok = secrets.compare_digest(password.encode(), _WEB_PASS.encode())
        return user_ok and pass_ok
    except Exception:
        return False


@app.middleware("http")
async def _auth_middleware(request: Request, call_next) -> Response:  # type: ignore[return]
    path = request.url.path
    # 公开路径：只读展示页、静态资源、登录/登出
    if (path == "/view" or path.startswith("/view/")
            or path.startswith("/static/")
            or path in ("/favicon.ico", "/login", "/logout")):
        return await call_next(request)
    # 小程序 API：X-API-Key 或 Basic 管理员
    if path.startswith("/api/"):
        api_key = request.headers.get("X-API-Key", "")
        key_ok = bool(_API_KEY) and secrets.compare_digest(api_key.encode(), _API_KEY.encode())
        if key_ok or _basic_admin_ok(request):
            return await call_next(request)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # 控制台：会话 cookie，Basic 管理员兜底
    user = auth.user_from_cookie(request.cookies.get(auth.COOKIE_NAME))
    if user is None and _basic_admin_ok(request):
        user = auth.admin_user()
    if user is None:
        if request.headers.get("HX-Request"):
            # 会话过期的 HTMX 轮询：让整页跳转登录页
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse(url="/login", status_code=302)
    request.state.user = user
    if user["role"] != "admin" and path.startswith(_ADMIN_ONLY_PREFIXES):
        return Response("无权限", status_code=403)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> RedirectResponse:
    return RedirectResponse(url="/static/favicon.svg")

app.include_router(login.router)
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
