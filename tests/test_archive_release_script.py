"""scripts/archive_release.py: the server deletes a file only when every check passes.

It runs as root against production data, driven by a laptop; nothing the
laptop sends is trusted — not the path, not the hash, not its idea of which
records are finished.
"""

import hashlib
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts import archive_release
from src import control
from web.services import snapshot as snap_svc

NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)
OLD = int((NOW - timedelta(days=200)).timestamp() * 1000)
RECENT = int((NOW - timedelta(days=30)).timestamp() * 1000)


@pytest.fixture
def server(tmp_path, monkeypatch, isolated_control_dir):
    att = tmp_path / "attachments"
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    monkeypatch.setattr(archive_release, "_ATTACHMENTS_DIR", att)
    monkeypatch.setattr(snap_svc, "_SNAPSHOTS_DIR", snaps)
    snap_svc._cache.clear()
    records = [
        {"dataRegNo": "DONE", "dataRegAuditStatus": 7, "updateTime": OLD},
        {"dataRegNo": "FRESH", "dataRegAuditStatus": 7, "updateTime": RECENT},
        {"dataRegNo": "ACTIVE", "dataRegAuditStatus": 2, "updateTime": OLD},
    ]
    (snaps / "甲公司_2026-09-26-1000.json").write_text(
        json.dumps({"company": "甲公司", "records": records}, ensure_ascii=False), encoding="utf-8"
    )
    files = {}
    for reg, name, body in [
        ("DONE", "国家局登记证书.pdf", b"cert"),
        ("DONE", "《登记公告通知书》.pdf", b"notice"),
        ("FRESH", "国家局登记证书.pdf", b"fresh"),
        ("ACTIVE", "《补正通知书》.pdf", b"current"),
        ("ACTIVE", "《补正通知书》_2026-09-01.pdf", b"old-round"),
    ]:
        d = att / "甲公司" / reg
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(body)
        files[f"甲公司/{reg}/{name}"] = hashlib.sha256(body).hexdigest()
    (att / "甲公司" / "DONE" / ".sync.json").write_text(json.dumps({
        "status": 7, "files": ["国家局登记证书.pdf", "《登记公告通知书》.pdf"],
        "sources": {"国家局登记证书.pdf": "path:/abc.pdf"},
    }, ensure_ascii=False), encoding="utf-8")
    yield att, files
    snap_svc._cache.clear()


def _release(files, paths, **kw):
    items = [{"path": p, "sha256": files.get(p, "0" * 64)} for p in paths]
    return archive_release.release(items, dry_run=kw.get("dry_run", False), days=90, now=NOW)


def test_releases_a_verified_file_of_a_finished_record(server):
    att, files = server
    result = _release(files, ["甲公司/DONE/国家局登记证书.pdf"])

    assert result["released"] == ["甲公司/DONE/国家局登记证书.pdf"]
    assert result["bytes"] == 4
    assert not (att / "甲公司/DONE/国家局登记证书.pdf").exists()
    manifest = json.loads((att / "甲公司/DONE/.sync.json").read_text(encoding="utf-8"))
    entry = manifest["archived"]["国家局登记证书.pdf"]
    assert entry["sha256"] == files["甲公司/DONE/国家局登记证书.pdf"]
    assert entry["size"] == 4 and entry["archived_at"].startswith("2026-09-27")
    assert manifest["files"] == ["《登记公告通知书》.pdf"]
    assert manifest["sources"] == {"国家局登记证书.pdf": "path:/abc.pdf"}  # identity kept for the sync


def test_dry_run_deletes_nothing(server):
    att, files = server
    result = _release(files, ["甲公司/DONE/国家局登记证书.pdf"], dry_run=True)
    assert result["released"] == ["甲公司/DONE/国家局登记证书.pdf"] and result["dry_run"] is True
    assert (att / "甲公司/DONE/国家局登记证书.pdf").exists()
    assert "archived" not in json.loads((att / "甲公司/DONE/.sync.json").read_text(encoding="utf-8"))


def test_the_server_judges_the_policy_from_its_own_snapshot(server):
    att, files = server
    result = _release(files, ["甲公司/FRESH/国家局登记证书.pdf", "甲公司/ACTIVE/《补正通知书》.pdf"])
    assert result["released"] == []
    assert {s["reason"] for s in result["skipped"]} == {"按服务器快照不满足释放条件"}
    assert (att / "甲公司/FRESH/国家局登记证书.pdf").exists()
    assert (att / "甲公司/ACTIVE/《补正通知书》.pdf").exists()


def test_an_older_round_of_an_active_record_may_go(server):
    att, files = server
    result = _release(files, ["甲公司/ACTIVE/《补正通知书》_2026-09-01.pdf"])
    assert result["released"] == ["甲公司/ACTIVE/《补正通知书》_2026-09-01.pdf"]
    assert (att / "甲公司/ACTIVE/《补正通知书》.pdf").exists()
    manifest = json.loads((att / "甲公司/ACTIVE/.sync.json").read_text(encoding="utf-8"))
    assert list(manifest["archived"]) == ["《补正通知书》_2026-09-01.pdf"]


def test_a_hash_mismatch_is_refused(server):
    att, files = server
    files = dict(files, **{"甲公司/DONE/国家局登记证书.pdf": "f" * 64})
    result = _release(files, ["甲公司/DONE/国家局登记证书.pdf"])
    assert result["released"] == [] and result["skipped"][0]["reason"] == "哈希与本机副本不一致"
    assert (att / "甲公司/DONE/国家局登记证书.pdf").exists()


@pytest.mark.parametrize("path", [
    "../甲公司/DONE/国家局登记证书.pdf", "/etc/passwd", "甲公司/DONE/.sync.json",
    "甲公司/DONE/x.pdf.part", "甲公司/DONE", "甲公司/../甲公司/DONE/国家局登记证书.pdf", "",
])
def test_anything_but_a_document_path_is_refused(server, path):
    att, files = server
    result = _release(files, [path])
    assert result["released"] == [] and result["skipped"][0]["reason"] == "路径不合法"
    assert (att / "甲公司/DONE/.sync.json").exists()


def test_a_missing_file_is_reported_not_fatal(server):
    _, files = server
    result = _release(files, ["甲公司/DONE/不存在.pdf", "甲公司/DONE/国家局登记证书.pdf"])
    assert result["skipped"] == [{"path": "甲公司/DONE/不存在.pdf", "reason": "服务器上没有这个文件"}]
    assert result["released"] == ["甲公司/DONE/国家局登记证书.pdf"]


@pytest.fixture(autouse=True)
def _quiet_logging(monkeypatch):
    monkeypatch.setattr(archive_release, "setup_logging", lambda name: None)


def test_main_needs_the_run_lock(server, monkeypatch, capsys):
    att, files = server
    holder = control.try_acquire(kind="run_all", trigger="daemon")
    try:
        items = [{"path": "甲公司/DONE/国家局登记证书.pdf", "sha256": files["甲公司/DONE/国家局登记证书.pdf"]}]
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(items)))
        assert archive_release.main([]) == 2
    finally:
        holder.finish("done")
    assert "error" in json.loads(capsys.readouterr().out)
    assert (att / "甲公司/DONE/国家局登记证书.pdf").exists()


def test_main_reads_stdin_and_prints_json(server, monkeypatch, capsys):
    att, files = server
    items = [{"path": "甲公司/DONE/《登记公告通知书》.pdf", "sha256": files["甲公司/DONE/《登记公告通知书》.pdf"]}]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(items, ensure_ascii=False)))
    assert archive_release.main([]) == 0  # settings.yaml absent in tests → the 90-day default
    out = json.loads(capsys.readouterr().out)
    assert out["released"] == ["甲公司/DONE/《登记公告通知书》.pdf"]
    assert control.read_state()["status"] == "done"  # the lock was released


def test_unarchive_forgets_marks_of_files_that_are_back(server):
    att, files = server
    _release(files, ["甲公司/DONE/国家局登记证书.pdf", "甲公司/DONE/《登记公告通知书》.pdf"])
    (att / "甲公司/DONE/国家局登记证书.pdf").write_bytes(b"cert")  # restore copied it back

    result = archive_release.unarchive("甲公司/DONE")
    assert result == {"restored": ["国家局登记证书.pdf"], "still_archived": ["《登记公告通知书》.pdf"]}
    manifest = json.loads((att / "甲公司/DONE/.sync.json").read_text(encoding="utf-8"))
    assert list(manifest["archived"]) == ["《登记公告通知书》.pdf"]
    assert "国家局登记证书.pdf" in manifest["files"]


def test_unarchive_refuses_a_bad_directory(server):
    with pytest.raises(ValueError):
        archive_release.unarchive("../甲公司")
