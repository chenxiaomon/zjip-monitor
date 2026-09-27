"""FastAPI entry point with session-cookie auth, SSE, and static files."""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

# ---------------------------------------------------------------------------
# Process set-up — MUST stay above every `web.*` / `src.*` import below.
#
# Production runs `uvicorn web.app:app`, so importing this module is the only
# hook the command line offers, and two things have to happen first:
#
#   1. load_dotenv(): web.auth reads WEB_USER/WEB_PASS/SESSION_SECRET/
#      VIEW_TOKEN from os.environ at IMPORT time, and logging_setup reads the
#      ZJIP_LOG_* knobs. python-dotenv never overrides an already-set
#      variable, so a later call cannot repair a module that already read the
#      unset defaults.
#   2. setup_logging("web"): that same import-time code also LOGS — web.auth
#      warns "VIEW_TOKEN 未配置…" whenever the public read-only page is open
#      to everyone. With the route imports first, that warning was emitted
#      through loguru's DEFAULT sink, which is bare stderr (ANSI-coloured on a
#      terminal) and writes no file at all — so the line never reached
#      logs/web_*.log and was invisible to anyone reading the file log after
#      the fact. setup_logging() installs the real sinks, so it has to win the
#      race against the modules that log while being imported.
#
# Only the stdlib, dotenv and third-party libraries that configure nothing at
# import (fastapi, loguru) may sit above this block. The E402s below are
# deliberate, not leftovers.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
load_dotenv(_HERE.parent / ".env")

from src.logging_setup import setup_logging  # noqa: E402 — .env may set ZJIP_LOG_* first

setup_logging("web")

from web import auth, login_guard  # noqa: E402 — logs at import; needs the sinks above
from web.routes import (  # noqa: E402 — same reason
    accounts,
    api,
    attachments,
    changes,
    dashboard,
    deadlines,
    health,
    login,
    notify,
    records,
    reports,
    settings,
    view,
)
from web.sse import router as sse_router, start_watcher  # noqa: E402 — same reason

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
    # Graceful shutdown: signal in-flight scans to stop at their next
    # checkpoint and drop queued work, so worker threads (non-daemon) join
    # quickly and the port is freed for the next start — otherwise a running
    # Playwright scan can pin the old process for minutes ("port in use").
    from src import control
    from web import deps

    control.request_local_stop()
    deps.executor.shutdown(wait=False, cancel_futures=True)
    # The verify lane gets the same treatment; a worker wedged inside a
    # Playwright login stays bounded by the 120s login watchdog.
    from web.routes import accounts as _accounts_routes

    _accounts_routes._verify_executor.shutdown(wait=False, cancel_futures=True)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # a watcher that died earlier must not break shutdown
        logger.warning(f"SSE watcher ended with error: {exc!r}")


app = FastAPI(title="数智通监控", lifespan=_lifespan)


# 管理员专属页面（分组账号访问返回 403）
_ADMIN_ONLY_PREFIXES = ("/settings", "/notify", "/reports", "/run-once")

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Extra origins allowed to submit state-changing requests, e.g. when the
# console is reached through a second hostname. Comma-separated host[:port].
_ALLOWED_ORIGINS = {
    o.strip().lower() for o in os.getenv("WEB_ALLOWED_ORIGINS", "").split(",") if o.strip()
}


def _same_origin_ok(request: Request) -> bool:
    """Reject cross-site state changes.

    The session cookie is SameSite=Lax, which already blocks cross-site form
    POSTs — but the Basic-Auth admin fallback is not bound by SameSite at all:
    once a browser has cached those credentials (an operator using curl or the
    browser's own auth prompt), a form on any page could POST
    /accounts/{u}/delete and be authenticated.

    A missing Origin AND Referer is allowed: that is a script/curl, which is
    not a confused deputy. Browsers send Origin on every cross-site POST and
    on every htmx XHR.
    """
    origin = request.headers.get("origin") or ""
    if not origin:
        referer = request.headers.get("referer") or ""
        if not referer:
            return True
        parsed = urlparse(referer)
        origin_netloc = parsed.netloc
    else:
        origin_netloc = urlparse(origin).netloc

    origin_netloc = origin_netloc.lower()
    # Compare hosts only: nginx may terminate a different scheme than the app
    # sees, and a scheme mismatch is not what this check is about.
    return bool(origin_netloc) and (
        origin_netloc == (request.headers.get("host") or "").lower()
        or origin_netloc in _ALLOWED_ORIGINS
    )


def _basic_admin_ok(request: Request) -> bool:
    """Basic Auth fallback with admin credentials (for scripts/curl).

    Shares the login guard's counters with the form login: otherwise the rate
    limit could simply be walked around by brute-forcing this header instead.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    ip = login_guard.client_ip(request)
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        return False

    guard_key = f"basic:{username}"
    if login_guard.seconds_locked(ip, guard_key):
        logger.warning(f"Basic 认证被锁定中 ip={ip} user={login_guard.mask(username)}")
        return False

    user_ok = secrets.compare_digest(username.encode(), _WEB_USER.encode())
    pass_ok = secrets.compare_digest(password.encode(), _WEB_PASS.encode())
    if user_ok and pass_ok:
        login_guard.record_success(ip, guard_key)
        return True
    if login_guard.record_failure(ip, guard_key):
        logger.error(f"Basic 认证连续失败已锁定 ip={ip} user={login_guard.mask(username)}")
    return False


@app.middleware("http")
async def _auth_middleware(request: Request, call_next) -> Response:  # type: ignore[return]
    path = request.url.path
    # 跨站写请求一律拒绝（/api/* 用密钥而非 cookie，不受 CSRF 影响）
    if (
        request.method in _UNSAFE_METHODS
        and not path.startswith("/api/")
        and not _same_origin_ok(request)
    ):
        logger.warning(
            f"跨站写请求被拒绝 origin={request.headers.get('origin')!r} "
            f"referer={request.headers.get('referer')!r} host={request.headers.get('host')!r} "
            f"path={path}"
        )
        return Response("跨站请求被拒绝", status_code=403)
    # 公开路径：只读展示页、静态资源、登录/登出、健康检查
    # /health must be here or the deploy probe gets 302→/login, which reads as
    # "up" to anything that only checks for a non-5xx — see web/routes/health.py.
    if (path == "/view" or path.startswith("/view/")
            or path.startswith("/static/")
            or path in ("/favicon.ico", "/health", "/login", "/logout", "/robots.txt")):
        response = await call_next(request)
        if path.startswith("/static/") and response.status_code == 200:
            # Versioned URLs (?v=<content hash>, web.deps.static_url) may be cached
            # for good; bare ones an hour, so a deploy needs no hard refresh.
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if "v" in request.query_params
                else "public, max-age=3600"
            )
        return response
    # 小程序 API：X-API-Key 或 Basic 管理员
    if path.startswith("/api/"):
        api_key = request.headers.get("X-API-Key", "")
        key_ok = bool(_API_KEY) and secrets.compare_digest(api_key.encode(), _API_KEY.encode())
        if key_ok or _basic_admin_ok(request):
            # The single API_KEY carries admin scope today. Setting a user here
            # lets the /api handlers go through the same visible_accounts()
            # path as the console, so binding keys to groups later is a change
            # to this line, not to every handler.
            request.state.user = auth.admin_user()
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


@app.get("/robots.txt", include_in_schema=False)
async def robots() -> PlainTextResponse:
    """Nothing here should ever be indexed — /view lists client companies."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")

app.include_router(health.router)
app.include_router(login.router)
app.include_router(sse_router)
app.include_router(dashboard.router)
app.include_router(changes.router)
app.include_router(deadlines.router)
app.include_router(accounts.router)
app.include_router(records.router)
app.include_router(notify.router)
app.include_router(reports.router)
app.include_router(settings.router)
app.include_router(view.router)
app.include_router(api.router)
app.include_router(attachments.router)
