"""用户 page (admins) and 修改密码 (everyone with a file account).

Everything under /users is admin-only twice over: the middleware's
_ADMIN_ONLY_PREFIXES and a require_admin() in every handler. Handlers are
plain `def`: creating a user or setting a password runs PBKDF2, which must
stay off the event loop.

An administrator cannot disable, delete, sign out or demote themselves from
here — a slip would lock the last admin out until someone fetched the .env
credentials. Their own password goes through /me/password like everyone
else's, which asks for the current one.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from collections import Counter

from src.config_loader import ConfigError, load_accounts
from web import auth, login_guard
from web.auth import current_user, require_admin
from web.deps import get_groups, templates
from web.services import audit_log, users
from web.services import groups as group_svc
from web.services.groups import GroupError
from web.services.settings_svc import REFUSED_HINT, SettingsUnreadable
from web.services.users import UserError, UsersUnreadable

router = APIRouter()

_ROLE_ORDER = {role: i for i, role in enumerate(users.ROLES)}
_MODAL_ERROR_HEADERS = {"HX-Retarget": "#modal-container", "HX-Reswap": "innerHTML"}


def _me(request: Request) -> str:
    return (current_user(request) or {}).get("username", "")


def _row(request: Request, name: str, rec: dict) -> dict:
    return {**rec, "username": name, "is_me": name == _me(request)}


def _company_counts() -> Counter:
    """Companies per group key ("" = 未分组)."""
    try:
        return Counter(a.group for a in load_accounts())
    except ConfigError:
        return Counter()


def _visible_count(rec: dict, counts: Counter) -> int | None:
    """How many companies this user sees; None = all (admins)."""
    if rec["role"] == "admin":
        return None
    return sum(counts.get(g, 0) for g in set(rec["groups"]))


def _rows(request: Request) -> list[dict]:
    found = users.load()
    names = sorted(found, key=lambda n: (_ROLE_ORDER.get(found[n]["role"], 9), n))
    logins = audit_log.last_logins()
    counts = _company_counts()
    return [{**_row(request, name, found[name]), "last_login": logins.get(name),
             "visible": _visible_count(found[name], counts)} for name in names]


def _groups_ctx(error: str | None = None) -> dict:
    """The 分组 card: every group with its companies and members."""
    counts = _company_counts()
    found = users.load()
    rows = []
    for key, label in get_groups().items():
        members = [found[n]["display_name"] or n for n in sorted(found) if key in found[n]["groups"]]
        rows.append({"key": key, "label": label, "companies": counts.get(key, 0), "members": members})
    return {"group_rows": rows, "ungrouped": counts.get("", 0), "group_error": error}


def _groups_card(request: Request, error: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "_partials/groups_card.html", _groups_ctx(error))


def _group_choices(existing: list[str]) -> dict[str, str]:
    """settings.yaml's groups, plus any the user already has that settings
    no longer lists — so saving a user never silently drops a group."""
    choices = dict(get_groups())
    for key in existing:
        choices.setdefault(key, f"{key}（设置中已无此分组）")
    return choices


def _check_groups(submitted: list[str], existing: list[str]) -> list[str]:
    allowed = _group_choices(existing)
    cleaned = [g.strip() for g in submitted if g.strip()]
    unknown = [g for g in cleaned if g not in allowed]
    if unknown:
        raise UserError(f"分组 {unknown[0]} 不存在")
    return cleaned


def _form(request: Request, *, mode: str, error: str | None = None,
          user: dict | None = None, form: dict | None = None) -> HTMLResponse:
    existing = (user or {}).get("groups") or []
    return templates.TemplateResponse(
        request,
        "_partials/user_form.html",
        {"mode": mode, "error": error, "user": user, "form": form or {},
         "group_choices": _group_choices(existing), "role_labels": users.ROLE_LABELS},
        headers=_MODAL_ERROR_HEADERS if error else None,
    )


def _saved() -> HTMLResponse:
    """Close the modal and refresh the table."""
    return HTMLResponse("", headers={"HX-Trigger": "userSaved"})


def _row_response(request: Request, name: str) -> HTMLResponse:
    rec = users.get(name)
    if rec is None:
        return HTMLResponse("")
    row = {**_row(request, name, rec), "last_login": audit_log.last_logins().get(name),
           "visible": _visible_count(rec, _company_counts())}
    return templates.TemplateResponse(request, "_partials/user_row.html", {"row": row})


def _refuse(msg: str, status: int = 400) -> PlainTextResponse:
    # Short text/plain: ui.js shows it as the toast.
    return PlainTextResponse(msg, status_code=status)


# ---------------------------------------------------------------------------
# The 用户 page
# ---------------------------------------------------------------------------

@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request) -> HTMLResponse:
    require_admin(request)
    return templates.TemplateResponse(
        request,
        "users.html",
        {"rows": _rows(request), "load_error": users.load_error(),
         "env_admin": users.env_admin_name(), "role_labels": users.ROLE_LABELS,
         "last_logins": audit_log.last_logins(), **_groups_ctx()},
    )


@router.get("/users/rows", response_class=HTMLResponse)
def users_rows(request: Request) -> HTMLResponse:
    require_admin(request)
    return templates.TemplateResponse(
        request, "_partials/users_rows.html",
        {"rows": _rows(request), "env_admin": users.env_admin_name(),
         "last_logins": audit_log.last_logins()},
    )


@router.get("/users/new-form", response_class=HTMLResponse)
def new_user_form(request: Request) -> HTMLResponse:
    require_admin(request)
    return _form(request, mode="add")


@router.post("/users", response_class=HTMLResponse)
def create_user(
    request: Request,
    username: str = Form(""),
    display_name: str = Form(""),
    role: str = Form("viewer"),
    groups: list[str] = Form([]),
    password: str = Form(""),
    own_group: str = Form("off"),
) -> HTMLResponse:
    """New user. 「专属分组」(checked by default for non-admins) creates a
    group just for them, so a colleague starts out seeing nothing but what
    gets put in it — not the owner's companies, not another colleague's."""
    require_admin(request)
    form = {"username": username, "display_name": display_name, "role": role, "groups": groups,
            "own_group": own_group}
    new_group = None
    try:
        if own_group == "on" and role != "admin":
            # Validate first: a refused user must not leave an orphan group behind.
            name = users.validate_username(username)
            users.validate_password(password, name)
            label = f"{display_name.strip()[:24] or name}的企业"
            try:
                new_group = group_svc.create(label, key=group_svc.personal_key(name))
            except GroupError as exc:
                raise UserError(f"专属分组没建成：{exc}") from exc
            except SettingsUnreadable as exc:
                raise UserError(f"{exc}。{REFUSED_HINT}") from exc
            groups_chosen = [*groups, new_group]
        else:
            groups_chosen = groups
        name = users.create_user(
            username, display_name=display_name, role=role,
            groups=_check_groups(groups_chosen, []), password=password, by=_me(request),
        )
    except UserError as exc:
        if new_group:
            group_svc.delete(new_group)  # empty — created a moment ago
        else:
            audit_log.nothing_changed(request)
        return _form(request, mode="add", error=str(exc), form=form)
    except UsersUnreadable as exc:
        if new_group:
            group_svc.delete(new_group)
        audit_log.record_request(request, "user.create", target=username, ok=False,
                                 detail={"error": str(exc)})
        return _form(request, mode="add", error=f"{exc}，未保存", form=form)
    rec = users.get(name) or {}
    if new_group:
        audit_log.record_request(request, "group.create", target=new_group,
                                 detail={"label": label, "for_user": name})
    audit_log.record_request(request, "user.create", target=name,
                             detail={"role": rec.get("role"), "groups": rec.get("groups")})
    return _saved()


@router.get("/users/{username}/edit-form", response_class=HTMLResponse)
def edit_user_form(request: Request, username: str) -> HTMLResponse:
    require_admin(request)
    rec = users.get(username)
    if rec is None:
        return _refuse("用户不存在", 404)
    return _form(request, mode="edit", user=_row(request, users.normalize_username(username), rec))


@router.post("/users/{username}/edit", response_class=HTMLResponse)
def edit_user(
    request: Request,
    username: str,
    display_name: str = Form(""),
    role: str = Form("viewer"),
    groups: list[str] = Form([]),
) -> HTMLResponse:
    require_admin(request)
    name = users.normalize_username(username)
    rec = users.get(name)
    if rec is None:
        return _refuse("用户不存在", 404)
    user = _row(request, name, rec)
    form = {"display_name": display_name, "role": role, "groups": groups}
    try:
        if user["is_me"] and role != rec["role"]:
            raise UserError("不能修改自己的角色")
        diff = users.update_user(
            name, display_name=display_name, role=role,
            groups=_check_groups(groups, rec["groups"]),
        )
    except UserError as exc:
        audit_log.nothing_changed(request)
        return _form(request, mode="edit", error=str(exc), user=user, form=form)
    except UsersUnreadable as exc:
        audit_log.record_request(request, "user.update", target=name, ok=False,
                                 detail={"error": str(exc)})
        return _form(request, mode="edit", error=f"{exc}，未保存", user=user, form=form)
    if diff:
        audit_log.record_request(request, "user.update", target=name, detail=diff)
    else:
        audit_log.nothing_changed(request)
    return _saved()


@router.get("/users/{username}/password-form", response_class=HTMLResponse)
def password_form(request: Request, username: str) -> HTMLResponse:
    require_admin(request)
    rec = users.get(username)
    if rec is None:
        return _refuse("用户不存在", 404)
    return templates.TemplateResponse(
        request, "_partials/user_password_form.html",
        {"user": _row(request, users.normalize_username(username), rec), "error": None},
    )


@router.post("/users/{username}/password", response_class=HTMLResponse)
def reset_password(
    request: Request,
    username: str,
    password: str = Form(""),
    password2: str = Form(""),
    must_change: str = Form("off"),
) -> HTMLResponse:
    """Reset someone else's password; every session they have ends."""
    require_admin(request)
    name = users.normalize_username(username)
    rec = users.get(name)
    if rec is None:
        return _refuse("用户不存在", 404)
    user = _row(request, name, rec)

    def again(error: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_partials/user_password_form.html", {"user": user, "error": error},
            headers=_MODAL_ERROR_HEADERS,
        )

    if user["is_me"]:
        audit_log.nothing_changed(request)
        return again("改自己的口令请用左下角的「修改密码」")
    if password != password2:
        audit_log.nothing_changed(request)
        return again("两次输入的口令不一致")
    try:
        users.set_password(name, password, must_change=must_change == "on")
    except UserError as exc:
        audit_log.nothing_changed(request)
        return again(str(exc))
    except UsersUnreadable as exc:
        audit_log.record_request(request, "user.password_reset", target=name, ok=False,
                                 detail={"error": str(exc)})
        return again(f"{exc}，未保存")
    audit_log.record_request(request, "user.password_reset", target=name,
                             detail={"must_change": must_change == "on"})
    return _saved()


def _simple_change(request: Request, username: str, action: str, change) -> HTMLResponse:
    require_admin(request)
    name = users.normalize_username(username)
    if users.get(name) is None:
        return _refuse("用户不存在", 404)
    if name == _me(request):
        return _refuse("不能对自己执行此操作")
    try:
        result = change(name)
    except UsersUnreadable as exc:
        audit_log.record_request(request, action, target=name, ok=False, detail={"error": str(exc)})
        return _refuse(f"{exc}，未保存", 409)
    detail = {"role": result["role"], "groups": result["groups"]} if action == "user.delete" else None
    audit_log.record_request(request, action, target=name, detail=detail)
    return _row_response(request, name)


@router.post("/users/{username}/disable", response_class=HTMLResponse)
def disable_user(request: Request, username: str) -> HTMLResponse:
    return _simple_change(request, username, "user.disable", lambda n: users.set_disabled(n, True))


@router.post("/users/{username}/enable", response_class=HTMLResponse)
def enable_user(request: Request, username: str) -> HTMLResponse:
    return _simple_change(request, username, "user.enable", lambda n: users.set_disabled(n, False))


@router.post("/users/{username}/revoke", response_class=HTMLResponse)
def revoke_user(request: Request, username: str) -> HTMLResponse:
    return _simple_change(request, username, "user.revoke", users.revoke_sessions)


@router.post("/users/{username}/delete", response_class=HTMLResponse)
def delete_user(request: Request, username: str) -> HTMLResponse:
    return _simple_change(request, username, "user.delete", users.delete_user)


# ---------------------------------------------------------------------------
# 分组
# ---------------------------------------------------------------------------

def _group_change(request: Request, action: str, change, *, target: str = "",
                  detail: dict | None = None) -> HTMLResponse:
    require_admin(request)
    try:
        result = change()
    except GroupError as exc:
        audit_log.nothing_changed(request)
        return _groups_card(request, str(exc))
    except SettingsUnreadable as exc:
        audit_log.record_request(request, action, target=target, ok=False, detail={"error": str(exc)})
        return _groups_card(request, f"{exc}。{REFUSED_HINT}")
    audit_log.record_request(request, action, target=target or result, detail=detail)
    return _groups_card(request)


@router.post("/users/groups", response_class=HTMLResponse)
def create_group(request: Request, label: str = Form("")) -> HTMLResponse:
    return _group_change(request, "group.create", lambda: group_svc.create(label),
                         detail={"label": label.strip()})


@router.post("/users/groups/{key}/rename", response_class=HTMLResponse)
def rename_group(request: Request, key: str, label: str = Form("")) -> HTMLResponse:
    old = get_groups().get(key, "")
    return _group_change(request, "group.rename", lambda: group_svc.rename(key, label), target=key,
                         detail={"label": [old, label.strip()]})


@router.post("/users/groups/{key}/delete", response_class=HTMLResponse)
def delete_group(request: Request, key: str) -> HTMLResponse:
    return _group_change(request, "group.delete", lambda: group_svc.delete(key), target=key,
                         detail={"label": get_groups().get(key, "")})


# ---------------------------------------------------------------------------
# 修改密码 — one's own
# ---------------------------------------------------------------------------

def _me_page(request: Request, *, error: str | None = None, saved: bool = False,
             status_code: int = 200) -> HTMLResponse:
    user = current_user(request) or {}
    return templates.TemplateResponse(
        request, "me_password.html",
        {"error": error, "saved": saved, "is_file_user": user.get("source") == "file",
         "min_len": users.MIN_PASSWORD_LEN},
        status_code=status_code,
    )


@router.get("/me/password", response_class=HTMLResponse)
def me_password_page(request: Request) -> HTMLResponse:
    return _me_page(request)


@router.post("/me/password", response_class=HTMLResponse)
def me_password_change(
    request: Request,
    current: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
) -> HTMLResponse:
    """Needs the current password: a session left open on someone else's
    screen must not be enough to take the account over. Wrong guesses count
    against the same lockout as the login form."""
    user = current_user(request) or {}
    if user.get("source") != "file":
        audit_log.nothing_changed(request)
        return _me_page(request, status_code=400)
    name = user["username"]
    ip = login_guard.client_ip(request)
    if login_guard.seconds_locked(ip, name):
        audit_log.nothing_changed(request)
        return _me_page(request, error="失败次数过多，请稍后再试", status_code=429)
    rec = users.get(name)
    if rec is None or not users.verify_password(current, rec["password_hash"]):
        if login_guard.record_failure(ip, name):
            audit_log.record_request(request, "auth.lockout", target=name)
        audit_log.record_request(request, "self.password_change", target=name, ok=False,
                                 detail={"error": "当前口令不对"})
        return _me_page(request, error="当前口令不对")
    if password != password2:
        audit_log.nothing_changed(request)
        return _me_page(request, error="两次输入的新口令不一致")
    if password == current:
        audit_log.nothing_changed(request)
        return _me_page(request, error="新口令不能与当前口令相同")
    try:
        users.set_password(name, password, must_change=False)
    except UserError as exc:
        audit_log.nothing_changed(request)
        return _me_page(request, error=str(exc))
    except UsersUnreadable as exc:
        audit_log.record_request(request, "self.password_change", target=name, ok=False,
                                 detail={"error": str(exc)})
        return _me_page(request, error=f"{exc}，未保存")
    login_guard.record_success(ip, name)
    audit_log.record_request(request, "self.password_change", target=name)
    # The new key ended every session, this one included — sign it back in.
    request.state.user = dict(user, must_change_password=False)
    response = _me_page(request, saved=True)
    auth.set_session_cookie(response, name, secure=auth.is_https(request))
    return response
