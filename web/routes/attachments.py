"""Serve downloaded 通知书/证书 files (Basic Auth protected by the app middleware)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from web.auth import allowed_company_keys, current_user
from web.services import audit_log
from web.services.attachments_svc import _safe_name, attachment_path, download_name

router = APIRouter()


@router.get("/attachments/{company}/{reg_no}/{filename}")
def get_attachment(request: Request, company: str, reg_no: str, filename: str):
    # Compare on the same key attachment_path() resolves with, not the raw
    # display name — otherwise two companies whose names differ only in
    # punctuation share a directory but not a permission check.
    allowed = allowed_company_keys(request)
    if allowed is not None and _safe_name(company) not in allowed:
        return HTMLResponse("附件不存在", status_code=404)
    path = attachment_path(company, reg_no, filename)
    if path is None:
        return HTMLResponse("附件不存在", status_code=404)
    name = download_name(company, reg_no, path.name)
    # One line per person per file per 10 minutes: a phone's PDF viewer or a
    # download manager fetches one file as several Range requests.
    who = (current_user(request) or {}).get("username", "")
    if audit_log.should_record_download(who, f"{_safe_name(company)}/{reg_no}/{path.name}"):
        audit_log.record_request(request, "data.attachment", company=company,
                                 target=f"{reg_no}/{path.name}")
    # 双格式 Content-Disposition:filename= 放原始 UTF-8 字节(经 latin-1 转义通过
    # 头部编码校验,线上字节仍是 UTF-8)供不认 filename* 的浏览器/WebView 兜底——
    # 否则它们退回用 URL 最后一段,即磁盘上的旧类型名;现代浏览器优先取 filename*
    utf8_fallback = name.encode("utf-8").decode("latin-1")
    return FileResponse(path, headers={
        "Content-Disposition": f'attachment; filename="{utf8_fallback}"; filename*=utf-8\'\'{quote(name)}',
        # 禁缓存:响应头里的下载名会随快照变化,不能让浏览器复用旧缓存头
        "Cache-Control": "no-store",
    })
