"""Records page: full registration record table with filtering and keyword search.

The application-year chips (关注中 / 全部年份 / each year, web/services/
year_scope.py) narrow the table and its exports; the count line says how
many rows the chosen years leave out, so a search never comes up empty
without saying where the matches went. Links from 待办, the calendar and the
timeline ask for 全部年份: an old record named there must open.

The exports — CSV, and for one picked company the 进度台账 and the 交付包 —
cover what the filters leave, or only the records ticked in the table when
any are (ui.js keeps the ticks per company across pages and filters and adds
them as ?reg=). With nothing narrowing it, a company's 台账 and 交付包 are
the whole company, as they always were."""

from __future__ import annotations

import csv
import io
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.background import BackgroundTask

from src import diskstat, todo
from src.archive_policy import TERMINAL_STATUSES
from src.scraper import AUDIT_STATUS, decode_status
from web.auth import allowed_usernames, shown_accounts, visible_accounts
from src.focus import apply_year
from web.deps import STATUS_COLORS, attachment_disposition, int_or_none, templates
from web.services import audit_log, handover, ledger, year_scope
from web.services.attachments_svc import archived_attachments, attachments_dir, list_attachments
from web.services.events import read_change_rows
from web.services.snapshot import get_latest_snapshot
from web.services.year_scope import YearScope

router = APIRouter()

PAGE_SIZE = 50
_SHANGHAI_TZ = timezone(timedelta(hours=8))
# 交付包 is written here, served, then deleted; a download the client
# abandoned leaves its file behind, swept after an hour.
_HANDOVER_TMP = Path(__file__).parent.parent.parent / "data" / "tmp"
_HANDOVER_STALE_SECONDS = 3600


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


def _picked(request: Request) -> frozenset[str]:
    """登记编号 ticked on the records page: ?reg=A&reg=B (or ?reg=A,B)."""
    return frozenset(
        part.strip()
        for raw in request.query_params.getlist("reg")
        for part in raw.split(",")
        if part.strip()
    )


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


def record_matches(
    acc,
    r: dict,
    statuses: frozenset[int] | set[int] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    needle: str = "",
) -> bool:
    """One record against the records page's filters. The mini program's
    /api/records calls this too, so a search finds the same rows on both.
    `needle` is the keyword already stripped and lower-cased."""
    code = int_or_none(r.get("dataRegAuditStatus"))
    if statuses and code not in statuses:
        return False
    apply_ms = int_or_none(r.get("dataRegApplyTime"))
    if start_ms and apply_ms and apply_ms < start_ms:
        return False
    if end_ms and apply_ms and apply_ms > end_ms:
        return False
    if needle:
        haystack = " ".join([
            str(r.get("dataRegNo", "")), str(r.get("dataRegName", "")), acc.company, acc.username,
            str(r.get("dataRegApplyName") or ""),
        ]).lower()
        if needle not in haystack:
            return False
    return True


def _build_records(
    accounts: list,
    account_filter: str | None,
    status_filter: int | frozenset[int] | set[int] | None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    q: str | None = None,
    sort: str = "",
    *,
    scope: YearScope | None = None,
    tally: dict | None = None,
) -> tuple[list[dict], list, list[dict]]:
    """Returns (rows, accounts_without_snapshot, fetch_warnings).

    status_filter: one code, a set of codes, or None for all. `scope` keeps
    only the chosen application years (None: all). When `tally` is given it
    receives "years" (every application year in these accounts, newest
    first) and "outside" (rows that matched every other filter but not the
    years).

    Rows hold only what the snapshot says. The lookups that touch the disk —
    attachments, archive markers, 补正 deadline, certificate expiry — come
    from _fill_details, for the rows actually shown or exported."""
    if isinstance(status_filter, int):
        status_filter = frozenset({status_filter})
    rows: list[dict] = []
    missing_accounts: list = []
    fetch_warnings: list[dict] = []
    needle = (q or "").strip().lower()
    years: set[int] = set()
    outside = 0

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

        for r in snap.get("records", []):
            if (y := apply_year(r.get("dataRegApplyTime"))) is not None:
                years.add(y)

        if account_filter and acc.username != account_filter:
            continue

        for r in snap.get("records", []):
            if not record_matches(acc, r, status_filter, start_ms, end_ms, needle):
                continue
            if scope is not None and not scope.matches(r):
                outside += 1
                continue
            code = int_or_none(r.get("dataRegAuditStatus"))
            reg_no = r.get("dataRegNo", "")
            name = r.get("dataRegName", "")
            rows.append({
                "company": acc.company,
                "username": acc.username,
                "reg_no": reg_no,
                "name": name,
                "industry": r.get("dataRegIndustry", ""),
                "data_type": r.get("dataRegDataType", ""),
                "apply_date": _ms_to_date(r.get("dataRegApplyTime")),
                "status_code": code,
                "status_label": decode_status(r.get("dataRegAuditStatus")),
                "_raw": r,
                "_acc": acc,
            })

    _sort_rows(rows, sort)
    if tally is not None:
        tally["years"] = sorted(years, reverse=True)
        tally["outside"] = outside
    return rows, missing_accounts, fetch_warnings


def _fill_details(rows: list[dict]) -> list[dict]:
    """Add each row's attachments, archive markers, 补正 deadline and
    certificate expiry — a directory listing and the record's .sync.json —
    to just these rows. The page shows 50 of however many rows the filters
    leave, and filtering and sorting never look at these, so doing them for
    every row made each page view, chip and search keystroke cost as much as
    the whole table (0.6 s at 10,000 records)."""
    for row in rows:
        acc, reg_no, r = row["_acc"], row["reg_no"], row["_raw"]
        row["attachments"] = list_attachments(acc.company, reg_no) if reg_no else []
        row["archived"] = archived_attachments(acc.company, reg_no) if reg_no else []
        # 待补正 → the notice's deadline; 登记成功 → certificate expiry when near
        row["due"] = todo.correction_for(acc, r) if reg_no else None
        row["renewal"] = todo.renewal_for(acc, r) if reg_no else None
    return rows


def _all_rows(request: Request, account: str | None, statuses: frozenset[int] | None,
              start_date: str | None, end_date: str | None, q: str | None, sort: str = "",
              scope: YearScope | None = None, tally: dict | None = None):
    start_ms = _date_to_ms(start_date) if start_date else None
    end_ms = _date_to_ms(end_date, end_of_day=True) if end_date else None
    # 我负责的 when on; the record drawer below still checks visible_accounts.
    accounts_list = shown_accounts(request)
    rows, missing_accounts, fetch_warnings = _build_records(
        accounts_list, account, statuses, start_ms, end_ms, q, sort, scope=scope, tally=tally,
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
    year: str | None = None,
) -> dict:
    scope = year_scope.resolve(year)
    tally: dict = {}
    accounts_list, rows, missing_accounts, fetch_warnings = _all_rows(
        request, account, statuses, start_date, end_date, q, sort, scope, tally
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
        "sort": sort,  # after the older filters, so their URLs read exactly as before
        "year": scope.query or "",  # blank = the default scope
    }

    def records_url(partial: bool = False, csv: bool = False, **overrides) -> str:
        """This view's URL with some filters changed — every chip, the pager
        and the export link keep the OTHER filters, so clicking a status
        never drops the dates or the keyword."""
        params = {**filters, "page": current, **overrides}
        if "year" in overrides:
            params["year"] = year_scope.resolve(overrides["year"], current=scope.current).query
        if csv:
            params.pop("page", None)
        query = urlencode({
            k: v for k, v in params.items()
            if v not in (None, "") and not (k == "page" and v == 1)
        }, safe=",")
        base = "/partials/records-table" if partial else ("/records.csv" if csv else "/records")
        return base + (f"?{query}" if query else "")

    def export_url(path: str) -> str:
        """台账 / 交付包 link: the filters in effect, not the order or the page.
        The page's script adds the ticked records (?reg=) on top."""
        query = urlencode({
            k: filters[k] for k in ("account", "status", "q", "start_date", "end_date", "year") if filters[k]
        }, safe=",")
        return path + (f"?{query}" if query else "")

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
        "rows": _fill_details(rows[(current - 1) * PAGE_SIZE: current * PAGE_SIZE]),
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
        "year_scope": scope,
        "years": tally["years"],
        "outside": tally["outside"],
        "export_url": export_url,
        # One visible company picked: its exports, and the tick boxes for them
        "can_export": bool(account) and any(a.username == account for a in accounts_list),
        "handover": _handover_counts(accounts_list, account, rows),
        "REGISTERED_STATUSES": handover.REGISTERED_STATUSES,
        "TERMINAL_STATUSES": TERMINAL_STATUSES,
        "STATUS_COLORS": STATUS_COLORS,
        "AUDIT_STATUS": AUDIT_STATUS,
    }


def _handover_counts(accounts_list, account: str | None, rows: list[dict]) -> dict | None:
    """The 交付包 button of the company picked in the chips: {"registered",
    "open"} among the rows the filters leave — what the button downloads.
    None, no button, when no (visible) company is picked or it has nothing
    registered at all. A filter that leaves no registered row renders the
    button hidden, for the page's script to show once a ticked record is."""
    acc = next((a for a in accounts_list if a.username == (account or "")), None)
    snap = get_latest_snapshot(acc.company) if acc else None
    if not snap:
        return None
    if not handover.counts([r for r in snap.get("records") or [] if isinstance(r, dict)])["registered"]:
        return None
    return handover.counts([r["_raw"] for r in rows if r["username"] == acc.username])


@router.get("/records", response_class=HTMLResponse)
def records_page(
    request: Request,
    account: str | None = None,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    q: str | None = Query(None),
    page: str | None = Query(None),
    sort: str | None = Query(None),
    year: str | None = Query(None),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "records.html",
        _ctx(request, account or None, _statuses(request), start_date, end_date, q, page, _sort(sort), year),
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
    year: str | None = Query(None),
) -> HTMLResponse:
    ctx = _ctx(request, account or None, _statuses(request), start_date, end_date, q, page, _sort(sort), year)
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


def find_visible_record(
    request: Request, reg_no: str | None, *, username: str | None = None, company: str | None = None,
) -> tuple | None:
    """(account, snapshot, record) of one record the caller may see, or None.
    The console names the account by its platform login, the mini program by
    the company — the login never goes to the phone."""
    reg_no = (reg_no or "").strip()
    if not reg_no:
        return None
    acc = next(
        (a for a in visible_accounts(request)
         if (username is not None and a.username == username)
         or (company is not None and a.company == company)),
        None,
    )
    snap = get_latest_snapshot(acc.company) if acc else None
    record = next(
        (r for r in (snap or {}).get("records", []) if str(r.get("dataRegNo") or "") == reg_no),
        None,
    )
    return (acc, snap, record) if record is not None else None


def record_detail_data(request: Request, acc, snap: dict, record: dict) -> dict:
    """Everything the detail drawer shows about one record — and what the
    mini program's /api/record serializes, so the two never drift apart."""
    reg_no = str(record.get("dataRegNo") or "")
    return {
        "acc": acc,
        "r": record,
        "reg_no": reg_no,
        "name": str(record.get("dataRegName") or ""),
        "status_code": int_or_none(record.get("dataRegAuditStatus")),
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
    }


@router.get("/partials/record", response_class=HTMLResponse)
def record_detail(request: Request, account: str | None = None, reg_no: str | None = None) -> Response:
    """The detail drawer: one record of one visible account. Anything the
    viewer may not see, or that no longer exists, is the same plain 404 —
    the response never echoes the parameters back."""
    found = find_visible_record(request, reg_no, username=account or "")
    if found is None:
        return Response("记录不存在或无权查看", status_code=404, media_type="text/plain; charset=utf-8")
    ctx = {**record_detail_data(request, *found), "AUDIT_STATUS": AUDIT_STATUS}
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
    year: str | None = Query(None),
) -> Response:
    """The current filter's rows, all pages, in the page's order, as UTF-8 CSV
    with a BOM so Excel opens the Chinese text correctly. Same visibility
    rules and the same application years as the page."""
    scope = year_scope.resolve(year)
    picked = _picked(request)
    if picked:
        # 勾选了就只要勾选的，可以跨几家企业：上方选的企业、其它筛选都不再起作用
        # （勾选时可能是别的企业、别的筛选），能看到哪些企业照旧按权限。
        rows, _, _ = _build_records(visible_accounts(request), None, None, sort=_sort(sort))
        rows = [r for r in rows if r["reg_no"] in picked]
    else:
        _, rows, _, _ = _all_rows(request, account or None, _statuses(request), start_date, end_date, q,
                                  _sort(sort), scope)
    _fill_details(rows)  # only what is exported: five ticked rows cost five lookups
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
    if picked:
        companies = len({r["username"] for r in rows})
        filters = {"account": account, "picked": len(picked), "companies": companies if companies > 1 else None}
    else:
        filters = {"account": account, "statuses": _statuses(request), "start_date": start_date,
                   "end_date": end_date, "q": q, "year": None if scope.is_all else scope.value}
    audit_log.record_request(request, "data.records_csv", detail={
        **{k: v for k, v in filters.items() if v}, "rows": len(rows),
    })
    filename = f"records-{datetime.now(_SHANGHAI_TZ):%Y%m%d}.csv"
    return Response(
        "﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


def _picked_groups(request: Request, picked: frozenset[str]) -> list[tuple]:
    """The ticked records company by company: [(account, snapshot, records)]
    for each company the caller may see that holds one of them, in account
    order. Ticks are kept across the company chips, so the company picked
    there does not narrow them — visibility does."""
    groups = []
    for acc in visible_accounts(request):
        snap = get_latest_snapshot(acc.company)
        records = [r for r in (snap or {}).get("records") or []
                   if isinstance(r, dict) and str(r.get("dataRegNo") or "") in picked]
        if records:
            groups.append((acc, snap, records))
    return groups


def _chosen(request: Request, account: str | None) -> tuple[list[tuple], dict] | None:
    """What 导出台账 and 交付包 cover: ([(account, snapshot, records)], how).

    Ticked records (?reg=) win outright: they may have been ticked under other
    filters and in other companies (_picked_groups), so neither the filters
    nor the company picked in the chips apply. Otherwise that one company by
    the page's filters (_selection). None when the caller may see no such
    company, or it has no snapshot yet."""
    picked = _picked(request)
    if picked:
        groups = _picked_groups(request, picked)
        return groups, {"picked": sum(len(records) for _, _, records in groups)}
    acc = next((a for a in visible_accounts(request) if a.username == (account or "")), None)
    snap = get_latest_snapshot(acc.company) if acc else None
    if snap is None:
        return None
    records, how = _selection(request, acc, snap)
    return [(acc, snap, records)], how


def _selection(request: Request, acc, snap: dict) -> tuple[list[dict], dict]:
    """(records, how they were chosen) — one company by the page's filters:
    status, keyword, application dates and years, the same test as the
    table. "How" is {} when nothing narrows it: the whole company, exactly as
    before there was a choice. (Ticks are _chosen's.)"""
    records = [r for r in snap.get("records") or [] if isinstance(r, dict)]
    qp = request.query_params
    statuses = _statuses(request)
    start_date, end_date = (qp.get("start_date") or "").strip(), (qp.get("end_date") or "").strip()
    start_ms = _date_to_ms(start_date) if start_date else None
    end_ms = _date_to_ms(end_date, end_of_day=True) if end_date else None
    q = (qp.get("q") or "").strip()
    scope = year_scope.resolve(qp.get("year"))
    chosen = [r for r in records
              if record_matches(acc, r, statuses, start_ms, end_ms, q.lower()) and scope.matches(r)]
    how = {"statuses": sorted(statuses) if statuses else None, "q": q,
           "start_date": start_ms and start_date, "end_date": end_ms and end_date,
           "year": None if scope.is_all else scope.value}
    return chosen, {k: v for k, v in how.items() if v}


_NOTHING_CHOSEN = "没有符合当前筛选或勾选的记录（勾选的记录可能已不在最新数据里，请刷新页面重新勾选）"


def _ledger_of(request: Request, acc, snap: dict, records: list[dict], *, narrowed: bool) -> tuple[bytes, int]:
    """One company's 台账 over these records: (xlsx, how many changes it lists).
    A narrowed choice keeps only these records' changes; the whole company
    keeps every change, the vanished records' too."""
    changes = read_change_rows(account=acc.username, accounts=allowed_usernames(request), since=_EPOCH)
    if narrowed:
        changes = ledger.changes_of(records, changes)
    noticed = {
        reg_no: sum(1 for n in todo.notices_of(acc.company, reg_no) if n["kind"] == "补正")
        for r in records if (reg_no := str(r.get("dataRegNo") or ""))
    }
    body = ledger.build_ledger(acc.company, records, changes, corrections_noticed=noticed,
                               as_of=snap.get("snapshot_time"))
    return body, sum(1 for c in changes if c["type"] != "removed")


@router.get("/records/ledger.xlsx")
def records_ledger(request: Request, account: str | None = None) -> Response:
    """进度台账：登记记录页上选的一家企业，按当前筛选；或者勾选的记录（_chosen）。
    每份含这些记录和它们开始监控以来的状态变更；什么都没筛选时是这家企业的全部。
    勾选可以跨几家企业：那时每家一份 xlsx，打成一个 zip。看不到的企业、不存在的
    企业、还没有巡检数据的企业，回答都一样。"""
    chosen = _chosen(request, account)
    if chosen is None:
        return Response("没有这家企业的巡检数据", status_code=404, media_type="text/plain; charset=utf-8")
    groups, how = chosen
    if how and not any(records for _, _, records in groups):
        return Response(_NOTHING_CHOSEN, status_code=404, media_type="text/plain; charset=utf-8")
    today = datetime.now(_SHANGHAI_TZ).date()
    books = []
    for acc, snap, records in groups:
        body, changes = _ledger_of(request, acc, snap, records, narrowed=bool(how))
        audit_log.record_request(request, "data.records_ledger", company=acc.company, detail={
            "rows": len(records), "changes": changes,
            **({"picked": len(records)} if "picked" in how else how),
            **({"companies": len(groups)} if len(groups) > 1 else {}),
        })
        books.append((ledger.filename(acc.company, today), body))
    if len(books) == 1:
        name, body = books[0]
        media = ledger.MEDIA_TYPE
    else:
        name, body = ledger.batch_filename(groups[0][0].company, len(books), today), ledger.zip_of(books)
        media = ledger.ZIP_MEDIA_TYPE
    return Response(body, media_type=media, headers={
        "Content-Disposition": attachment_disposition(name), "Cache-Control": "no-store",
    })


def _sweep_handover_tmp(now: float | None = None) -> None:
    now = now or time.time()
    try:
        stale = [f for f in _HANDOVER_TMP.glob("handover-*.zip")
                 if now - f.stat().st_mtime > _HANDOVER_STALE_SECONDS]
    except OSError:
        return
    for f in stale:
        f.unlink(missing_ok=True)


@router.get("/records/handover.zip")
def records_handover(request: Request, account: str | None = None) -> Response:
    """交付包（zip）：登记成功、续展登记的证书清单 xlsx 加证书文件
    （web/services/handover.py），只从当前筛选出的、或勾选的记录里取，和导出
    台账一样（_chosen）；「尚有 N 条在审」也只数这些。勾选跨几家企业时一个 zip
    里每家一个文件夹，没有登记成功记录的那家不放。看不到的企业、不存在的企业、
    还没有巡检数据的企业，回答都一样。"""
    chosen = _chosen(request, account)
    if chosen is None:
        return Response("没有这家企业的巡检数据", status_code=404, media_type="text/plain; charset=utf-8")
    groups, how = chosen
    pkgs = [(acc, pkg) for acc, snap, records in groups
            if (pkg := handover.plan(acc.company, {**snap, "records": records}, attachments_dir())).rows]
    if not pkgs:
        why = "所选的记录里没有登记成功的" if how else "这家企业还没有登记成功的记录"
        return Response(why, status_code=404, media_type="text/plain; charset=utf-8")
    disk = diskstat.check_free()
    if disk.free_mb - sum(pkg.total_bytes for _, pkg in pkgs) / 1048576 < disk.min_free_mb:
        return Response(f"服务器磁盘空间不足（剩余 {disk.free_mb:.0f}MB），暂时打不了包",
                        status_code=503, media_type="text/plain; charset=utf-8")

    _HANDOVER_TMP.mkdir(parents=True, exist_ok=True)
    _sweep_handover_tmp()
    fd, tmp = tempfile.mkstemp(prefix="handover-", suffix=".zip", dir=_HANDOVER_TMP)
    os.close(fd)
    try:
        size = handover.write_batch_zip([pkg for _, pkg in pkgs], Path(tmp))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    for acc, pkg in pkgs:
        audit_log.record_request(request, "data.records_handover", company=acc.company, detail={
            "rows": len(pkg.rows), "certificates": len(pkg.present), "missing": len(pkg.missing),
            "open": pkg.open_count, "bytes": size,
            **({"picked": sum(len(records) for a, _, records in groups if a is acc)} if "picked" in how else how),
            **({"companies": len(pkgs)} if len(pkgs) > 1 else {}),
        })
    name = (pkgs[0][1].zip_name if len(pkgs) == 1 else
            handover.batch_zip_name([pkg for _, pkg in pkgs], datetime.now(_SHANGHAI_TZ).date()))
    return FileResponse(
        tmp, media_type=handover.MEDIA_TYPE,
        headers={"Content-Disposition": attachment_disposition(name), "Cache-Control": "no-store"},
        background=BackgroundTask(Path(tmp).unlink, missing_ok=True),
    )
