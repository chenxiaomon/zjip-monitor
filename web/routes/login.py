"""Login / logout routes for the console session cookie."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.auth import COOKIE_NAME, SESSION_MAX_AGE, check_login, make_cookie
from web.deps import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    matched = check_login(username.strip(), password)
    if matched is None:
        await asyncio.sleep(0.5)  # 减缓暴力尝试
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "用户名或密码错误", "form_username": username.strip()},
            status_code=401,
        )
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
