"""Text and key facts from the platform's notice and certificate PDFs.

Every document seen so far carries a text layer, so no OCR is needed. The
layout breaks sentences across lines and pads digits and CJK characters with
spaces; normalize() undoes that before the regexes run. pypdf is imported
lazily: it is a local-only dependency (requirements-dev.txt), never installed
on the server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Characters the PDF layout pads with spaces the prose never had.
_TIGHT = r"[　-〿一-鿿＀-￯\d年月日：:，,。、；;（）()《》“”■□]"
_DATE = r"(\d{4})年(\d{1,2})月(\d{1,2})日"

# document type (file name without extension) → short kind
_KINDS = (
    ("补正通知书", "补正"),
    ("公示阶段通知书", "公示"),
    ("登记公告通知书", "公告"),
    ("不予登记通知书", "不予登记"),
    ("异议转送通知书", "异议"),
    ("国家局登记证书", "国家局证书"),
    ("浙江省登记证书", "省证书"),
)


@dataclass
class Facts:
    kind: str
    submitted_on: str | None = None   # 您于…提交 / 申请日期
    issued_on: str | None = None      # 落款日 / 登记日期
    deadline: str | None = None       # 补正：…日前无正当理由逾期不答复的，视为撤回
    valid_from: str | None = None     # 公告：电子证书有效期
    valid_to: str | None = None
    name_in_doc: str | None = None    # 文书上写的数据名称（可能与平台现名不同）
    reasons: str | None = None        # 补正理由 / 不予登记理由 / 异议来源
    fields: dict[str, str] = field(default_factory=dict)


def extract_text(path) -> str:
    """Whole-document text; '' when the file is not a PDF or has no text layer."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def normalize(text: str) -> str:
    """Join the layout's lines and drop the spaces it put around digits and
    CJK ("2026 年 10 月 14 日", "日度统计\\n分析数据"). Spaces between Latin
    words survive ("bare island")."""
    flat = re.sub(r"\s*\n\s*", "", text)
    flat = re.sub(rf"\s+(?={_TIGHT})|(?<={_TIGHT})\s+", "", flat)
    return flat.strip()


def kind_of(doc_type: str) -> str:
    for needle, kind in _KINDS:
        if needle in doc_type:
            return kind
    return "其他"


def _iso(m: re.Match, start: int = 1) -> str:
    y, mo, d = (int(m.group(start + i)) for i in range(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _line_fields(text: str) -> dict[str, str]:
    """`key：value` lines, as on the certificates. Keys lose the layout's
    padding (登     记     号 → 登记号); only short keys count, so a sentence
    that happens to contain a colon is not taken for a field."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([^:：]{1,20}?)\s*[:：]\s*(.+?)\s*$", line)
        if not m:
            continue
        key = re.sub(r"\s+", "", m.group(1))
        if 1 < len(key) <= 10 and key not in out:
            out[key] = m.group(2)
    return out


def _parse_certificate(text: str, facts: Facts) -> Facts:
    fields = _line_fields(text)
    facts.fields = fields
    facts.name_in_doc = fields.get("数据名称") or fields.get("数据知识产权名称")
    for key, attr in (("申请日期", "submitted_on"), ("登记日期", "issued_on")):
        if m := re.search(_DATE, re.sub(r"\s+", "", fields.get(key, ""))):
            setattr(facts, attr, _iso(m))
    return facts


def parse(text: str, doc_type: str) -> Facts:
    facts = Facts(kind=kind_of(doc_type))
    if not text.strip():
        return facts
    if facts.kind in ("国家局证书", "省证书"):
        return _parse_certificate(text, facts)

    flat = normalize(text)
    if m := re.search(rf"您于{_DATE}提交", flat):
        facts.submitted_on = _iso(m)
    dates = list(re.finditer(_DATE, flat))
    if dates:
        facts.issued_on = _iso(dates[-1])  # the signature date closes every notice
    if m := re.search(r"提交的[(（]?(.+?)[)）]?数据知识产权登记", flat):
        facts.name_in_doc = m.group(1)
    if m := re.search(r"咨询[:：]?([\d-]{7,})", flat):
        facts.fields["咨询电话"] = m.group(1)

    if facts.kind == "补正":
        if m := re.search(rf"{_DATE}前无正当理由", flat):
            facts.deadline = _iso(m)
        wanted = [w for w in ("补正修改", "说明") if f"■{w}" in flat]
        if wanted:
            facts.fields["要求"] = "、".join(wanted)
        parts = [p for p in ("登记申请表填写不符合要求", "证明文件不符合要求") if f"■{p}" in flat]
        if m := re.search(r"■针对(.+?)需要作出补充说明", flat):
            parts.append("针对：" + m.group(1).strip("，。；; "))
        if m := re.search(r"备注[:：](.+?)(?:工作时间联系咨询|浙江省数据知识产权登记中心|$)", flat):
            remark = m.group(1).strip("，。；; ")
            if remark and remark != "无":
                parts.append("备注：" + remark)
        facts.reasons = "\n".join(parts) or None
    elif facts.kind == "不予登记":
        if m := re.search(r"规定[:：](.+?)登记平台对此", flat):
            facts.reasons = m.group(1).strip()
    elif facts.kind == "异议":
        if m := re.search(r"收到(.+?)的异议", flat):
            facts.reasons = f"收到{m.group(1)}的异议"
    elif facts.kind == "公告":
        if m := re.search(rf"自{_DATE}至{_DATE}", flat):
            facts.valid_from = _iso(m, 1)
            facts.valid_to = _iso(m, 4)
    return facts
