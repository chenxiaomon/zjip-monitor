"""The console's side of the operation log: who is acting, from where.

src/audit.py writes the lines; this module fills in the actor from the
request (request.state.user, set by the auth middleware) and the client IP,
and marks the request as audited so the middleware's safety net knows a
state-changing route did its bookkeeping.
"""

from __future__ import annotations

import threading
import time

from fastapi import Request

from src import audit
from web import login_guard


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
