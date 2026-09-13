"""GET /health — the unauthenticated probe deploy.sh and ops read.

Deliberately outside the login gate in web/app.py's allow-list: a probe that
needs a session cookie cannot answer "is the service up?" from a shell script,
and 302-to-/login would look like success to anything checking only for a
non-5xx. The trade-off is paid for in src/health.py, which keeps the payload
to counts, timestamps and status codes — see its module docstring.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from loguru import logger

from src.health import collect_health

router = APIRouter()

# no-store: nginx, curl or any proxy in front must never replay a 200 that
# was captured before the restart this probe is checking.
# X-Robots-Tag: the box answers on a bare public IP; /robots.txt already
# disallows everything, but that only covers crawlers that fetch it first.
_HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}

_TRUTHY = {"1", "true", "yes", "on"}


@router.get("/health")
def health(strict: str | None = Query(default=None)) -> JSONResponse:
    """Health snapshot; 503 only with ?strict=1 (or true/yes/on) when degraded.

    Two modes because the callers want different things. `./deploy.sh status`
    and a human curl want to READ the JSON, so plain /health stays 200 even
    when degraded — "the web process answers at all" is then testable on its
    own. deploy.sh's post-restart loop wants a status code to branch on, and
    uses strict=1: 200 = healthy, 503 = up but degraded (it prints the JSON
    and warns rather than failing the deploy), anything else = not up yet.
    """
    try:
        payload = collect_health()
    except Exception as exc:  # noqa: BLE001 — a 500 here reads as "service down"
        # collect_health() isolates every collector, so reaching this means a
        # bug in the assembly itself. Still answer: deploy.sh treats a 5xx as
        # "service never came up" and would fail an otherwise good deploy.
        logger.exception("/health 采集整体失败（已返回降级响应）")
        payload = {
            "status": "degraded",
            "checks": [{
                "name": "health_collect",
                "ok": False,
                "detail": f"健康检查自身出错（{type(exc).__name__}），详见 logs/web_*.log",
            }],
        }

    strict_mode = (strict or "").strip().lower() in _TRUTHY
    degraded = payload.get("status") == "degraded"
    return JSONResponse(
        payload,
        status_code=503 if (strict_mode and degraded) else 200,
        headers=_HEADERS,
    )
