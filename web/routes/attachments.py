"""Serve downloaded 通知书/证书 files (Basic Auth protected by the app middleware)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from web.auth import allowed_companies
from web.services.attachments_svc import attachment_path

router = APIRouter()


@router.get("/attachments/{company}/{reg_no}/{filename}")
def get_attachment(request: Request, company: str, reg_no: str, filename: str):
    allowed = allowed_companies(request)
    if allowed is not None and company not in allowed:
        return HTMLResponse("附件不存在", status_code=404)
    path = attachment_path(company, reg_no, filename)
    if path is None:
        return HTMLResponse("附件不存在", status_code=404)
    return FileResponse(path, filename=path.name)
