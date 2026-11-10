"""Serve downloaded 通知书/证书 files (Basic Auth protected by the app middleware)."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from web.auth import allowed_company_keys, current_user
from web.deps import attachment_disposition
from web.services import audit_log
from web.services.attachments_svc import _safe_name, attachment_path, download_name

router = APIRouter()


def attachment_response(
    request: Request, company: str, reg_no: str, filename: str, not_found: Callable[[], Response],
) -> Response:
    """One attachment of a company the caller may see, or `not_found()`.
    The console's link and the mini program's /api/attachment both come
    through here, so both check, log and name the file the same way."""
    # A single path segment only. The console's {filename} cannot hold a "/",
    # but the mini program passes the name as a query parameter, and
    # attachment_path() only keeps the result inside data/attachments — a
    # "../../<other company>/…" name would stay inside and pass.
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        return not_found()
    # Compare on the same key attachment_path() resolves with, not the raw
    # display name — otherwise two companies whose names differ only in
    # punctuation share a directory but not a permission check.
    allowed = allowed_company_keys(request)
    if allowed is not None and _safe_name(company) not in allowed:
        return not_found()
    path = attachment_path(company, reg_no, filename)
    if path is None:
        return not_found()
    name = download_name(company, reg_no, path.name)
    # One line per person per file per 10 minutes: a phone's PDF viewer or a
    # download manager fetches one file as several Range requests.
    who = (current_user(request) or {}).get("username", "")
    if audit_log.should_record_download(who, f"{_safe_name(company)}/{reg_no}/{path.name}"):
        audit_log.record_request(request, "data.attachment", company=company,
                                 target=f"{reg_no}/{path.name}")
    # 双格式 Content-Disposition（见 attachment_disposition）：不认 filename* 的
    # 浏览器/WebView 否则会退回用 URL 最后一段，即磁盘上的旧类型名
    return FileResponse(path, headers={
        "Content-Disposition": attachment_disposition(name),
        # 禁缓存:响应头里的下载名会随快照变化,不能让浏览器复用旧缓存头
        "Cache-Control": "no-store",
    })


@router.get("/attachments/{company}/{reg_no}/{filename}")
def get_attachment(request: Request, company: str, reg_no: str, filename: str):
    return attachment_response(
        request, company, reg_no, filename, lambda: HTMLResponse("附件不存在", status_code=404),
    )
