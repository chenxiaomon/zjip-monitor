"""Failure counting and temporary lockout for the console login.

The only brake before this was `await asyncio.sleep(0.5)` on a wrong password.
That does not block the event loop, so a hundred concurrent attempts still ran
at full speed — the delay slowed a human down and nothing else. The console is
reachable over plain HTTP on a public IP, so a weak WEB_PASS was brute-forceable
at network speed.

State is per process and in memory: one uvicorn worker runs the console
(run_web.py), a restart clears the counters, and that is an acceptable trade
for having no new dependency and no shared store. Moving to several workers
would need a shared backend.

Two counters, both needed:
  * (ip, username) — the normal case, so one user's typo does not lock others;
  * ip alone — an attacker rotating usernames never trips the first counter.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

from fastapi import Request

MAX_FAILURES = int(os.getenv("WEB_LOGIN_MAX_FAILURES", "5"))
IP_MAX_FAILURES = int(os.getenv("WEB_LOGIN_IP_MAX_FAILURES", "20"))
WINDOW_SECONDS = int(os.getenv("WEB_LOGIN_WINDOW_SECONDS", "900"))
LOCK_SECONDS = int(os.getenv("WEB_LOGIN_LOCK_SECONDS", "900"))

# Bound the dicts so a rotating-username flood cannot grow them without limit.
_MAX_TRACKED = 5000

_lock = threading.Lock()
_failures: dict[tuple[str, str], deque[float]] = {}
_locked_until: dict[tuple[str, str], float] = {}
_ip_failures: dict[str, deque[float]] = {}
_ip_locked_until: dict[str, float] = {}


def _now() -> float:
    # monotonic: immune to clock steps, and trivially patchable in tests
    return time.monotonic()


def mask(identifier: str) -> str:
    """Mask a username for logs — the log should say who was targeted without
    handing a log reader a working half of the credential pair."""
    if not identifier:
        return "***"
    if len(identifier) <= 3:
        return identifier[0] + "***"
    return f"{identifier[:2]}{'*' * (len(identifier) - 3)}{identifier[-1]}"


def client_ip(request: Request) -> str:
    """Best available client address.

    Production nginx sets X-Real-IP (and not X-Forwarded-For), so X-Real-IP
    wins. Whatever X-Forwarded-For a client sends is passed through by that
    nginx untouched: preferring it let anyone pick their own address — a
    fresh one per attempt walks straight past the per-IP lockout, and the
    operation log would record whatever they typed. X-Forwarded-For is still
    read when X-Real-IP is absent (a different proxy), then the socket peer
    so a direct-to-uvicorn setup still works.
    """
    real = request.headers.get("x-real-ip", "")
    if real:
        return real.strip()
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def _prune(now: float) -> None:
    """Drop expired entries; called under the lock on every access."""
    cutoff = now - WINDOW_SECONDS
    for store in (_failures, _ip_failures):
        for key in [k for k, dq in store.items() if not dq or dq[-1] < cutoff]:
            del store[key]
    for store in (_locked_until, _ip_locked_until):  # type: ignore[assignment]
        for key in [k for k, until in store.items() if until <= now]:
            del store[key]
    if len(_failures) > _MAX_TRACKED:
        _failures.clear()
        _locked_until.clear()
    if len(_ip_failures) > _MAX_TRACKED:
        _ip_failures.clear()
        _ip_locked_until.clear()


def seconds_locked(ip: str, username: str) -> int:
    """Remaining lockout in seconds (0 = not locked)."""
    now = _now()
    with _lock:
        _prune(now)
        until = max(
            _locked_until.get((ip, username.lower()), 0.0),
            _ip_locked_until.get(ip, 0.0),
        )
    return max(0, int(until - now)) if until > now else 0


def record_failure(ip: str, username: str) -> bool:
    """Count one failed attempt. Returns True if THIS attempt caused a lockout."""
    now = _now()
    key = (ip, username.lower())
    with _lock:
        _prune(now)
        user_dq = _failures.setdefault(key, deque())
        user_dq.append(now)
        ip_dq = _ip_failures.setdefault(ip, deque())
        ip_dq.append(now)

        just_locked = False
        if len(user_dq) >= MAX_FAILURES and key not in _locked_until:
            _locked_until[key] = now + LOCK_SECONDS
            just_locked = True
        if len(ip_dq) >= IP_MAX_FAILURES and ip not in _ip_locked_until:
            _ip_locked_until[ip] = now + LOCK_SECONDS
            just_locked = True
    return just_locked


def record_success(ip: str, username: str) -> None:
    with _lock:
        _failures.pop((ip, username.lower()), None)
        _locked_until.pop((ip, username.lower()), None)
        _ip_failures.pop(ip, None)


def reset() -> None:
    """Clear all state (tests)."""
    with _lock:
        _failures.clear()
        _locked_until.clear()
        _ip_failures.clear()
        _ip_locked_until.clear()
