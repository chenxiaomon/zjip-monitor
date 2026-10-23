"""Login / logout routes for the console session cookie."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from web import auth, login_guard
from web.auth import COOKIE_NAME
from web.deps import templates
from web.services import audit_log

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
    # A name that belongs to nobody is often a password typed into the wrong
    # box: the operation log keeps it masked, like the server log does.
    who = username if auth.is_known_user(username) else login_guard.mask(username)
    if reason == "disabled":
        # Only reachable with the right password, so it tells a guesser nothing.
        logger.warning(f"已停用的控制台用户尝试登录 ip={ip} user={login_guard.mask(username)}")
        audit_log.record_request(request, "auth.login_failed", actor=who, ok=False,
                                 detail={"reason": "已停用"})
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
        audit_log.record_request(request, "auth.login_failed", actor=who, ok=False)
        if just_locked:
            audit_log.record_request(request, "auth.lockout", actor=who, ok=False,
                                     detail={"minutes": login_guard.LOCK_SECONDS // 60})
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "用户名或密码错误", "form_username": username},
            status_code=401,
        )
    login_guard.record_success(ip, username)
    audit_log.record_request(request, "auth.login", as_user=auth.lookup(matched))
    response = RedirectResponse(url="/", status_code=303)
    auth.set_session_cookie(response, matched, secure=auth.is_https(request))
    return response


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    """POST only: as a GET, any third-party page (or a prefetching client)
    could log the operator out with a plain <img src="/logout">.

    /logout is a public path — the middleware sets no user — so the cookie
    is read here to know whose logout to log. It only clears this browser's
    cookie: other devices stay signed in (强制下线 on the 用户 page ends those)."""
    user = auth.user_from_cookie(request.cookies.get(COOKIE_NAME))
    if user is not None:
        audit_log.record_request(request, "auth.logout", as_user=user)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/", secure=auth.is_https(request))
    return response
