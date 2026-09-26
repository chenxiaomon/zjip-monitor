"""Text and key facts from the platform's notice and certificate PDFs.

Every document seen so far carries a text layer, so no OCR is needed. The
layout breaks sentences across lines and pads digits and CJK characters with
spaces; normalize() undoes that before the regexes run. Used by the scan
(src/attachments.py, at download time) and by the laptop's archive tool;
pypdf is imported lazily so a missing package costs one warning, not the scan.
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
# The kinds whose facts the scan stores in .sync.json (certificates carry
# nothing the console needs that the record JSON lacks).
NOTICE_KINDS = frozenset({"补正", "公示", "公告", "不予登记", "异议"})
_REASONS_MAX = 400


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


def as_record(facts: Facts) -> dict:
    """The JSON-ready subset kept in .sync.json → facts[filename]."""
    return {
        "kind": facts.kind,
        "submitted_on": facts.submitted_on,
        "issued_on": facts.issued_on,
        "deadline": facts.deadline,
        "valid_from": facts.valid_from,
        "valid_to": facts.valid_to,
        "reasons": (facts.reasons or "")[:_REASONS_MAX] or None,
    }


def notice_facts(path) -> dict | None:
    """as_record() for a notice PDF on disk; None for certificates, non-PDFs,
    files without a text layer, or when pypdf is missing. Never raises."""
    try:
        doc_type = re.sub(r"_\d{4}-\d{2}-\d{2}(?:_\d+)?$", "", path.stem)
        if kind_of(doc_type) not in NOTICE_KINDS or path.suffix.lower() != ".pdf":
            return None
        text = extract_text(path)
        if not text.strip():
            return None
        return as_record(parse(text, doc_type))
    except Exception:  # noqa: BLE001 — a parse failure must never fail a scan
        return None


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
            start, end = _iso(m, 1), _iso(m, 4)
            if end > start:
                facts.valid_from, facts.valid_to = start, end
            else:
                # Seen on nine 2024 notices: "自2024年09月07日至2024年08月26日" —
                # the platform's own document is wrong. Keep the text, not
                # dates that would list the certificate as long expired.
                facts.fields["有效期原文"] = m.group(0)
    return facts
