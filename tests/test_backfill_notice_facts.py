"""scripts/backfill_notice_facts.py: facts for notices stored before facts existed."""

import io
import json

import pytest

from scripts import backfill_notice_facts as script
from src import control, pdftext
from tests.test_archive_pdftext import ANNOUNCEMENT, CORRECTION

NOTICE = "《浙江省数据知识产权登记形式审查补正通知书》.pdf"
GAZETTE = "《浙江省数据知识产权登记登记公告通知书》.pdf"


@pytest.fixture
def tree(tmp_path, monkeypatch, isolated_control_dir):
    monkeypatch.setattr(script, "_ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(script, "setup_logging", lambda name: None)
    monkeypatch.setattr(pdftext, "extract_text",
                        lambda p: {b"c": CORRECTION, b"a": ANNOUNCEMENT}.get(p.read_bytes(), ""))
    a = tmp_path / "甲公司" / "A"
    a.mkdir(parents=True)
    (a / NOTICE).write_bytes(b"c")
    (a / "国家局登记证书.pdf").write_bytes(b"cert")
    (a / ".sync.json").write_text(json.dumps({"status": 2, "files": [NOTICE]}), encoding="utf-8")
    b = tmp_path / "甲公司" / "B"
    b.mkdir()
    (b / GAZETTE).write_bytes(b"blank")  # no text layer
    (b / ".sync.json").write_text(json.dumps({"status": 7, "facts": {GAZETTE: {"kind": "公告", "valid_to": "2029-01-01"}}}), encoding="utf-8")
    c = tmp_path / "甲公司" / "C"  # released: no files on disk
    c.mkdir()
    (c / ".sync.json").write_text(json.dumps({"status": 7, "archived": {GAZETTE: {"sha256": "x"}}}), encoding="utf-8")
    return tmp_path


def _facts(root, reg):
    return json.loads((root / "甲公司" / reg / ".sync.json").read_text(encoding="utf-8")).get("facts")


def test_parses_missing_and_keeps_existing(tree):
    counts = script.backfill()
    assert counts == {"records": 1, "parsed": 1, "merged": 0, "unreadable": 0, "dry_run": False}
    assert _facts(tree, "A")[NOTICE]["deadline"] == "2026-10-14"
    assert "国家局登记证书.pdf" not in _facts(tree, "A")
    assert _facts(tree, "B") == {GAZETTE: {"kind": "公告", "valid_to": "2029-01-01"}}  # existing untouched
    assert script.backfill() == {"records": 0, "parsed": 0, "merged": 0, "unreadable": 0, "dry_run": False}


def test_stdin_fills_gaps_only(tree):
    incoming = {
        "甲公司/C": {GAZETTE: {"kind": "公告", "valid_from": "2026-08-08", "valid_to": "2029-08-08", "reasons": "x" * 500}},
        "甲公司/B": {GAZETTE: {"kind": "公告", "valid_to": "1999-01-01"}},  # already known → ignored
        "甲公司/A": {"证书.pdf": {"kind": "国家局证书"}, NOTICE: "junk"},   # not a notice / not a dict
        "乙公司/Z": {GAZETTE: {"kind": "公告"}},                            # no such record dir
    }
    counts = script.backfill(incoming)
    assert (counts["merged"], counts["parsed"]) == (1, 1)
    got = _facts(tree, "C")[GAZETTE]
    assert got["valid_to"] == "2029-08-08" and len(got["reasons"]) == 400
    assert _facts(tree, "B")[GAZETTE]["valid_to"] == "2029-01-01"


def test_dry_run_writes_nothing(tree):
    counts = script.backfill(dry_run=True)
    assert counts["parsed"] == 1 and counts["dry_run"] is True
    assert _facts(tree, "A") is None


def test_main_needs_the_lock_and_prints_json(tree, monkeypatch, capsys):
    holder = control.try_acquire(kind="run_all", trigger="daemon")
    try:
        assert script.main([]) == 2
    finally:
        holder.finish("done")
    assert "error" in json.loads(capsys.readouterr().out)

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"甲公司/C": {GAZETTE: {"kind": "公告", "valid_to": "2029-08-08"}}})))
    assert script.main(["--from-stdin"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["parsed"], out["merged"]) == (1, 1)
    assert control.read_state()["status"] == "done"
