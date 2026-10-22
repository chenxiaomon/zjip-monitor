"""Login / logout routes for the console session cookie."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from web import auth, login_guard
from web.auth import COOKIE_NAME, SESSION_MAX_AGE, make_cookie
from web.deps import templates

router = APIRouter()

# Extra damping for a human at the keyboard; the actual rate limit is
# login_guard. Module-level so tests can drop it to zero.
FAILED_LOGIN_DELAY_SECONDS = 0.5


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    username = username.strip()
    ip = login_guard.client_ip(request)

    wait = login_guard.seconds_locked(ip, username)
    if wait:
        logger.warning(
            f"控制台登录被锁定中 ip={ip} user={login_guard.mask(username)} 剩余 {wait}s"
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": f"失败次数过多，请 {wait // 60 + 1} 分钟后再试",
                "form_username": username,
            },
            status_code=429,
            headers={"Retry-After": str(wait)},
        )

    # PBKDF2 takes a few hundred ms on the production box — never on the loop.
    matched, reason = await asyncio.to_thread(auth.authenticate, username, password)
    if reason == "disabled":
        # Only reachable with the right password, so it tells a guesser nothing.
        logger.warning(f"已停用的控制台用户尝试登录 ip={ip} user={login_guard.mask(username)}")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "账号已停用，请联系管理员", "form_username": username},
            status_code=403,
        )
    if matched is None:
        just_locked = login_guard.record_failure(ip, username)
        await asyncio.sleep(FAILED_LOGIN_DELAY_SECONDS)
        log = logger.error if just_locked else logger.warning
        log(
            f"控制台登录失败 ip={ip} user={login_guard.mask(username)}"
            + ("（已锁定）" if just_locked else "")
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "用户名或密码错误", "form_username": username},
            status_code=401,
        )
    login_guard.record_success(ip, username)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        make_cookie(matched),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    """POST only: as a GET, any third-party page (or a prefetching client)
    could log the operator out with a plain <img src="/logout">."""
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
