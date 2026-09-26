"""python -m archive search / report — reading archive.sqlite without writing SQL.

search: full text over every notice and certificate (trigram FTS for 3+
characters, LIKE for shorter terms), with a snippet around each hit.
report: what the history says about filings — why corrections are asked for,
how many rounds they take, how long registration takes, which certificates
are due for renewal — printed and saved as Markdown under ARCHIVE_DIR/reports/.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from src.scraper import AUDIT_STATUS

from .config import ArchiveConfig
from src.pdftext import normalize

# Recurring themes in 补正 reasons, most specific first. A notice can hit several.
REASON_THEMES = (
    ("样例数据与算法不一致", r"样例|样本"),
    ("算法规则 / 规则说明", r"算法|规则"),
    ("字段含义或命名", r"字段"),
    ("写了固定数据量或统计时间", r"固定数据量|统计时间"),
    ("数据来源选择或证明", r"来源|公开收集|自行采集"),
    ("智力成果 / 实质处理不足", r"智力成果|实质意义"),
    ("登记申请表填写", r"登记申请表填写不符合要求"),
    ("证明文件", r"证明文件不符合要求"),
)


def _snippet(text: str, term: str, width: int = 24) -> str:
    flat = normalize(text or "")
    i = flat.find(term)
    if i < 0:
        return flat[: width * 2]
    start = max(0, i - width)
    return ("…" if start else "") + flat[start: i + len(term) + width] + "…"


def search(db_path: Path, term: str, *, kind: str | None = None, limit: int = 20) -> list[dict]:
    db = sqlite3.connect(db_path)
    try:
        where = " AND d.doc_kind = ?" if kind else ""
        params: list = [kind] if kind else []
        if len(term) >= 3:
            sql = (
                "SELECT d.company_dir, d.reg_no, d.doc_kind, d.issued_on, d.deadline, d.text, d.superseded "
                "FROM documents_fts f JOIN documents d ON d.id = f.rowid "
                f"WHERE documents_fts MATCH ?{where} ORDER BY d.issued_on DESC LIMIT ?"
            )
            params = ['"' + term.replace('"', '""') + '"'] + params + [limit]
        else:
            sql = (
                "SELECT d.company_dir, d.reg_no, d.doc_kind, d.issued_on, d.deadline, d.text, d.superseded "
                "FROM documents d WHERE (d.text LIKE ? OR d.reasons LIKE ?)"
                f"{where} ORDER BY d.issued_on DESC LIMIT ?"
            )
            params = [f"%{term}%", f"%{term}%"] + params + [limit]
        rows = db.execute(sql, params).fetchall()
    finally:
        db.close()
    return [
        {"company": c, "reg_no": r, "kind": k, "issued_on": i, "deadline": dl,
         "superseded": bool(s), "snippet": _snippet(t, term)}
        for c, r, k, i, dl, t, s in rows
    ]


def run_search(cfg: ArchiveConfig, term: str, *, kind: str | None = None, limit: int = 20) -> int:
    if not cfg.db_path.exists():
        print("✗ 还没有 archive.sqlite，先运行 python -m archive pull 和 index")
        return 1
    hits = search(cfg.db_path, term, kind=kind, limit=limit)
    for h in hits:
        tag = "（旧一轮）" if h["superseded"] else ""
        due = f" 截止 {h['deadline']}" if h["deadline"] else ""
        print(f"{h['issued_on'] or '----------'} {h['kind']}{tag} {h['company']} {h['reg_no']}{due}\n    {h['snippet']}")
    print(f"共 {len(hits)} 条" + ("（已达上限，可加 --limit）" if len(hits) == limit else ""))
    return 0


def build_report(db_path: Path, *, today: date | None = None) -> str:
    today = today or date.today()
    db = sqlite3.connect(db_path)
    try:
        q = db.execute
        lines = [f"# 数据知识产权登记归档报告（{today.isoformat()}）", ""]

        n_companies = q("SELECT count(*) FROM companies").fetchone()[0]
        by_label: Counter = Counter()
        for code, n in q("SELECT status_code, count(*) FROM records GROUP BY status_code"):
            by_label[AUDIT_STATUS.get(code, f"状态{code}")] += n  # 1 and 12 are both 待审核
        total = sum(by_label.values())
        lines += ["## 概况", "", f"企业 {n_companies} 家，登记申请 {total} 条：", ""]
        lines += [f"- {label}：{n}" for label, n in by_label.most_common()]

        reasons = [r for (r,) in q("SELECT reasons FROM documents WHERE doc_kind = '补正' AND reasons IS NOT NULL")]
        lines += ["", f"## 补正理由（共 {len(reasons)} 份补正通知书，含被替换的旧一轮）", ""]
        for theme, pattern in REASON_THEMES:
            hits = sum(1 for r in reasons if re.search(pattern, r))
            if hits:
                lines.append(f"- {theme}：{hits} 份（{hits * 100 // max(len(reasons), 1)}%）")

        rounds = Counter(n for (n,) in q(
            "SELECT count(*) FROM status_events WHERE new_code = 2 GROUP BY reg_no"
        ))
        if rounds:
            lines += ["", "## 补正轮数（有记录以来进入「待补正」的次数）", ""]
            lines += [f"- {k} 轮：{rounds[k]} 条申请" for k in sorted(rounds)]

        spans = []
        for apply_date, issued in q(
            "SELECT r.apply_date, min(d.issued_on) FROM records r JOIN documents d ON d.reg_no = r.reg_no "
            "WHERE d.doc_kind = '公告' AND r.apply_date IS NOT NULL AND d.issued_on IS NOT NULL GROUP BY r.reg_no"
        ):
            days = (date.fromisoformat(issued) - date.fromisoformat(apply_date)).days
            if days >= 0:
                spans.append(days)
        if spans:
            lines += ["", "## 从申请到登记公告", "",
                      f"- {len(spans)} 条已公告的申请：中位 {statistics.median(spans):g} 天，"
                      f"最快 {min(spans)} 天，最慢 {max(spans)} 天"]

        # The 公告's validity is the FIRST term. A renewed record (续展登记, 13)
        # runs three more years from the day after it, and no notice we hold
        # states the new date — so only records still at 登记成功 (7) are due.
        horizon = today + timedelta(days=365)
        due = q(
            "SELECT d.valid_to, d.company_dir, d.reg_no, coalesce(r.name, d.name_in_doc) FROM documents d "
            "JOIN records r ON r.reg_no = d.reg_no WHERE d.doc_kind = '公告' AND d.superseded = 0 "
            "AND r.status_code = 7 AND d.valid_to BETWEEN ? AND ? ORDER BY d.valid_to",
            (today.isoformat(), horizon.isoformat()),
        ).fetchall()
        renewed = q("SELECT count(*) FROM records WHERE status_code = 13").fetchone()[0]
        lines += ["", "## 一年内到期、尚未续展的登记证书（期满前六个月内可办理续展）", ""]
        if due:
            lines += [f"- {v} {c} {r} {n or ''}".rstrip() for v, c, r, n in due]
        else:
            lines.append("- 无")
        if renewed:
            lines.append(f"- （已续展的 {renewed} 条不在此列：续展后有效期自上一届期满次日起再算三年）")
        return "\n".join(lines) + "\n"
    finally:
        db.close()


def run_report(cfg: ArchiveConfig) -> int:
    if not cfg.db_path.exists():
        print("✗ 还没有 archive.sqlite，先运行 python -m archive pull 和 index")
        return 1
    text = build_report(cfg.db_path)
    out = cfg.archive_dir / "reports" / f"archive-report-{date.today():%Y%m%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"已保存：{out}")
    return 0
