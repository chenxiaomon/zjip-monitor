"""The console's side of the operation log: who is acting, from where —
and reading it back for the 操作日志 page.

src/audit.py writes the lines; this module fills in the actor from the
request (request.state.user, set by the auth middleware) and the client IP,
and marks the request as audited so the middleware's safety net knows a
state-changing route did its bookkeeping. The reader mirrors
web/services/events.py: each month file is parsed once per (mtime, size).
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import Request

from src import audit
from web import login_guard
from web.services.attachments_svc import _safe_name
from web.services.users import ROLE_LABELS

_SHANGHAI_TZ = timezone(timedelta(hours=8))


def record_request(
    request: Request,
    action: str,
    *,
    company: str = "",
    target: str = "",
    ok: bool = True,
    detail: dict | None = None,
    as_user: dict | None = None,
    actor: str | None = None,
) -> None:
    """Log `action` as done by whoever is signed in on `request`.

    The login routes run before anyone is signed in: `as_user` names the
    user who just logged in or out, `actor` a bare login name (already
    masked when it belongs to nobody).
    """
    user = as_user or getattr(request.state, "user", None) or {}
    audit.record(
        actor if actor is not None else user.get("username", ""),
        action,
        actor_name=user.get("display_name", ""),
        role=user.get("role", ""),
        via=user.get("via", ""),
        ip=login_guard.client_ip(request),
        company=company,
        target=target,
        ok=ok,
        detail=detail,
    )
    request.state.audited = True


def nothing_changed(request: Request) -> None:
    """A state-changing route that ended up changing nothing (a form sent
    back with a validation error): tell the middleware's safety net there
    is nothing to log, instead of it filing an http.unaudited line."""
    request.state.audited = True


# A download manager or a phone's PDF viewer fetches one file as several
# Range requests; one person opening one file is one line.
_DOWNLOAD_DEDUPE_SECONDS = 600
_DOWNLOAD_DEDUPE_MAX = 5000
_recent_downloads: dict[tuple[str, str], float] = {}
_downloads_lock = threading.Lock()


def should_record_download(username: str, path: str) -> bool:
    now = time.monotonic()
    key = (username, path)
    with _downloads_lock:
        last = _recent_downloads.get(key)
        if last is not None and now - last < _DOWNLOAD_DEDUPE_SECONDS:
            return False
        if len(_recent_downloads) >= _DOWNLOAD_DEDUPE_MAX:
            _recent_downloads.clear()
        _recent_downloads[key] = now
    return True


# ---------------------------------------------------------------------------
# Reading — the 操作日志 page and the 用户 page's 最近登录
# ---------------------------------------------------------------------------

ACTION_LABELS: dict[str, str] = {
    "auth.login": "登录",
    "auth.logout": "退出",
    "auth.login_failed": "登录失败",
    "auth.lockout": "登录被锁定",
    "self.password_change": "修改本人口令",
    "user.create": "新增用户",
    "user.update": "编辑用户",
    "user.password_reset": "重置用户口令",
    "user.disable": "停用用户",
    "user.enable": "启用用户",
    "user.revoke": "强制下线",
    "user.delete": "删除用户",
    "user.import": "导入 WEB_USERS",
    "account.add": "新增企业账号",
    "account.edit": "编辑企业账号",
    "account.group": "调整企业分组",
    "account.owner": "指定负责人",
    "account.reveal": "查看平台口令（去平台办理）",
    "account.delete": "删除企业账号",
    "account.verify": "测试登录",
    "account.relogin": "重登",
    "account.scan": "单账号巡检",
    "scan.start": "立即巡检全部",
    "scan.stop": "停止巡检",
    "settings.pause": "暂停 / 恢复定时巡检",
    "settings.schedule": "修改巡检计划",
    "settings.login": "修改登录参数",
    "settings.thresholds": "修改告警阈值",
    "notify.dingtalk": "修改钉钉通知",
    "notify.wechat": "修改企业微信通知",
    "notify.email": "修改邮件通知",
    "report.generate": "生成报表",
    "report.delete": "删除报表",
    "data.attachment": "下载附件",
    "data.records_csv": "导出 CSV",
    "archive.release": "归档释放（删除服务器文件）",
    "archive.unarchive": "取消归档（文件送回服务器）",
    "http.unaudited": "未登记的写操作",
}

CATEGORIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "auth": ("登录", ("auth.",)),
    "user": ("用户", ("user.", "self.")),
    "account": ("企业账号", ("account.",)),
    "scan": ("巡检", ("scan.",)),
    "settings": ("设置与通知", ("settings.", "notify.")),
    "report": ("报表", ("report.",)),
    "data": ("下载与导出", ("data.",)),
    "archive": ("归档", ("archive.",)),
    "other": ("其他", ("http.",)),
}

# Detail keys whose value is a [before, after] pair, by the actions that write them.
_CHANGE_ACTIONS = ("account.edit", "account.group", "account.owner", "user.update", "settings.", "notify.")
_FLAG_TEXT = {
    "password_changed": ("改了密码", ""),
    "stored_password": ("用已保存的密码", "用填写的密码"),
    "paused": ("已暂停", "已恢复"),
    "already_running": ("已在运行", ""),
    "verify_skipped": ("验证过于频繁，未验证", ""),
    "must_change": ("要求下次登录改口令", ""),
}
_KEY_LABELS = {
    "contact": "联系人", "group": "分组", "groups": "分组", "role": "角色", "display_name": "显示名",
    "owner": "负责人",
    "crons": "计划", "method": "登录方式", "token_max_age_hours": "Token 有效期",
    "headless": "无头模式", "enabled": "启用", "error": "原因", "reason": "原因",
    "rows": "行数", "reg_no": "登记号", "account": "账号", "statuses": "状态", "start_date": "起", "end_date": "止",
    "q": "关键字", "files": "文件数", "bytes": "字节", "users": "用户", "via": "方式",
    "minutes": "锁定分钟", "status": "状态码", "http_method": "请求",
}


def _fmt_value(key: str, value) -> str:
    from web.deps import group_label  # lazy: deps imports this package's users module

    if value is None or value == "" or value == []:
        return "（空）"
    if key == "role":
        return ROLE_LABELS.get(value, str(value))
    if key in ("group", "groups"):
        values = value if isinstance(value, list) else [value]
        return "、".join(group_label(v) for v in values) or "（空）"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return "、".join(str(v) for v in value)
    return str(value)


def describe(entry: dict) -> str:
    """The detail as one readable line for the page."""
    action = entry.get("action", "")
    detail = entry.get("detail") or {}
    if not isinstance(detail, dict):
        return str(detail)
    is_change = action.startswith(_CHANGE_ACTIONS)
    parts: list[str] = []
    for key, value in detail.items():
        if key in _FLAG_TEXT and isinstance(value, bool):
            text = _FLAG_TEXT[key][0 if value else 1]
            if text:
                parts.append(text)
            continue
        if key == "changed" and isinstance(value, list):
            parts.append("改了 " + "、".join(str(v) for v in value))
            continue
        label = _KEY_LABELS.get(key, key)
        if is_change and isinstance(value, list) and len(value) == 2:
            parts.append(f"{label} {_fmt_value(key, value[0])} → {_fmt_value(key, value[1])}")
        else:
            parts.append(f"{label} {_fmt_value(key, value)}")
    return "；".join(parts)


_parse_lock = threading.Lock()
_parsed: dict[str, tuple[tuple[int, int], list[dict]]] = {}


def _load(path) -> list[dict]:
    """One month file, parsed once per (mtime, size) — it only ever grows."""
    try:
        st = path.stat()
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    with _parse_lock:
        hit = _parsed.get(str(path))
        if hit and hit[0] == key:
            return hit[1]
    entries: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["ts"])
        except (ValueError, KeyError, TypeError):
            continue  # a torn or hand-edited line costs that line only
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        entry["_ts"] = ts
        entries.append(entry)
    with _parse_lock:
        _parsed[str(path)] = (key, entries)
    return entries


def _month_of(path) -> str:
    return path.stem.removeprefix("audit-")  # "YYYY-MM"


def query(
    *,
    since: datetime,
    until: datetime | None = None,
    actor: str = "",
    category: str = "",
    keyword: str = "",
    failures_only: bool = False,
) -> list[dict]:
    """Matching entries, newest first."""
    first_month = since.astimezone(_SHANGHAI_TZ).strftime("%Y-%m")
    last_month = (until or datetime.now(timezone.utc)).astimezone(_SHANGHAI_TZ).strftime("%Y-%m")
    prefixes = CATEGORIES.get(category, ("", ()))[1]
    needle = keyword.strip().lower()
    # Archive lines name the on-disk directory, where punctuation became "_".
    needle_dir = _safe_name(keyword.strip()).lower() if needle else ""
    out: list[dict] = []
    for path in audit.files():
        if not first_month <= _month_of(path) <= last_month:
            continue
        for entry in _load(path):
            ts = entry["_ts"]
            if ts < since or (until is not None and ts >= until):
                continue
            if actor and entry.get("actor") != actor:
                continue
            if prefixes and not entry.get("action", "").startswith(prefixes):
                continue
            if failures_only and entry.get("ok", True):
                continue
            if needle:
                hay = " ".join(str(entry.get(k, "")) for k in
                               ("actor", "actor_name", "company", "target", "ip", "action"))
                hay = f"{hay} {ACTION_LABELS.get(entry.get('action', ''), '')} {describe(entry)}".lower()
                if needle not in hay and needle_dir not in hay:
                    continue
            out.append(entry)
    out.sort(key=lambda e: e["_ts"], reverse=True)
    return out


def actors(since: datetime) -> list[str]:
    """Everyone who appears in the window, for the filter's drop-down."""
    return sorted({e.get("actor", "") for e in query(since=since) if e.get("actor")})


def last_logins(months: int = 6) -> dict[str, str]:
    """username → ISO time of their latest successful login, from the newest
    `months` files (the 用户 page's 最近登录)."""
    found: dict[str, datetime] = {}
    for path in reversed(audit.files()[-months:]):
        for entry in _load(path):
            if entry.get("action") != "auth.login":
                continue
            name = entry.get("actor", "")
            if name and (name not in found or entry["_ts"] > found[name]):
                found[name] = entry["_ts"]
    return {name: ts.isoformat() for name, ts in found.items()}
