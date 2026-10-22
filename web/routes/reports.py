"""Reports page: list, generate, view and delete the HTML reports of src/reporter.py."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from loguru import logger

from src.reporter import generate_report_from_snapshots
from web.deps import executor as _executor, spawn, templates
from web.services import audit_log

router = APIRouter()

_REPORTS_DIR = Path(__file__).parent.parent.parent / "data" / "reports"
PAGE_SIZE = 20
# One generation at a time, process-wide. done/error are shown by the poll
# that observes them and cleared by the next page load.
_gen_status: dict[str, str] = {"status": "idle", "msg": ""}


def _report_path(filename: str) -> Path | None:
    """The report file for a name from the URL, or None for anything that is
    not a report in data/reports (traversal, other suffixes, missing)."""
    resolved = (_REPORTS_DIR / filename).resolve()
    if not resolved.is_relative_to(_REPORTS_DIR.resolve()) or resolved.suffix != ".html" or not resolved.is_file():
        return None
    return resolved


def _list_ctx(page: int = 1) -> dict:
    files = sorted(_REPORTS_DIR.glob("*.html"), key=lambda f: f.stat().st_mtime, reverse=True) if _REPORTS_DIR.exists() else []
    total = len(files)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    reports = []
    for f in files[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]:
        st = f.stat()
        dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc) + timedelta(hours=8)
        reports.append({
            "filename": f.name,
            "datetime": dt.strftime("%Y-%m-%d %H:%M"),
            "size": f"{st.st_size / 1024:.1f} KB",
        })
    return {"reports": reports, "total": total, "page": page, "pages": pages}


def _page(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except ValueError:
        return 1


async def _generate_report_async() -> None:
    # Renders from existing snapshots — no login, no fetching, no notifications,
    # no run lock. Completes in seconds even while a scan is running.
    _gen_status["status"] = "running"
    loop = asyncio.get_event_loop()
    try:
        path = await loop.run_in_executor(_executor, generate_report_from_snapshots)
        if path is None:
            _gen_status["status"] = "error"
            _gen_status["msg"] = "暂无快照数据，请先执行一次巡检"
        else:
            _gen_status["status"] = "done"
            _gen_status["msg"] = ""
    except Exception as exc:
        # The raw text can carry filesystem paths; the operator needs the log,
        # not a truncated traceback fragment in the browser.
        logger.exception(f"Report generation failed: {exc!r}")
        _gen_status["status"] = "error"
        _gen_status["msg"] = "报表生成失败，请查看服务器日志"
    finally:
        if _gen_status["status"] == "running":  # e.g. cancelled coroutine
            _gen_status["status"] = "error"
            _gen_status["msg"] = "任务被中断"


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, page: str | None = Query(None)) -> HTMLResponse:
    if _gen_status["status"] in ("done", "error"):
        # The outcome was already shown by the poll that saw it; a fresh page
        # starts clean instead of announcing an old result forever.
        _gen_status.update(status="idle", msg="")
    return templates.TemplateResponse(
        request, "reports.html", {**_list_ctx(_page(page)), "gen_status": dict(_gen_status)},
    )


@router.get("/reports/list", response_class=HTMLResponse)
def reports_list(request: Request, page: str | None = Query(None)) -> HTMLResponse:
    return templates.TemplateResponse(request, "_partials/reports_list.html", _list_ctx(_page(page)))


@router.get("/reports/view/{filename}")
def view_report(filename: str):
    path = _report_path(filename)
    if path is None:
        return HTMLResponse("报表不存在", status_code=404)
    return FileResponse(path, media_type="text/html")


@router.post("/reports/generate", response_class=HTMLResponse)
async def generate_report(request: Request) -> HTMLResponse:
    already = _gen_status.get("status") == "running"
    if not already:
        spawn(_generate_report_async())
    audit_log.record_request(request, "report.generate",
                             detail={"already_running": True} if already else None)
    return templates.TemplateResponse(
        request, "_partials/report_gen_status.html", {"gen_status": {"status": "running", "msg": ""}},
    )


@router.get("/reports/gen-status", response_class=HTMLResponse)
def gen_status(request: Request) -> HTMLResponse:
    """The status element only, always — so the poll target keeps existing and
    the button stays wired. Once the job has ended, the refreshed list rides
    along as an out-of-band swap (hx-swap-oob) into #reports-list."""
    ctx = {"gen_status": dict(_gen_status)}
    if _gen_status["status"] in ("done", "error"):
        ctx.update(_list_ctx(1), oob=True)
    return templates.TemplateResponse(request, "_partials/report_gen_status.html", ctx)


@router.post("/reports/delete/{filename}", response_class=HTMLResponse)
def delete_report(request: Request, filename: str, page: str | None = Query(None)) -> HTMLResponse:
    path = _report_path(filename)
    if path is None:
        return HTMLResponse("报表不存在", status_code=404)
    path.unlink(missing_ok=True)
    logger.info(f"报表已删除：{path.name}")
    audit_log.record_request(request, "report.delete", target=path.name)
    return templates.TemplateResponse(request, "_partials/reports_list.html", _list_ctx(_page(page)))
