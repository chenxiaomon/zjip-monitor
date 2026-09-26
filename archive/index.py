"""archive.sqlite, updated incrementally from what `pull` put in ARCHIVE_DIR.

Tables:
    companies      one row per company name seen in any snapshot
    records        one row per registration (dataRegNo): first/last seen, latest state
    status_events  every added / changed / removed the monitor ever logged
    documents      one row per file under attachments/, with the facts parsed
                   from its text (dates, deadline, reasons) and the text itself
    documents_fts  FTS5 over documents.text/reasons (trigram: matches Chinese
                   substrings of 3+ characters; shorter terms: LIKE on documents)

Time columns: *_at are UTC ISO instants, *_on / apply_date are calendar dates
in Beijing time, as the platform and its documents state them.

Only new or changed files are read on each run (ingested / documents keep
size + mtime), so a re-run after `pull` takes seconds.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.attachments import _safe_name
from src.scraper import AUDIT_STATUS, int_or_none

from . import pdftext
from .config import ArchiveConfig

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ingested (path TEXT PRIMARY KEY, size INTEGER, mtime REAL);
CREATE TABLE IF NOT EXISTS companies (
    name TEXT PRIMARY KEY, dir_name TEXT NOT NULL, first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS records (
    reg_no TEXT PRIMARY KEY, company TEXT NOT NULL, name TEXT,
    industry TEXT, industry_categories TEXT, origin TEXT, apply_date TEXT,
    status_code INTEGER, status TEXT, platform_updated_at TEXT,
    national_reg_no TEXT, national_certificate_no TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, raw TEXT
);
CREATE TABLE IF NOT EXISTS status_events (
    reg_no TEXT NOT NULL, company TEXT, at TEXT NOT NULL, kind TEXT NOT NULL,
    old_code INTEGER, new_code INTEGER,
    PRIMARY KEY (reg_no, at, kind)
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
    company_dir TEXT NOT NULL, reg_no TEXT NOT NULL, filename TEXT NOT NULL,
    doc_type TEXT NOT NULL, doc_kind TEXT, superseded INTEGER NOT NULL DEFAULT 0,
    source TEXT, size INTEGER, mtime REAL, sha256 TEXT, downloaded_at TEXT,
    submitted_on TEXT, issued_on TEXT, deadline TEXT, valid_from TEXT, valid_to TEXT,
    name_in_doc TEXT, reasons TEXT, fields TEXT, text TEXT, released_at TEXT
);
CREATE INDEX IF NOT EXISTS documents_reg_no ON documents(reg_no);
CREATE INDEX IF NOT EXISTS status_events_reg_no ON status_events(reg_no);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(text, reasons, tokenize='trigram');
"""

# Same pattern the writer uses (src/main.py _SNAPSHOT_NAME_RE): {company}_{YYYY-MM-DD-HHMM}
_SNAPSHOT_RE = re.compile(r"^(?P<prefix>.+)_(\d{4}-\d{2}-\d{2}-\d{4})$")
# An older round kept under a dated name by src/attachments._archive_name()
_SUPERSEDED_RE = re.compile(r"^(?P<stem>.+?)_\d{4}-\d{2}-\d{2}(?:_\d+)?$")
_SHANGHAI_TZ = timezone(timedelta(hours=8))

# Columns that follow the newest snapshot; first_seen/last_seen are handled apart.
_LATEST_COLUMNS = (
    "company", "name", "industry", "industry_categories", "origin", "apply_date",
    "status_code", "status", "platform_updated_at", "national_reg_no",
    "national_certificate_no", "raw",
)
_RECORD_UPSERT = (
    f"INSERT INTO records (reg_no, {', '.join(_LATEST_COLUMNS)}, first_seen, last_seen) "
    f"VALUES ({', '.join('?' * (len(_LATEST_COLUMNS) + 3))}) "
    "ON CONFLICT(reg_no) DO UPDATE SET "
    + ", ".join(
        f"{c} = CASE WHEN excluded.last_seen >= last_seen THEN excluded.{c} ELSE {c} END"
        for c in _LATEST_COLUMNS
    )
    + ", first_seen = min(first_seen, excluded.first_seen)"
    + ", last_seen = max(last_seen, excluded.last_seen)"
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    return db


def _unchanged(db: sqlite3.Connection, key: str, path: Path) -> bool:
    st = path.stat()
    row = db.execute("SELECT size, mtime FROM ingested WHERE path = ?", (key,)).fetchone()
    return row is not None and row[0] == st.st_size and row[1] == st.st_mtime


def _mark(db: sqlite3.Connection, key: str, path: Path) -> None:
    st = path.stat()
    db.execute(
        "INSERT OR REPLACE INTO ingested (path, size, mtime) VALUES (?, ?, ?)",
        (key, st.st_size, st.st_mtime),
    )


def _ms_to_utc(value) -> str | None:
    ms = int_or_none(value)
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None


def _ms_to_date(value) -> str | None:
    ms = int_or_none(value)
    return datetime.fromtimestamp(ms / 1000, _SHANGHAI_TZ).strftime("%Y-%m-%d") if ms else None


def ingest_snapshots(db: sqlite3.Connection, root: Path) -> int:
    count = 0
    for path in sorted((root / "snapshots").glob("*.json")):
        key = f"snapshots/{path.name}"
        if not _SNAPSHOT_RE.match(path.stem) or _unchanged(db, key, path):
            continue
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # half-copied or corrupt: not marked, so the next run retries
        company, at = snap.get("company"), snap.get("snapshot_time")
        if company and at:
            db.execute(
                "INSERT INTO companies (name, dir_name, first_seen, last_seen) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET first_seen = min(first_seen, excluded.first_seen), "
                "last_seen = max(last_seen, excluded.last_seen)",
                (company, _safe_name(company), at, at),
            )
            for r in snap.get("records") or []:
                reg_no = r.get("dataRegNo")
                if not reg_no:
                    continue
                code = int_or_none(r.get("dataRegAuditStatus"))
                db.execute(_RECORD_UPSERT, (
                    reg_no, company, r.get("dataRegName"), r.get("dataRegIndustry"),
                    r.get("dataRegIndustryCategories"), r.get("dataRegOrigin"),
                    _ms_to_date(r.get("dataRegApplyTime")),
                    code, AUDIT_STATUS.get(code) if code is not None else None,
                    _ms_to_utc(r.get("updateTime")),
                    r.get("nationalRegNo"), r.get("nationalCertificateNo"),
                    json.dumps(r, ensure_ascii=False),
                    at, at,
                ))
        _mark(db, key, path)
        count += 1
    return count


def ingest_events(db: sqlite3.Connection, root: Path) -> int:
    """events.jsonl grows between pulls and is simply re-read (rows dedupe on
    reg_no + time + kind); rotated events-*.jsonl are read once."""
    count = 0
    for path in sorted((root / "events").glob("events*.jsonl")):
        key = f"events/{path.name}"
        if _unchanged(db, key, path):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            at, company = e.get("timestamp"), e.get("company")
            if not at:
                continue
            rows = []
            for c in e.get("changed") or []:
                rows.append((c.get("reg_no"), "changed", c.get("old_status_code"), c.get("new_status_code")))
            for a in e.get("added") or []:
                rows.append((a.get("dataRegNo"), "added", None, int_or_none(a.get("dataRegAuditStatus"))))
            for x in e.get("removed") or []:
                rows.append((x.get("dataRegNo"), "removed", int_or_none(x.get("dataRegAuditStatus")), None))
            for reg_no, kind, old, new in rows:
                if reg_no:
                    db.execute(
                        "INSERT OR IGNORE INTO status_events (reg_no, company, at, kind, old_code, new_code) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (reg_no, company, at, kind, old, new),
                    )
        _mark(db, key, path)
        count += 1
    return count


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_documents(db: sqlite3.Connection, root: Path) -> int:
    count = 0
    attachments = root / "attachments"
    for rec_dir in sorted(p for p in attachments.glob("*/*") if p.is_dir()):
        try:
            sources = json.loads((rec_dir / ".sync.json").read_text(encoding="utf-8")).get("sources") or {}
        except (OSError, ValueError):
            sources = {}
        for f in sorted(rec_dir.iterdir()):
            if not f.is_file() or f.name.startswith(".") or f.name.endswith(".part"):
                continue
            rel = f.relative_to(attachments).as_posix()
            st = f.stat()
            row = db.execute("SELECT id, size, mtime FROM documents WHERE path = ?", (rel,)).fetchone()
            if row and row[1] == st.st_size and row[2] == st.st_mtime:
                continue
            m = _SUPERSEDED_RE.match(f.stem)
            doc_type = m.group("stem") if m else f.stem
            text = pdftext.extract_text(f) if f.suffix.lower() == ".pdf" else ""
            facts = pdftext.parse(text, doc_type)
            values = {
                "path": rel, "company_dir": rec_dir.parent.name, "reg_no": rec_dir.name,
                "filename": f.name, "doc_type": doc_type, "doc_kind": facts.kind,
                "superseded": 1 if m else 0, "source": sources.get(f.name),
                "size": st.st_size, "mtime": st.st_mtime, "sha256": _sha256(f),
                "downloaded_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                "submitted_on": facts.submitted_on, "issued_on": facts.issued_on,
                "deadline": facts.deadline, "valid_from": facts.valid_from, "valid_to": facts.valid_to,
                "name_in_doc": facts.name_in_doc, "reasons": facts.reasons,
                "fields": json.dumps(facts.fields, ensure_ascii=False) if facts.fields else None,
                "text": text,
            }
            cols = ", ".join(values)
            updates = ", ".join(f"{c} = excluded.{c}" for c in values if c != "path")
            db.execute(
                f"INSERT INTO documents ({cols}) VALUES ({', '.join('?' * len(values))}) "
                f"ON CONFLICT(path) DO UPDATE SET {updates}",
                tuple(values.values()),
            )
            doc_id = db.execute("SELECT id FROM documents WHERE path = ?", (rel,)).fetchone()[0]
            db.execute("DELETE FROM documents_fts WHERE rowid = ?", (doc_id,))
            db.execute(
                "INSERT INTO documents_fts (rowid, text, reasons) VALUES (?, ?, ?)",
                (doc_id, text, facts.reasons or ""),
            )
            count += 1
    return count


def build(root: Path, db_path: Path) -> dict[str, int]:
    db = connect(db_path)
    try:
        with db:
            snapshots = ingest_snapshots(db, root)
        with db:
            events = ingest_events(db, root)
        with db:
            documents = ingest_documents(db, root)
        return {"snapshots": snapshots, "events": events, "documents": documents}
    finally:
        db.close()


def summary(db_path: Path) -> dict[str, int]:
    db = sqlite3.connect(db_path)
    try:
        q = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "companies": q("SELECT count(*) FROM companies"),
            "records": q("SELECT count(*) FROM records"),
            "pending_correction": q("SELECT count(*) FROM records WHERE status_code = 2"),
            "status_events": q("SELECT count(*) FROM status_events"),
            "documents": q("SELECT count(*) FROM documents"),
            "with_text": q("SELECT count(*) FROM documents WHERE text <> ''"),
            "correction_notices": q("SELECT count(*) FROM documents WHERE doc_kind = '补正'"),
            "with_deadline": q("SELECT count(*) FROM documents WHERE deadline IS NOT NULL"),
        }
    finally:
        db.close()


def run(cfg: ArchiveConfig) -> int:
    if not cfg.archive_dir.is_dir():
        print(f"✗ 归档目录 {cfg.archive_dir} 不存在，先运行 python -m archive pull")
        return 1
    new = build(cfg.archive_dir, cfg.db_path)
    s = summary(cfg.db_path)
    print(
        f"✓ 本次处理：快照 {new['snapshots']} 份、事件日志 {new['events']} 个、文件 {new['documents']} 个\n"
        f"  库内合计：企业 {s['companies']}、登记申请 {s['records']}（其中待补正 {s['pending_correction']}）、"
        f"状态变化 {s['status_events']}、文件 {s['documents']}（有正文 {s['with_text']}；"
        f"补正通知书 {s['correction_notices']}，解析出截止日期 {s['with_deadline']}）\n"
        f"  数据库：{cfg.db_path}"
    )
    return 0
