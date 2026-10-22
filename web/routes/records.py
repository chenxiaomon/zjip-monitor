"""Records page: full registration record table with filtering and keyword search."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

from src import todo
from src.scraper import AUDIT_STATUS, decode_status
from web.auth import allowed_usernames, shown_accounts, visible_accounts
from web.deps import STATUS_COLORS, int_or_none, templates
from web.services import audit_log
from web.services.attachments_svc import archived_attachments, list_attachments
from web.services.events import read_change_rows
from web.services.snapshot import get_latest_snapshot

router = APIRouter()

PAGE_SIZE = 50
_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _page_number(page: str | None) -> int:
    try:
        return max(1, int(page or 1))
    except ValueError:
        return 1


def _ms_to_date(ms: int | None) -> str:
    if not ms:
        return "—"
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) + timedelta(hours=8)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "—"


def _date_to_ms(date_str: str, end_of_day: bool = False) -> int | None:
    """Convert 'YYYY-MM-DD' (Asia/Shanghai) to millisecond timestamp."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        # treat as UTC+8
        dt_utc8 = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return int(dt_utc8.timestamp() * 1000)
    except Exception:
        return None


# NOTE on status/account query params: the HTMX filter form carries hidden
# inputs, so partial refreshes send `status=` / `account=` as EMPTY STRINGS —
# typing them `int | None` in the route would 422 every unfiltered search.
# `account` is coerced with `or None`; statuses are read by _statuses() below.

SORTS = ("", "apply_asc", "status", "company", "name")  # "" = newest application first

# 状态排序：先要处理的，再在途的，最后是已结束的。
_STATUS_ORDER = {
    2: 0, 4: 0, 16: 0,      # 待补正 / 异议 / 撤回补正
    1: 1, 12: 1,            # 待审核
    3: 2, 17: 2,            # 公示中 / 待发证
    7: 3, 13: 3,            # 登记成功 / 续展登记
    6: 4, 14: 4,            # 不予登记 / 批量登记失败
    5: 5, 8: 5, 9: 5, 10: 5, 11: 5, 15: 5,  # 撤回 / 终止 / 撤销 / 失效
}


def _statuses(request: Request) -> frozenset[int] | None:
    """Selected statuses: ?status=2,7 or repeated ?status=2&status=7; a single
    ?status=7 (the dashboard's links) still works. Blank values — the filter
    form's hidden input — mean no filter."""
    codes = {
        code
        for raw in request.query_params.getlist("status")
        for part in raw.split(",")
        if (code := int_or_none(part.strip())) is not None
    }
    return frozenset(codes) or None


def _sort(raw: str | None) -> str:
    return raw if raw in SORTS else ""


def _apply_ms(row: dict) -> int:
    return int_or_none(row["_raw"].get("dataRegApplyTime")) or 0


def _sort_rows(rows: list[dict], sort: str) -> None:
    """Stable, so every secondary order is "newest application first"."""
    rows.sort(key=_apply_ms, reverse=True)
    if sort == "apply_asc":
        rows.sort(key=_apply_ms)
    elif sort == "status":
        rows.sort(key=lambda r: _STATUS_ORDER.get(r["status_code"], 6))
    elif sort == "company":
        rows.sort(key=lambda r: str(r["company"] or ""))
    elif sort == "name":
        rows.sort(key=lambda r: str(r["name"] or ""))


def _build_records(
    accounts: list,
    account_filter: str | None,
    status_filter: int | frozenset[int] | set[int] | None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    q: str | None = None,
    sort: str = "",
) -> tuple[list[dict], list, list[dict]]:
    """Returns (rows, accounts_without_snapshot, fetch_warnings).

    status_filter: one code, a set of codes, or None for all."""
    if isinstance(status_filter, int):
        status_filter = frozenset({status_filter})
    rows: list[dict] = []
    missing_accounts: list = []
    fetch_warnings: list[dict] = []
    needle = (q or "").strip().lower()

    for acc in accounts:
        snap = get_latest_snapshot(acc.company)
        if not snap:
            # A company with no snapshot yet must not look identical to a
            # company with zero cases — surface it above the table.
            missing_accounts.append(acc)
            continue

        # Reconciliation banner data from the snapshot's fetch meta (additive
        # key — absent on old snapshots).
        fetch = snap.get("fetch") or {}
        if fetch:
            reported = fetch.get("reported_total") or 0
            fetched = fetch.get("fetched_count")
            if not fetch.get("complete", True):
                fetch_warnings.append({
                    "company": acc.company,
                    "kind": "partial",
                    "fetched": fetched,
                    "reported": reported,
                    "error": fetch.get("error"),
                })
            elif reported and fetched is not None and fetched != reported:
                fetch_warnings.append({
                    "company": acc.company,
                    "kind": "mismatch",
                    "fetched": fetched,
                    "reported": reported,
                    "error": None,
                })

        if account_filter and acc.username != account_filter:
            continue

        for r in snap.get("records", []):
            code = int_or_none(r.get("dataRegAuditStatus"))
            if status_filter and code not in status_filter:
                continue
            apply_ms = int_or_none(r.get("dataRegApplyTime"))
            if start_ms and apply_ms and apply_ms < start_ms:
                continue
            if end_ms and apply_ms and apply_ms > end_ms:
                continue
            reg_no = r.get("dataRegNo", "")
            name = r.get("dataRegName", "")
            if needle:
                haystack = " ".join([
                    str(reg_no), str(name), acc.company, acc.username,
                    str(r.get("dataRegApplyName") or ""),
                ]).lower()
                if needle not in haystack:
                    continue
            rows.append({
                "company": acc.company,
                "username": acc.username,
                "reg_no": reg_no,
                "attachments": list_attachments(acc.company, reg_no) if reg_no else [],
                "archived": archived_attachments(acc.company, reg_no) if reg_no else [],
                # 待补正 → the notice's deadline; 登记成功 → certificate expiry when near
                "due": todo.correction_for(acc, r) if reg_no else None,
                "renewal": todo.renewal_for(acc, r) if reg_no else None,
                "name": name,
                "industry": r.get("dataRegIndustry", ""),
                "data_type": r.get("dataRegDataType", ""),
                "apply_date": _ms_to_date(r.get("dataRegApplyTime")),
                "status_code": code,
                "status_label": decode_status(r.get("dataRegAuditStatus")),
                "_raw": r,
            })

    _sort_rows(rows, sort)
    return rows, missing_accounts, fetch_warnings


def _all_rows(request: Request, account: str | None, statuses: frozenset[int] | None,
              start_date: str | None, end_date: str | None, q: str | None, sort: str = ""):
    start_ms = _date_to_ms(start_date) if start_date else None
    end_ms = _date_to_ms(end_date, end_of_day=True) if end_date else None
    # 我负责的 when on; the record drawer below still checks visible_accounts.
    accounts_list = shown_accounts(request)
    rows, missing_accounts, fetch_warnings = _build_records(
        accounts_list, account, statuses, start_ms, end_ms, q, sort
    )
    return accounts_list, rows, missing_accounts, fetch_warnings


def _status_param(codes) -> str:
    return ",".join(str(c) for c in sorted(codes)) if codes else ""


def _ctx(
    request: Request,
    account: str | None,
    statuses: frozenset[int] | None,
    start_date: str | None,
    end_date: str | None,
    q: str | None = None,
    page: str | None = None,
    sort: str = "",
) -> dict:
    accounts_list, rows, missing_accounts, fetch_warnings = _all_rows(
        request, account, statuses, start_date, end_date, q, sort
    )
    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    current = min(_page_number(page), pages)
    filters = {
        "account": account or "",
        "status": _status_param(statuses),
        "q": (q or "").strip(),
        "start_date": start_date or "",
        "end_date": end_date or "",
        "sort": sort,  # last, so URLs without a sort read exactly as before
    }

    def records_url(partial: bool = False, csv: bool = False, **overrides) -> str:
        """This view's URL with some filters changed — every chip, the pager
        and the export link keep the OTHER filters, so clicking a status
        never drops the dates or the keyword."""
        params = {**filters, "page": current, **overrides}
        if csv:
            params.pop("page", None)
        query = urlencode({
            k: v for k, v in params.items()
            if v not in (None, "") and not (k == "page" and v == 1)
        }, safe=",")
        base = "/partials/records-table" if partial else ("/records.csv" if csv else "/records")
        return base + (f"?{query}" if query else "")

    def status_toggle(code: int) -> str:
        """The status parameter after clicking this status's chip."""
        return _status_param((statuses or frozenset()) ^ {code})

    def sort_header(column: str) -> dict:
        """Link and state of a sortable column header. 申请时间 flips between
        newest-first (the default) and oldest-first; the others switch that
        order on, and a second click goes back to the default."""
        if column == "apply":
            state = "desc" if sort == "" else ("asc" if sort == "apply_asc" else None)
            target = "apply_asc" if sort == "" else ""
        else:
            state = "asc" if sort == column else None
            target = "" if sort == column else column
        return {"url": records_url(sort=target, page=1), "state": state}

    return {
        "rows": rows[(current - 1) * PAGE_SIZE: current * PAGE_SIZE],
        "total": total,
        "page": current,
        "pages": pages,
        "records_url": records_url,
        "accounts_list": accounts_list,
        "missing_accounts": missing_accounts,
        "fetch_warnings": fetch_warnings,
        "filter_account": account,
        "filter_statuses": statuses or frozenset(),
        "status_param": _status_param(statuses),
        "status_toggle": status_toggle,
        "sort": sort,
        "sort_header": sort_header,
        "start_date": start_date or "",
        "end_date": end_date or "",
        "q": (q or "").strip(),
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
    }


@router.get("/records", response_class=HTMLResponse)
def records_page(
    request: Request,
    account: str | None = None,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    q: str | None = Query(None),
    page: str | None = Query(None),
    sort: str | None = Query(None),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "records.html",
        _ctx(request, account or None, _statuses(request), start_date, end_date, q, page, _sort(sort)),
    )


@router.get("/partials/records-table", response_class=HTMLResponse)
def records_table_partial(
    request: Request,
    account: str | None = None,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    q: str | None = Query(None),
    page: str | None = Query(None),
    sort: str | None = Query(None),
) -> HTMLResponse:
    ctx = _ctx(request, account or None, _statuses(request), start_date, end_date, q, page, _sort(sort))
    # The address bar follows the table, so a reload, a bookmark or a shared
    # link shows the same filters (replace, not push: typing is not history).
    return templates.TemplateResponse(
        request, "_partials/records_box.html", ctx,
        headers={"HX-Replace-Url": ctx["records_url"]()},
    )


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ms_to_datetime(ms) -> str:
    ms = int_or_none(ms)
    if not ms:
        return "—"
    try:
        return datetime.fromtimestamp(ms / 1000, tz=_SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "—"


@router.get("/partials/record", response_class=HTMLResponse)
def record_detail(request: Request, account: str | None = None, reg_no: str | None = None) -> Response:
    """The detail drawer: one record of one visible account. Anything the
    viewer may not see, or that no longer exists, is the same plain 404 —
    the response never echoes the parameters back."""
    reg_no = (reg_no or "").strip()
    acc = next((a for a in visible_accounts(request) if a.username == (account or "")), None)
    snap = get_latest_snapshot(acc.company) if acc and reg_no else None
    record = next(
        (r for r in (snap or {}).get("records", []) if str(r.get("dataRegNo") or "") == reg_no),
        None,
    )
    if record is None:
        return Response("记录不存在或无权查看", status_code=404, media_type="text/plain; charset=utf-8")
    code = int_or_none(record.get("dataRegAuditStatus"))
    ctx = {
        "acc": acc,
        "r": record,
        "reg_no": reg_no,
        "name": str(record.get("dataRegName") or ""),
        "status_code": code,
        "status_label": decode_status(record.get("dataRegAuditStatus")),
        "apply_date": _ms_to_date(int_or_none(record.get("dataRegApplyTime"))),
        "update_time": _ms_to_datetime(record.get("updateTime")),
        "due": todo.correction_for(acc, record),
        "cert": todo.renewal_for(acc, record, window=None),
        "notices": todo.notices_of(acc.company, reg_no),
        "attachments": list_attachments(acc.company, reg_no),
        "archived": archived_attachments(acc.company, reg_no),
        "history": read_change_rows(
            account=acc.username, accounts=allowed_usernames(request), since=_EPOCH, reg_no=reg_no,
        ),
        "snapshot_time": snap.get("snapshot_time"),
        "AUDIT_STATUS": AUDIT_STATUS,
    }
    return templates.TemplateResponse(request, "_partials/record_drawer.html", ctx)


_CSV_HEADER = ["公司", "账号", "登记编号", "数据名称", "行业", "申请时间", "状态", "补正截止", "证书到期", "附件数"]


@router.get("/records.csv")
def records_csv(
    request: Request,
    account: str | None = None,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    q: str | None = Query(None),
    sort: str | None = Query(None),
) -> Response:
    """The current filter's rows, all pages, in the page's order, as UTF-8 CSV
    with a BOM so Excel opens the Chinese text correctly. Same visibility
    rules as the page."""
    _, rows, _, _ = _all_rows(request, account or None, _statuses(request), start_date, end_date, q, _sort(sort))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_HEADER)
    for r in rows:
        writer.writerow([
            r["company"], r["username"], r["reg_no"], r["name"], r["industry"], r["apply_date"],
            r["status_label"],
            (r["due"].deadline or "") if r["due"] else "",
            r["renewal"].valid_to if r["renewal"] else "",
            len(r["attachments"]) + len(r["archived"]),
        ])
    filters = {"account": account, "statuses": _statuses(request), "start_date": start_date,
               "end_date": end_date, "q": q}
    audit_log.record_request(request, "data.records_csv", detail={
        **{k: v for k, v in filters.items() if v}, "rows": len(rows),
    })
    filename = f"records-{datetime.now(_SHANGHAI_TZ):%Y%m%d}.csv"
    return Response(
        "﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )
