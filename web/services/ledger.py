"""进度台账：一家企业的登记明细与状态变更记录，导出为 xlsx。

版式照同事手工整理的《数据知识产权登记进度台账》，两张表：

  「登记明细」     总体进度（各状态的数量）+ 每条申请一行，按申请日期排
  「状态变更记录」 变化概况（按变化类型计数）+ 每次变化一行，按时间倒序

数据都是系统里现成的：最新快照（登记明细）和巡检变更日志
（read_change_rows，监控开始以来的全部）。登记记录页上筛选或勾选了一部分时，
两张表都只含这部分（changes_of 挑出它们的变更行）。

汇总格写成公式，同时写进算好的结果：在 Excel / WPS 里手动改了某条的状态，
数量会跟着变；在微信、手机上只预览不重算时，看到的也不是空格。明细行里的
补正次数、最近变化直接写数值——补正次数要合并变更日志和补正通知书两处来源，
公式表达不了。

「记录消失」不进台账：那是抓取异常的提示，不是登记进度。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import xlsxwriter
from xlsxwriter.utility import xl_rowcol_to_cell

from src.scraper import decode_status, int_or_none

_SHANGHAI = timezone(timedelta(hours=8))

SHEET_RECORDS = "登记明细"
SHEET_CHANGES = "状态变更记录"
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# 总体进度的固定四行（顺序、底色照原台账）；其它状态有记录时才追加一行。
_STATUS_ROWS = (
    ("登记成功", "green", "已完成登记，取得登记证书"),
    ("公示中", "blue", "审核已通过，公示期满无异议即登记"),
    ("待审核", "yellow", "已提交，等待登记机构审核"),
    ("待补正", "orange", "需按登记机构意见补充或修改材料，提交后重新审核"),
)
_OTHER_STATUS_NOTES = {
    "待发证": "公示期满，等待发放登记证书",
    "异议": "公示期内有人提出异议，等待登记机构处理",
    "续展登记": "登记证书已续展",
    "不予登记": "登记机构决定不予登记",
    "主动撤回": "申请人已撤回登记申请",
    "视为撤回": "逾期未补正等原因，申请视为撤回",
    "终止登记": "登记程序已终止",
    "登记撤销": "已取得的登记被撤销",
    "未续展失效": "证书到期未续展，登记已失效",
}
# 「已通过审核」汇总哪几种状态（表里有的才算进去）
_PASSED = ("公示中", "登记成功", "待发证", "续展登记")

# 变化概况的固定五行；新增申请和其它变化（如「公示中→待发证」）有才追加。
_CHANGE_ROWS = (
    ("进入公示", "blue", "审核通过，进入公示期"),
    ("登记成功", "green", "公示期满无异议，完成登记"),
    ("需补正", "orange", "登记机构要求补充或修改材料，状态转为“待补正”"),
    ("补正已提交", "yellow", "补正材料已提交，状态回到“待审核”"),
    ("审核环节更新", "gray", "平台记录的审核过程变动，状态仍为“待审核”"),
)
NEW_APPLICATION = "新增申请"
_NEW_APPLICATION_NOTE = "平台上新出现的登记申请"

# 颜色照原台账：字色、底色
_TONES = {
    "green": ("#006100", "#C6EFCE"),
    "blue": ("#1F4E78", "#DDEBF7"),
    "yellow": ("#7F6000", "#FFF2CC"),
    "orange": ("#C55A11", "#FCE4D6"),
    "gray": ("#595959", "#EDEDED"),
    "passed": ("#375623", "#E2EFDA"),
}
_STATUS_TONE = {label: tone for label, tone, _ in _STATUS_ROWS}
_NAVY = "#1F4E78"
_TOTAL_FILL = "#F2F2F2"
_ZEBRA_FILL = "#F2F6FB"
_BASE = {"font_name": "宋体", "font_size": 11, "valign": "vcenter"}
_BORDER = {"border": 1, "border_color": "#BFBFBF"}
_COUNT = "0;-0;-"  # 0 显示成「-」

_RECORD_COLUMNS = (  # 表头，列宽
    ("序号", 25.13), ("公司", 12.75), ("登记编号", 20), ("数据名称", 58), ("业务类别", 17),
    ("申请日期", 12), ("状态", 10), ("补正次数", 10), ("最近变更时间", 17), ("最近变化", 14),
    ("备注", 24),
)
_CHANGE_COLUMNS = (
    ("序号", 14), ("变更时间", 17), ("公司", 12), ("登记编号", 20), ("数据名称", 54),
    ("变更前", 10), ("变更后", 10), ("变化说明", 14),
)
_FORMULA_ROWS = 1000  # 汇总公式至少覆盖这么多行，手动往下加的记录也算进去


def describe_change(before: str, after: str, *, added: bool = False) -> str:
    """变化说明，规则与原台账的公式一致；新出现的申请是「新增申请」。
    待审核有两个状态码（1、12），两者之间的切换就是「审核环节更新」。"""
    if added:
        return NEW_APPLICATION
    if before == "待审核" and after == "待审核":
        return "审核环节更新"
    if after == "待补正":
        return "需补正"
    if before == "待补正" and after == "待审核":
        return "补正已提交"
    if after == "公示中":
        return "进入公示"
    if after == "登记成功":
        return "登记成功"
    return f"{before}→{after}"


def safe_file_part(name: str) -> str:
    """A company name as part of a file name: characters no file system takes become _."""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip() or "企业"


def filename(company: str, on: date) -> str:
    """数据知识产权登记进度台账_<公司>_<YYYYMMDD>.xlsx；文件名里不能有的字符换成 _。"""
    return f"数据知识产权登记进度台账_{safe_file_part(company)}_{on:%Y%m%d}.xlsx"


# --- data ----------------------------------------------------------------------

@dataclass
class _Change:
    at: datetime | None  # 北京时间，不带时区（Excel 没有时区）
    key: str             # 与登记明细对应的记录标识
    reg_no: str
    name: str
    before: str
    after: str
    note: str


def _beijing(value) -> datetime | None:
    """ISO 时间戳或毫秒数 → 不带时区的北京时间。"""
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            ms = int_or_none(value)
            if not ms:
                return None
            dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return dt.astimezone(_SHANGHAI).replace(tzinfo=None)


def _record_keys(rec: dict) -> list[str]:
    """一条记录在变更日志里可能用的标识：登记编号，还没编号时是 id:<平台行号>。"""
    keys = []
    no = str(rec.get("dataRegNo") or "").strip()
    if no:
        keys.append(no)
    rid = str(rec.get("id") or "").strip()
    if rid:
        keys.append(f"id:{rid}")
    return keys


def _change_key(row: dict) -> str:
    no = str(row.get("reg_no") or "").strip()
    if no and no != "(无编号)":
        return no
    return "name:" + str(row.get("name") or "")


def changes_of(records: list[dict], change_rows: list[dict]) -> list[dict]:
    """只留这些记录的变更行——导出的是登记记录页筛选或勾选的一部分时用。
    变更日志里一条记录可能记的是登记编号、还没编号时的 id:<平台行号>，
    两样都没有时只剩名称。"""
    keys = {k for rec in records for k in _record_keys(rec)}
    names = {str(rec.get("dataRegName") or "") for rec in records}
    out = []
    for row in change_rows:
        key = _change_key(row)
        if key in keys or (key.startswith("name:") and key[5:] in names):
            out.append(row)
    return out


def _label(code, stored) -> str:
    return decode_status(code) if int_or_none(code) is not None else str(stored or "")


def _changes(change_rows: list[dict]) -> list[_Change]:
    """read_change_rows 的行（新的在前）→ 台账的变更行，去掉「记录消失」。"""
    out = []
    for row in change_rows:
        if row.get("type") not in ("changed", "added"):
            continue
        added = row["type"] == "added"
        before = "" if added else _label(row.get("old_status_code"), row.get("old_status"))
        after = _label(row.get("new_status_code"), row.get("new_status"))
        out.append(_Change(
            at=_beijing(row.get("timestamp")),
            key=_change_key(row),
            reg_no=str(row.get("reg_no") or ""),
            name=str(row.get("name") or ""),
            before=before,
            after=after,
            note=describe_change(before, after, added=added),
        ))
    return out


def correction_count(status_label: str, changes: list[_Change], noticed: int = 0) -> int:
    """补正次数：变更日志里转入或转出「待补正」的次数，和收到的补正通知书份数，
    取大的——监控开始之前的补正日志里没有，通知书可能有；现在就在待补正，
    至少算一次。"""
    entered = sum(1 for c in changes if c.after == "待补正")
    left = sum(1 for c in changes if c.before == "待补正")
    n = max(entered, left, noticed)
    if status_label == "待补正":
        n = max(n, 1)
    return n


# --- workbook ------------------------------------------------------------------

class _Formats:
    def __init__(self, wb):
        self._wb = wb
        self._cache: dict = {}

    def __call__(self, **props):
        key = tuple(sorted(props.items()))
        if key not in self._cache:
            self._cache[key] = self._wb.add_format({**_BASE, **props})
        return self._cache[key]

    def tone(self, tone: str, *, bold: bool = True, **props):
        font, fill = _TONES[tone]
        return self(**{"bold": bold, "font_color": font, "bg_color": fill, "align": "center",
                       **_BORDER, **props})


def _setup_sheet(ws, columns, tab_color: str, header_row: int, last_row: int) -> None:
    for i, (_, width) in enumerate(columns):
        ws.set_column(i, i, width)
    ws.set_default_row(19.5)
    ws.set_row(header_row - 1, 39)  # 大标题
    ws.set_row(header_row, 24)
    ws.freeze_panes(header_row + 1, 0)
    ws.autofilter(header_row, 0, max(last_row, header_row), len(columns) - 1)
    ws.set_tab_color(tab_color)
    ws.set_landscape()
    ws.set_paper(9)  # A4
    ws.fit_to_pages(1, 0)
    ws.center_horizontally()
    ws.set_margins(left=0.4, right=0.4, top=0.6, bottom=0.6)
    ws.set_footer("&C第 &P 页，共 &N 页")
    ws.repeat_rows(header_row)
    ws.print_area(0, 0, max(last_row, header_row), len(columns) - 1)


def _row_colors(wb, ws, first: int, end: int, key_col: str, status_cols: tuple[int, int],
                ncols: int, tones: dict[str, str] | None = None) -> None:
    """状态格按状态着色；其它格隔行浅蓝底（照原台账的条件格式）。"""
    lo, hi = status_cols
    for label, tone in (tones or _STATUS_TONE).items():
        font, fill = _TONES[tone]
        ws.conditional_format(first, lo, end, hi, {
            "type": "cell", "criteria": "==", "value": f'"{label}"',
            "format": wb.add_format({"bold": True, "font_color": font, "bg_color": fill}),
        })
    zebra = wb.add_format({"bg_color": _ZEBRA_FILL})
    rule = {"type": "formula", "criteria": f'=AND(${key_col}{first + 1}<>"",MOD(ROW(),2)=0)',
            "format": zebra}
    if lo > 0:
        ws.conditional_format(first, 0, end, lo - 1, rule)
    if hi < ncols - 1:
        ws.conditional_format(first, hi + 1, end, ncols - 1, rule)


def _rng(col: str, first: int, end: int) -> str:
    """$G$11:$G$1010（参数是从 0 起的行号）。"""
    return f"${col}${first + 1}:${col}${end + 1}"


def _write_records(wb, f: _Formats, company: str, records: list[dict], changes: list[_Change],
                   noticed: dict[str, int], as_of: datetime | None) -> None:
    ws = wb.add_worksheet(SHEET_RECORDS)

    by_key: dict[str, list[_Change]] = {}
    for c in changes:
        by_key.setdefault(c.key, []).append(c)

    rows = []
    for rec in records:
        keys = _record_keys(rec) + ["name:" + str(rec.get("dataRegName") or "")]
        mine = [c for k in dict.fromkeys(keys) for c in by_key.get(k, [])]
        latest = max((c for c in mine if c.at), key=lambda c: c.at, default=None)
        status = decode_status(rec.get("dataRegAuditStatus"))
        reg_no = str(rec.get("dataRegNo") or "")
        rows.append({
            "apply_ms": int_or_none(rec.get("dataRegApplyTime")) or 0,
            "reg_no": reg_no,
            "name": str(rec.get("dataRegName") or ""),
            "category": str(rec.get("dataRegDataType") or ""),
            "apply": _beijing(rec.get("dataRegApplyTime")),
            "status": status,
            "corrections": correction_count(status, mine, noticed.get(reg_no, 0)),
            "latest": latest,
        })
    rows.sort(key=lambda r: (r["apply_ms"], r["reg_no"]))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    status_rows = list(_STATUS_ROWS) + [
        (label, "gray", _OTHER_STATUS_NOTES.get(label, ""))
        for label in sorted(set(counts) - set(_STATUS_TONE), key=lambda s: (-counts[s], s))
    ]

    header = 2 + len(status_rows) + 2 + 1  # 标题、表头、各状态、合计、已通过、大标题
    first = header + 1
    last = first + len(rows) - 1
    end = max(last, first + _FORMULA_ROWS - 1)

    ws.write_string(0, 0, "一、总体进度", f(bold=True, font_size=12, font_color=_NAVY, align="left"))
    head = f(bold=True, font_color="#FFFFFF", bg_color=_NAVY, align="center", **_BORDER)
    ws.write_string(1, 0, "状态", head)
    ws.write_string(1, 1, "数量（项）", head)
    ws.merge_range(1, 2, 1, 4, "说明", head)

    text = f(align="left", indent=1, **_BORDER)
    count = f(num_format=_COUNT, align="center", **_BORDER)
    row_of: dict[str, int] = {}
    for i, (label, tone, note) in enumerate(status_rows):
        r = 2 + i
        row_of[label] = r
        ws.write_string(r, 0, label, f.tone(tone))
        ws.write_formula(r, 1, f"=COUNTIF({_rng('G', first, end)},{xl_rowcol_to_cell(r, 0)})",
                         count, counts.get(label, 0))
        ws.merge_range(r, 2, r, 4, note, text)

    r = 2 + len(status_rows)
    total = dict(bold=True, bg_color=_TOTAL_FILL, **_BORDER)
    ws.write_string(r, 0, "合计", f(align="center", **total))
    ws.write_formula(r, 1, f"=COUNTA({_rng('D', first, end)})",
                     f(num_format=_COUNT, align="center", **total), len(rows))
    ws.merge_range(r, 2, r, 4, f"数据截至 {as_of:%Y-%m-%d %H:%M}（最近一次巡检）" if as_of else "",
                   f(align="left", indent=1, **total))

    r += 1
    passed = [label for label in _PASSED if label in row_of]
    ws.write_string(r, 0, "已通过审核（含公示中）", f.tone("passed"))
    ws.write_formula(r, 1, "=" + "+".join(xl_rowcol_to_cell(row_of[p], 1) for p in passed),
                     f.tone("passed", num_format=_COUNT), sum(counts.get(p, 0) for p in passed))
    ws.merge_range(r, 2, r, 4, "、".join(passed) + "的申请均已通过审核",
                   f.tone("passed", bold=False, align="left", indent=1))

    ws.merge_range(header - 1, 0, header - 1, len(_RECORD_COLUMNS) - 1, "登记申请明细",
                   f(bold=True, font_size=22, font_name="黑体", font_color="#595959", align="left"))
    for c, (title, _) in enumerate(_RECORD_COLUMNS):
        ws.write_string(header, c, title, head)

    center = f(align="center", **_BORDER)
    day = f(num_format="yyyy-mm-dd", align="center", **_BORDER)
    minute = f(num_format="yyyy-mm-dd hh:mm", align="center", **_BORDER)
    bold = f(bold=True, align="center", **_BORDER)
    for i, row in enumerate(rows):
        r = first + i
        ws.write_number(r, 0, i + 1, center)
        ws.write_string(r, 1, company, center)
        ws.write_string(r, 2, row["reg_no"], center)
        ws.write_string(r, 3, row["name"], text)
        ws.write_string(r, 4, row["category"], text)
        if row["apply"]:
            ws.write_datetime(r, 5, row["apply"].replace(hour=0, minute=0, second=0, microsecond=0), day)
        else:
            ws.write_blank(r, 5, None, day)
        ws.write_string(r, 6, row["status"], bold)
        ws.write_number(r, 7, row["corrections"], count)
        latest = row["latest"]
        if latest:
            ws.write_datetime(r, 8, latest.at, minute)
            ws.write_string(r, 9, latest.note, center)
        else:
            ws.write_blank(r, 8, None, minute)
            ws.write_blank(r, 9, None, center)
        ws.write_blank(r, 10, None, text)

    _row_colors(wb, ws, first, end, "D", (6, 6), len(_RECORD_COLUMNS))
    _setup_sheet(ws, _RECORD_COLUMNS, "#5B9BD5", header, last)
    ws.activate()


def _write_changes(wb, f: _Formats, company: str, records: list[dict],
                   changes: list[_Change]) -> None:
    ws = wb.add_worksheet(SHEET_CHANGES)

    # 变更日志里还没编号的记录，台账上显示它现在的登记编号
    current_no: dict[str, str] = {}
    for rec in records:
        for k in _record_keys(rec):
            current_no[k] = str(rec.get("dataRegNo") or "")

    counts: dict[str, int] = {}
    names: dict[str, set[str]] = {}
    for c in changes:
        counts[c.note] = counts.get(c.note, 0) + 1
        names.setdefault(c.note, set()).add(c.name)
    fixed = {label for label, _, _ in _CHANGE_ROWS}
    extra = sorted(set(counts) - fixed - {NEW_APPLICATION}, key=lambda s: (-counts[s], s))
    kinds = list(_CHANGE_ROWS)
    if NEW_APPLICATION in counts:
        kinds.append((NEW_APPLICATION, "gray", _NEW_APPLICATION_NOTE))
    kinds += [(label, "gray", "状态由「{}」变为「{}」".format(*label.split("→", 1))) for label in extra]

    header = 2 + len(kinds) + 1 + 1  # 标题、表头、各类、合计、大标题
    first = header + 1
    last = first + max(len(changes), 1) - 1
    end = max(last, first + _FORMULA_ROWS - 1)
    H, E = _rng("H", first, end), _rng("E", first, end)

    ws.write_string(0, 0, "一、变化概况", f(bold=True, font_size=12, font_color=_NAVY, align="left"))
    head = f(bold=True, font_color="#FFFFFF", bg_color=_NAVY, align="center", **_BORDER)
    for c, title in enumerate(("变化类型", "次数", "涉及项数")):
        ws.write_string(1, c, title, head)
    ws.merge_range(1, 3, 1, 7, "说明", head)

    text = f(align="left", indent=1, **_BORDER)
    count = f(num_format=_COUNT, align="center", **_BORDER)
    for i, (label, tone, note) in enumerate(kinds):
        r = 2 + i
        a = f"$A{r + 1}"
        ws.write_string(r, 0, label, f.tone(tone))
        ws.write_formula(r, 1, f"=COUNTIF({H},{a})", count, counts.get(label, 0))
        ws.write_formula(
            r, 2, f'=ROUND(SUMPRODUCT(({H}={a})/COUNTIFS({E},{E}&"",{H},{H}&"")),0)',
            count, len(names.get(label, ())))
        ws.merge_range(r, 3, r, 7, note, text)

    r = 2 + len(kinds)
    total = dict(bold=True, bg_color=_TOTAL_FILL, **_BORDER)
    total_count = f(num_format=_COUNT, align="center", **total)
    ws.write_string(r, 0, "合计", f(align="center", **total))
    ws.write_formula(r, 1, f"=COUNTA({E})", total_count, len(changes))
    ws.write_formula(r, 2, f'=ROUND(SUMPRODUCT(({E}<>"")/COUNTIF({E},{E}&"")),0)', total_count,
                     len({c.name for c in changes}))
    times = [c.at for c in changes if c.at]
    span = (f"记录区间：{min(times):%Y-%m-%d %H:%M} 至 {max(times):%Y-%m-%d %H:%M}，按时间倒序排列"
            if times else "开始监控以来暂无状态变化")
    ws.merge_range(r, 3, r, 7, span, f(align="left", indent=1, **total))

    ws.merge_range(header - 1, 0, header - 1, len(_CHANGE_COLUMNS) - 1, "状态变更记录",
                   f(bold=True, font_size=22, font_name="黑体", font_color="#595959", align="left"))
    for c, (title, _) in enumerate(_CHANGE_COLUMNS):
        ws.write_string(header, c, title, head)

    center = f(align="center", **_BORDER)
    minute = f(num_format="yyyy-mm-dd hh:mm", align="center", **_BORDER)
    if not changes:
        ws.merge_range(first, 0, first, len(_CHANGE_COLUMNS) - 1, "（开始监控以来暂无状态变化）",
                       f(align="center", font_color="#808080", **_BORDER))
    for i, c in enumerate(changes):
        r = first + i
        reg_no = current_no.get(c.key) or ("" if c.reg_no.startswith("id:") else c.reg_no)
        ws.write_number(r, 0, i + 1, center)
        if c.at:
            ws.write_datetime(r, 1, c.at, minute)
        else:
            ws.write_blank(r, 1, None, minute)
        ws.write_string(r, 2, company, center)
        ws.write_string(r, 3, reg_no, center)
        ws.write_string(r, 4, c.name, text)
        ws.write_string(r, 5, c.before, center)
        ws.write_string(r, 6, c.after, center)
        ws.write_string(r, 7, c.note, center)

    _row_colors(wb, ws, first, end, "E", (5, 6), len(_CHANGE_COLUMNS))
    _setup_sheet(ws, _CHANGE_COLUMNS, "#F4B183", header, last)


def build_ledger(
    company: str,
    records: list[dict],
    change_rows: list[dict],
    *,
    corrections_noticed: dict[str, int] | None = None,
    as_of: str | None = None,
) -> bytes:
    """一家企业的台账 xlsx。

    records:      最新快照里的平台记录
    change_rows:  web.services.events.read_change_rows 的行，新的在前
    corrections_noticed: 登记编号 → 收到的补正通知书份数
    as_of:        快照时间（ISO），写在「合计」一行里
    """
    changes = _changes(change_rows)
    buf = io.BytesIO()
    # 文字一律按文字写：数据名称以 = 开头也不能变成公式
    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_formulas": False,
                                   "strings_to_urls": False, "strings_to_numbers": False})
    wb.set_properties({"title": f"数据知识产权登记进度台账 · {company}"})
    f = _Formats(wb)
    _write_records(wb, f, company, records, changes, corrections_noticed or {}, _beijing(as_of))
    _write_changes(wb, f, company, records, changes)
    wb.close()
    return buf.getvalue()
