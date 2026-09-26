"""archive/index.py: archive.sqlite built from a pulled ARCHIVE_DIR, incrementally."""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from archive import index, pdftext

from tests.test_archive_pdftext import CORRECTION

NOTICE = "《浙江省数据知识产权登记形式审查补正通知书》"
OLD_ROUND = (
    CORRECTION.replace("2026 年 10 月 14 日前", "2026 年 9 月 14 日前")
    .replace("算法规则、数据样例", "字段说明")
    .replace("备注:算法规则与数据样例不适应，样例数据中“等级”的获取过程在算法规则中没有说\n明。", "备注: 无 ")
)
APPLY_MS = int(datetime(2026, 8, 28, 10, 0, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)


def _snapshot(root, company, stamp, at, records):
    d = root / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"company": company, "snapshot_time": at, "record_count": len(records), "records": records}
    (d / f"{company}_{stamp}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _rec(reg_no, status, name, update_ms=0):
    return {"dataRegNo": reg_no, "dataRegAuditStatus": status, "dataRegName": name,
            "dataRegIndustry": "制造业", "dataRegApplyTime": APPLY_MS, "updateTime": update_ms}


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    root = tmp_path / "zjip-archive"
    _snapshot(root, "甲公司", "2026-09-01-0900", "2026-09-01T01:00:00+00:00", [_rec("R1", 1, "旧名")])
    _snapshot(root, "甲公司", "2026-09-22-0900", "2026-09-22T01:00:00+00:00",
              [_rec("R1", 2, "新名", 1790000000000), _rec("R2", 7, "另一条")])
    (root / "snapshots" / "not-a-snapshot.json").write_text("{}", encoding="utf-8")

    (root / "events").mkdir()
    (root / "events" / "events.jsonl").write_text(json.dumps({
        "account": "u", "company": "甲公司", "timestamp": "2026-09-22T01:00:30+00:00",
        "added": [_rec("R2", 7, "另一条")],
        "changed": [{"reg_no": "R1", "name": "新名", "old_status": "待审核", "old_status_code": 1,
                     "new_status": "待补正", "new_status_code": 2}],
        "removed": [],
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    rec = root / "attachments" / "甲公司" / "R1"
    rec.mkdir(parents=True)
    (rec / f"{NOTICE}.pdf").write_bytes(b"%PDF-round-2")
    (rec / f"{NOTICE}_2026-09-01.pdf").write_bytes(b"%PDF-round-1")
    (rec / f".{NOTICE}.pdf.incoming.part").write_bytes(b"in flight")
    (rec / ".sync.json").write_text(json.dumps({"status": 2, "sources": {f"{NOTICE}.pdf": "id:9"}}), encoding="utf-8")

    texts = {b"%PDF-round-2": CORRECTION, b"%PDF-round-1": OLD_ROUND}
    monkeypatch.setattr(pdftext, "extract_text", lambda path: texts.get(path.read_bytes(), ""))
    return root


def _db(root):
    return sqlite3.connect(root / "archive.sqlite")


def test_builds_records_history_and_documents(archive_dir):
    index.build(archive_dir, archive_dir / "archive.sqlite")
    db = _db(archive_dir)

    assert db.execute("SELECT name, dir_name FROM companies").fetchall() == [("甲公司", "甲公司")]
    r1 = db.execute(
        "SELECT status_code, status, name, apply_date, first_seen, last_seen, platform_updated_at "
        "FROM records WHERE reg_no = 'R1'"
    ).fetchone()
    assert r1[:4] == (2, "待补正", "新名", "2026-08-28")
    assert r1[4:6] == ("2026-09-01T01:00:00+00:00", "2026-09-22T01:00:00+00:00")
    assert r1[6].startswith("2026-09-21T")
    assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 2

    events = db.execute("SELECT reg_no, kind, old_code, new_code FROM status_events ORDER BY reg_no").fetchall()
    assert events == [("R1", "changed", 1, 2), ("R2", "added", None, 7)]

    docs = db.execute(
        "SELECT filename, doc_type, doc_kind, superseded, source, deadline, issued_on, sha256 "
        "FROM documents ORDER BY superseded"
    ).fetchall()
    assert [d[0] for d in docs] == [f"{NOTICE}.pdf", f"{NOTICE}_2026-09-01.pdf"]  # .part, .sync.json skipped
    current, older = docs
    assert current[1:7] == (NOTICE, "补正", 0, "id:9", "2026-10-14", "2026-09-23")
    assert older[1:6] == (NOTICE, "补正", 1, None, "2026-09-14")
    assert current[7] == hashlib.sha256(b"%PDF-round-2").hexdigest()


def test_notice_text_is_searchable(archive_dir):
    index.build(archive_dir, archive_dir / "archive.sqlite")
    db = _db(archive_dir)

    hits = db.execute(
        "SELECT d.filename FROM documents_fts f JOIN documents d ON d.id = f.rowid "
        "WHERE documents_fts MATCH '数据样例'"
    ).fetchall()
    assert hits == [(f"{NOTICE}.pdf",)]  # the older round asked about 字段说明 instead
    reasons = db.execute("SELECT reasons FROM documents WHERE superseded = 0").fetchone()[0]
    assert reasons.startswith("针对：算法规则、数据样例")


def test_rerun_is_incremental_and_picks_up_changes(archive_dir):
    db_path = archive_dir / "archive.sqlite"
    assert index.build(archive_dir, db_path) == {"snapshots": 2, "events": 1, "documents": 2}
    before = index.summary(db_path)

    assert index.build(archive_dir, db_path) == {"snapshots": 0, "events": 0, "documents": 0}
    assert index.summary(db_path) == before

    current = archive_dir / "attachments" / "甲公司" / "R1" / f"{NOTICE}.pdf"
    current.write_bytes(b"%PDF-round-3")
    os.utime(current, (current.stat().st_atime, current.stat().st_mtime + 60))
    assert index.build(archive_dir, db_path)["documents"] == 1
    sha = _db(archive_dir).execute("SELECT sha256 FROM documents WHERE superseded = 0").fetchone()[0]
    assert sha == hashlib.sha256(b"%PDF-round-3").hexdigest()


def test_an_older_snapshot_arriving_later_does_not_override_the_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(pdftext, "extract_text", lambda path: "")
    root = tmp_path / "a"
    db_path = root / "archive.sqlite"
    _snapshot(root, "甲公司", "2026-09-22-0900", "2026-09-22T01:00:00+00:00", [_rec("R1", 2, "新名")])
    index.build(root, db_path)
    _snapshot(root, "甲公司", "2026-09-01-0900", "2026-09-01T01:00:00+00:00", [_rec("R1", 1, "旧名")])
    index.build(root, db_path)

    row = _db(root).execute("SELECT status_code, name, first_seen, last_seen FROM records").fetchone()
    assert row == (2, "新名", "2026-09-01T01:00:00+00:00", "2026-09-22T01:00:00+00:00")


def test_a_half_copied_snapshot_is_retried_on_the_next_run(tmp_path, monkeypatch):
    monkeypatch.setattr(pdftext, "extract_text", lambda path: "")
    root = tmp_path / "a"
    db_path = root / "archive.sqlite"
    (root / "snapshots").mkdir(parents=True)
    broken = root / "snapshots" / "甲公司_2026-09-22-0900.json"
    broken.write_text('{"company": "甲公司", "records": [', encoding="utf-8")

    assert index.build(root, db_path)["snapshots"] == 0
    _snapshot(root, "甲公司", "2026-09-22-0900", "2026-09-22T01:00:00+00:00", [_rec("R1", 2, "新名")])
    assert index.build(root, db_path)["snapshots"] == 1
    assert _db(root).execute("SELECT count(*) FROM records").fetchone()[0] == 1


def test_run_refuses_a_missing_archive_dir(tmp_path, capsys):
    from archive.config import ArchiveConfig

    cfg = ArchiveConfig(server="s", remote="/r", archive_dir=tmp_path / "nope", ssh_cmd="ssh")
    assert index.run(cfg) == 1
    assert "pull" in capsys.readouterr().out
