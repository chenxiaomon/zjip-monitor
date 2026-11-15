"""交付包：一家企业登记成功的成果，打成一个 zip 交给我们（再转给客户）。

    数据知识产权登记交付包_<公司>_<YYYYMMDD>/
        数据知识产权登记证书清单_<公司>_<YYYYMMDD>.xlsx
        证书/<数据名称>_<登记号>_国家局登记证书.pdf
        证书/<数据名称>_<登记号>_浙江省登记证书.pdf      （有才放）

只收登记成功、续展登记的记录；还有在审的，名字里的日期写成「截至<日期>」，
清单里注明「尚有 N 条在审，未列入」。不放通知书、不放整份台账（那是
「导出台账」）。

两条路径出同一个包，逐字节相同：控制台 GET /records/handover.zip 从服务器
data/ 取，本机 `python -m archive bundle` 从 ~/zjip-archive 取（已释放到本机
的证书也在那里）。所以本模块只吃「快照 + 附件目录」两样参数，不读任何模块
级路径、不碰 FastAPI；名字里的日期、zip 条目时间、xlsx 的创建时间都取快照
时间，不取「现在」。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import xlsxwriter
from xlsxwriter.utility import xl_rowcol_to_cell

from src import todo
from src.archive_policy import (
    NATIONAL_CERTIFICATE, PROVINCIAL_CERTIFICATE, TERMINAL_STATUSES, certificate_type,
)
from src.attachments import _safe_name, is_synced, load_manifest_cached
from src.scraper import decode_status, int_or_none
from web.services import ledger
from web.services.attachments_svc import attachment_download_name

MEDIA_TYPE = "application/zip"
SHEET = "登记证书清单"
CERT_DIR = "证书"
REGISTERED_STATUSES = frozenset({7, 13})  # 登记成功 续展登记
RENEWED_STATUS = 13
NAME_CAP = 60  # 证书文件名里数据名称最多几个字：解压到 Windows 也不超路径长度

MISSING_ARCHIVED = "缺：已归档到本机"
MISSING_NOT_ISSUED = "缺：平台未提供"
MISSING_NOT_FETCHED = "缺：未下载"

_SHANGHAI = timezone(timedelta(hours=8))
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

_COLUMNS = (  # 表头，列宽
    ("序号", 16), ("公司", 12.75), ("登记编号", 20), ("数据名称", 50), ("业务类别", 14),
    ("申请日期", 12), ("状态", 10), ("国家登记号", 22), ("证书编号", 20), ("有效期至", 12),
    ("附证书", 34), ("备注", 30),
)
_STATUS_COL = 6
_CERTS_COL = 10
_TONES = {"登记成功": "green", "续展登记": "green"}
_STATUS_NOTES = {
    "登记成功": "已完成登记，取得登记证书",
    "续展登记": "登记证书已续展",
}


# --- what goes in --------------------------------------------------------------

@dataclass(frozen=True)
class CertFile:
    label: str                 # 国家局登记证书 / 浙江省登记证书
    source: Path | None        # the file to pack; None when missing
    arcname: str | None        # 证书/<名字>, below the top folder
    missing: str | None        # MISSING_*; None when present

    @property
    def cell(self) -> str:
        return self.label if self.missing is None else f"{self.label}（{self.missing}）"


@dataclass(frozen=True)
class Row:
    reg_no: str
    name: str
    category: str
    apply_ms: int
    apply: datetime | None     # 北京时间，不带时区
    status: str
    national_reg_no: str
    certificate_no: str
    valid_to: str | None
    note: str
    certs: tuple[CertFile, ...]


@dataclass(frozen=True)
class Package:
    company: str
    as_of: datetime | None     # 快照时间，北京时间，不带时区
    rows: tuple[Row, ...]
    open_count: int
    fetch_complete: bool = True

    @property
    def day(self) -> str:
        on = self.as_of or datetime.now(_SHANGHAI).replace(tzinfo=None)
        return f"{'截至' if self.open_count else ''}{on:%Y%m%d}"

    @property
    def stem(self) -> str:
        return f"数据知识产权登记交付包_{ledger.safe_file_part(self.company)}_{self.day}"

    @property
    def zip_name(self) -> str:
        return self.stem + ".zip"

    @property
    def xlsx_name(self) -> str:
        return f"数据知识产权登记证书清单_{ledger.safe_file_part(self.company)}_{self.day}.xlsx"

    @property
    def present(self) -> list[CertFile]:
        return [c for r in self.rows for c in r.certs if c.source is not None]

    @property
    def missing(self) -> list[CertFile]:
        return [c for r in self.rows for c in r.certs if c.missing]

    @property
    def total_bytes(self) -> int:
        return sum(c.source.stat().st_size for c in self.present)


def counts(records: list[dict]) -> dict[str, int]:
    """{"registered": 登记成功+续展登记, "open": 还没办结} — what the button says."""
    registered = sum(1 for r in records if int_or_none(r.get("dataRegAuditStatus")) in REGISTERED_STATUSES)
    still_open = sum(1 for r in records if int_or_none(r.get("dataRegAuditStatus")) not in TERMINAL_STATUSES)
    return {"registered": registered, "open": still_open}


def _document(rec_dir: Path, label: str) -> Path | None:
    """The record's current certificate of this type, whatever its extension
    (src/attachments takes it from the download URL)."""
    try:
        files = sorted(rec_dir.iterdir())
    except OSError:
        return None
    for f in files:
        if (not f.name.startswith(".") and not f.name.endswith(".part")
                and certificate_type(f.name) == label and f.is_file()):
            return f
    return None


def certificate_state(rec_dir: Path, label: str, record: dict, manifest: dict) -> tuple[Path | None, str | None]:
    """(file, None) when the certificate is here; (None, why) when it should be
    and is not; (None, None) for a provincial certificate the platform never
    issued — it stopped issuing them in 2024, so that is the normal case."""
    found = _document(rec_dir, label)
    if found is not None:
        return found, None
    archived = manifest.get("archived")
    if isinstance(archived, dict) and any(certificate_type(n) == label for n in archived):
        return None, MISSING_ARCHIVED
    if label == PROVINCIAL_CERTIFICATE:
        return None, None
    return None, MISSING_NOT_ISSUED if is_synced(manifest, record) else MISSING_NOT_FETCHED


def _facts(manifest: dict) -> dict[str, dict]:
    facts = manifest.get("facts")
    return {k: v for k, v in facts.items() if isinstance(v, dict)} if isinstance(facts, dict) else {}


def plan(company: str, snapshot: dict, attachments_root: Path) -> Package:
    """What the package holds: the snapshot's registered records, each with its
    certificates looked up under attachments_root/<企业>/<登记号>/."""
    records = [r for r in snapshot.get("records") or [] if isinstance(r, dict)]
    company_dir = attachments_root / _safe_name(company)
    rows = []
    for rec in records:
        code = int_or_none(rec.get("dataRegAuditStatus"))
        if code not in REGISTERED_STATUSES:
            continue
        reg_no = str(rec.get("dataRegNo") or "")
        name = str(rec.get("dataRegName") or "")
        rec_dir = company_dir / _safe_name(reg_no)
        manifest = load_manifest_cached(rec_dir) if reg_no else {}
        certs = []
        for label in (NATIONAL_CERTIFICATE, PROVINCIAL_CERTIFICATE):
            source, missing = certificate_state(rec_dir, label, rec, manifest) if reg_no else (None, MISSING_NOT_FETCHED)
            if source is None and missing is None:
                continue
            arcname = (f"{CERT_DIR}/{attachment_download_name(name, reg_no, source.name, max_name=NAME_CAP)}"
                       if source is not None else None)
            certs.append(CertFile(label=label, source=source, arcname=arcname, missing=missing))

        _, valid_to = todo.certificate_validity(_facts(manifest))
        if code == RENEWED_STATUS:
            note = f"已续展；原证书有效期至 {valid_to}" if valid_to else "已续展"
            valid_to = None  # the 公告 only knows the first term
        else:
            note = "" if valid_to else "有效期未知（没有解析到登记公告）"
        rows.append(Row(
            reg_no=reg_no, name=name,
            category=str(rec.get("dataRegDataType") or ""),
            apply_ms=int_or_none(rec.get("dataRegApplyTime")) or 0,
            apply=ledger._beijing(rec.get("dataRegApplyTime")),
            status=decode_status(code),
            national_reg_no=str(rec.get("nationalRegNo") or ""),
            certificate_no=str(rec.get("nationalCertificateNo") or ""),
            valid_to=valid_to, note=note, certs=tuple(certs),
        ))
    rows.sort(key=lambda r: (r.apply_ms, r.reg_no))
    fetch = snapshot.get("fetch")
    return Package(
        company=company,
        as_of=ledger._beijing(snapshot.get("snapshot_time")),
        rows=tuple(rows),
        open_count=counts(records)["open"],
        fetch_complete=not (isinstance(fetch, dict) and fetch.get("complete") is False),
    )


# --- the workbook --------------------------------------------------------------

def build_xlsx(pkg: Package) -> bytes:
    """One sheet, the 进度台账's look, one row per registered record."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_formulas": False,
                                   "strings_to_urls": False, "strings_to_numbers": False})
    props = {"title": f"数据知识产权登记证书清单 · {pkg.company}"}
    if pkg.as_of:  # 固定创建时间：同一份快照做两次，文件一模一样
        props["created"] = pkg.as_of.replace(tzinfo=_SHANGHAI).astimezone(timezone.utc).replace(tzinfo=None)
    wb.set_properties(props)
    f = ledger._Formats(wb)
    ws = wb.add_worksheet(SHEET)
    rows = pkg.rows

    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    status_rows = [label for label in ("登记成功", "续展登记") if label == "登记成功" or by_status.get(label)]
    missing_national = sum(1 for r in rows for c in r.certs if c.missing and c.label == NATIONAL_CERTIFICATE)

    # 一、总体情况：各状态、合计、附证书、缺国家局证书；再空一行是大标题
    header = 2 + len(status_rows) + 3 + 1
    first = header + 1
    last = first + len(rows) - 1
    end = max(last, first + ledger._FORMULA_ROWS - 1)

    border = ledger._BORDER
    ws.write_string(0, 0, "一、总体情况", f(bold=True, font_size=12, font_color=ledger._NAVY, align="left"))
    head = f(bold=True, font_color="#FFFFFF", bg_color=ledger._NAVY, align="center", text_wrap=True, **border)
    ws.write_string(1, 0, "项目", head)
    ws.write_string(1, 1, "数量（项）", head)
    ws.merge_range(1, 2, 1, 4, "说明", head)

    text = f(align="left", indent=1, **border)
    count = f(num_format=ledger._COUNT, align="center", **border)
    r = 2
    for label in status_rows:
        ws.write_string(r, 0, label, f.tone("green"))
        ws.write_formula(r, 1, f"=COUNTIF({ledger._rng('G', first, end)},{xl_rowcol_to_cell(r, 0)})",
                         count, by_status.get(label, 0))
        ws.merge_range(r, 2, r, 4, _STATUS_NOTES[label], text)
        r += 1

    total = dict(bold=True, bg_color=ledger._TOTAL_FILL, **border)
    ws.write_string(r, 0, "合计", f(align="center", **total))
    ws.write_formula(r, 1, f"=COUNTA({ledger._rng('D', first, end)})",
                     f(num_format=ledger._COUNT, align="center", **total), len(rows))
    note = f"数据截至 {pkg.as_of:%Y-%m-%d %H:%M}（最近一次巡检）" if pkg.as_of else ""
    if pkg.open_count:
        note += f"；尚有 {pkg.open_count} 条在审，未列入"
    if not pkg.fetch_complete:
        note += "；该次巡检没有取全，清单可能不完整"
    ws.merge_range(r, 2, r, 4, note.lstrip("；"), f(align="left", indent=1, text_wrap=True, **total))
    r += 1

    national = sum(1 for c in pkg.present if c.label == NATIONAL_CERTIFICATE)
    provincial = sum(1 for c in pkg.present if c.label == PROVINCIAL_CERTIFICATE)
    ws.write_string(r, 0, "附证书", f.tone("passed"))
    ws.write_number(r, 1, national + provincial, f.tone("passed", num_format=ledger._COUNT))
    ws.merge_range(r, 2, r, 4, f"国家局登记证书 {national} 张、浙江省登记证书 {provincial} 张，在「{CERT_DIR}」文件夹",
                   f.tone("passed", bold=False, align="left", indent=1))
    r += 1
    tone = "orange" if missing_national else "gray"
    ws.write_string(r, 0, "缺国家局证书", f.tone(tone))
    ws.write_number(r, 1, missing_national, f.tone(tone, num_format=ledger._COUNT))
    ws.merge_range(r, 2, r, 4, "见下表「附证书」一列的说明" if missing_national else "每条都附上了",
                   f.tone(tone, bold=False, align="left", indent=1))

    ws.merge_range(header - 1, 0, header - 1, len(_COLUMNS) - 1, "登记证书清单",
                   f(bold=True, font_size=22, font_name="黑体", font_color="#595959", align="left"))
    for c, (title, _) in enumerate(_COLUMNS):
        ws.write_string(header, c, title, head)

    center = f(align="center", **border)
    day = f(num_format="yyyy-mm-dd", align="center", **border)
    bold = f(bold=True, align="center", **border)
    wrap = f(align="left", indent=1, text_wrap=True, **border)
    for i, row in enumerate(rows):
        rr = first + i
        ws.write_number(rr, 0, i + 1, center)
        ws.write_string(rr, 1, pkg.company, center)
        ws.write_string(rr, 2, row.reg_no, center)
        ws.write_string(rr, 3, row.name, text)
        ws.write_string(rr, 4, row.category, text)
        if row.apply:
            ws.write_datetime(rr, 5, row.apply.replace(hour=0, minute=0, second=0, microsecond=0), day)
        else:
            ws.write_blank(rr, 5, None, day)
        ws.write_string(rr, 6, row.status, bold)
        ws.write_string(rr, 7, row.national_reg_no, center)
        ws.write_string(rr, 8, row.certificate_no, center)
        if row.valid_to:
            try:
                ws.write_datetime(rr, 9, datetime.fromisoformat(row.valid_to), day)
            except ValueError:
                ws.write_string(rr, 9, row.valid_to, center)
        else:
            ws.write_blank(rr, 9, None, day)
        ws.write_string(rr, 10, "、".join(c.cell for c in row.certs), wrap)
        ws.write_string(rr, 11, row.note, wrap)

    ledger._row_colors(wb, ws, first, end, "D", (_STATUS_COL, _STATUS_COL), len(_COLUMNS), tones=_TONES)
    font, fill = ledger._TONES["orange"]
    ws.conditional_format(first, _CERTS_COL, end, _CERTS_COL, {
        "type": "text", "criteria": "containing", "value": "缺：",
        "format": wb.add_format({"font_color": font, "bg_color": fill}),
    })
    ledger._setup_sheet(ws, _COLUMNS, "#70AD47", header, last)
    ws.activate()
    wb.close()
    return buf.getvalue()


# --- the zip -------------------------------------------------------------------

def _entry(arcname: str, when: tuple) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(arcname, date_time=when)
    zi.compress_type = zipfile.ZIP_STORED  # PDF 和 xlsx 本身已压缩
    zi.create_system = 3
    zi.external_attr = 0o100644 << 16
    return zi


def write_zip(pkg: Package, dest: Path) -> int:
    """Write the package to dest; returns its size. Entries, their order and
    their times depend only on the package, so the server and the laptop
    produce the same bytes from the same snapshot and files."""
    when = pkg.as_of.timetuple()[:6] if pkg.as_of and pkg.as_of.year >= 1980 else _ZIP_EPOCH
    entries = [(f"{pkg.stem}/{pkg.xlsx_name}", None)]
    entries += sorted((f"{pkg.stem}/{c.arcname}", c.source) for c in pkg.present)
    with zipfile.ZipFile(dest, "w", allowZip64=True) as z:
        for arcname, source in entries:
            data = build_xlsx(pkg) if source is None else source.read_bytes()
            z.writestr(_entry(arcname, when), data)
    return dest.stat().st_size
