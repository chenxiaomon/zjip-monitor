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
        ("DONE", "浙江省登记证书.pdf", b"prov"),
        ("FRESH", "《登记公告通知书》.pdf", b"fresh"),
        ("ACTIVE", "《补正通知书》.pdf", b"current"),
        ("ACTIVE", "《补正通知书》_2026-09-01.pdf", b"old-round"),
    ]:
        d = att / "甲公司" / reg
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(body)
        files[f"甲公司/{reg}/{name}"] = hashlib.sha256(body).hexdigest()
    (att / "甲公司" / "DONE" / ".sync.json").write_text(json.dumps({
        "status": 7, "files": ["国家局登记证书.pdf", "浙江省登记证书.pdf", "《登记公告通知书》.pdf"],
        "sources": {"《登记公告通知书》.pdf": "path:/abc.pdf"},
    }, ensure_ascii=False), encoding="utf-8")
    yield att, files
    snap_svc._cache.clear()


def _release(files, paths, **kw):
    items = [{"path": p, "sha256": files.get(p, "0" * 64)} for p in paths]
    return archive_release.release(items, dry_run=kw.get("dry_run", False), days=90,
                                   cert_days=kw.get("cert_days", 180), now=NOW)


NOTICE = "甲公司/DONE/《登记公告通知书》.pdf"


def _mark_archived(att, rel_dir, names):
    """What the 2026-09-26 release left behind: files gone, marks in the manifest."""
    path = att / rel_dir / ".sync.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["archived"] = {n: {"sha256": "0" * 64, "size": 1, "archived_at": "2026-09-26T00:00:00+00:00"}
                            for n in names}
    manifest["files"] = [f for f in manifest["files"] if f not in names]
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for n in names:
        (att / rel_dir / n).unlink(missing_ok=True)


def test_releases_a_verified_file_of_a_finished_record(server):
    att, files = server
    result = _release(files, [NOTICE])

    assert result["released"] == [NOTICE]
    assert result["bytes"] == 6
    assert not (att / NOTICE).exists()
    manifest = json.loads((att / "甲公司/DONE/.sync.json").read_text(encoding="utf-8"))
    entry = manifest["archived"]["《登记公告通知书》.pdf"]
    assert entry["sha256"] == files[NOTICE]
    assert entry["size"] == 6 and entry["archived_at"].startswith("2026-09-27")
    assert manifest["files"] == ["国家局登记证书.pdf", "浙江省登记证书.pdf"]
    assert manifest["sources"] == {"《登记公告通知书》.pdf": "path:/abc.pdf"}  # identity kept for the sync


@pytest.mark.parametrize("name", ["国家局登记证书.pdf", "浙江省登记证书.pdf"])
def test_a_current_certificate_waits_its_own_days(server, name):
    """The console's 交付包 hands certificates out, so the server keeps them
    longer than notices, by its own settings whatever the laptop sends. DONE
    was finished 200 days ago: past 180, not past 365."""
    att, files = server
    path = f"甲公司/DONE/{name}"
    kept = _release(files, [path], cert_days=365)
    assert kept["released"] == []
    assert kept["skipped"] == [{"path": path, "reason": "按服务器快照不满足释放条件"}]
    assert (att / path).exists()
    assert "archived" not in json.loads((att / "甲公司/DONE/.sync.json").read_text(encoding="utf-8"))

    gone = _release(files, [path])
    assert gone["released"] == [path] and not (att / path).exists()
    manifest = json.loads((att / "甲公司/DONE/.sync.json").read_text(encoding="utf-8"))
    assert name in manifest["archived"] and name not in manifest["files"]


def test_an_older_certificate_round_may_go(server):
    att, files = server
    old = att / "甲公司/DONE/国家局登记证书_2026-07-19.pdf"
    old.write_bytes(b"old-cert")
    files["甲公司/DONE/国家局登记证书_2026-07-19.pdf"] = hashlib.sha256(b"old-cert").hexdigest()
    result = _release(files, ["甲公司/DONE/国家局登记证书_2026-07-19.pdf"])
    assert result["released"] == ["甲公司/DONE/国家局登记证书_2026-07-19.pdf"]
    assert not old.exists() and (att / "甲公司/DONE/国家局登记证书.pdf").exists()


def test_dry_run_deletes_nothing(server):
    att, files = server
    result = _release(files, [NOTICE], dry_run=True)
    assert result["released"] == [NOTICE] and result["dry_run"] is True
    assert (att / NOTICE).exists()
    assert "archived" not in json.loads((att / "甲公司/DONE/.sync.json").read_text(encoding="utf-8"))


def test_the_server_judges_the_policy_from_its_own_snapshot(server):
    att, files = server
    result = _release(files, ["甲公司/FRESH/《登记公告通知书》.pdf", "甲公司/ACTIVE/《补正通知书》.pdf"])
    assert result["released"] == []
    assert {s["reason"] for s in result["skipped"]} == {"按服务器快照不满足释放条件"}
    assert (att / "甲公司/FRESH/《登记公告通知书》.pdf").exists()
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
    files = dict(files, **{NOTICE: "f" * 64})
    result = _release(files, [NOTICE])
    assert result["released"] == [] and result["skipped"][0]["reason"] == "哈希与本机副本不一致"
    assert (att / NOTICE).exists()


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
    result = _release(files, ["甲公司/DONE/不存在.pdf", NOTICE])
    assert result["skipped"] == [{"path": "甲公司/DONE/不存在.pdf", "reason": "服务器上没有这个文件"}]
    assert result["released"] == [NOTICE]


@pytest.fixture(autouse=True)
def _quiet_logging(monkeypatch):
    monkeypatch.setattr(archive_release, "setup_logging", lambda name: None)


def test_main_needs_the_run_lock(server, monkeypatch, capsys):
    att, files = server
    holder = control.try_acquire(kind="run_all", trigger="daemon")
    try:
        items = [{"path": NOTICE, "sha256": files[NOTICE]}]
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(items)))
        assert archive_release.main([]) == 2
    finally:
        holder.finish("done")
    assert "error" in json.loads(capsys.readouterr().out)
    assert (att / NOTICE).exists()


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
    _mark_archived(att, "甲公司/DONE", ["国家局登记证书.pdf", "《登记公告通知书》.pdf"])
    (att / "甲公司/DONE/国家局登记证书.pdf").write_bytes(b"cert")  # restore copied it back

    result = archive_release.unarchive("甲公司/DONE")
    assert result == {"restored": ["国家局登记证书.pdf"], "still_archived": ["《登记公告通知书》.pdf"]}
    manifest = json.loads((att / "甲公司/DONE/.sync.json").read_text(encoding="utf-8"))
    assert list(manifest["archived"]) == ["《登记公告通知书》.pdf"]
    assert "国家局登记证书.pdf" in manifest["files"]


def test_unarchive_refuses_a_bad_directory(server):
    with pytest.raises(ValueError):
        archive_release.unarchive("../甲公司")


# --- the operation log ---------------------------------------------------------

def _audit_lines(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


def test_a_release_is_logged_once_per_record(server, _isolated_audit, monkeypatch):
    att, files = server
    monkeypatch.setenv("SSH_CLIENT", "198.51.100.7 52114 22")
    _release(files, ["甲公司/DONE/国家局登记证书.pdf", NOTICE,
                     "甲公司/ACTIVE/《补正通知书》_2026-09-01.pdf"])
    lines = _audit_lines(_isolated_audit)
    assert sorted((e["action"], e["company"], e["target"]) for e in lines) == [
        ("archive.release", "甲公司", "ACTIVE"), ("archive.release", "甲公司", "DONE"),
    ]
    done = next(e for e in lines if e["target"] == "DONE")
    assert done["actor"] == "本机归档" and done["via"] == "ssh" and done["ip"] == "198.51.100.7"
    # one line for DONE's two files, the certificate and the notice
    assert done["detail"]["files"] == 2 and done["detail"]["bytes"] == len(b"cert") + len(b"notice")


def test_a_dry_run_logs_nothing(server, _isolated_audit):
    att, files = server
    _release(files, [NOTICE], dry_run=True)
    assert _audit_lines(_isolated_audit) == []


def test_unarchive_is_logged(server, _isolated_audit):
    att, files = server
    _mark_archived(att, "甲公司/DONE", ["国家局登记证书.pdf"])
    (att / "甲公司/DONE/国家局登记证书.pdf").write_bytes(b"cert")
    archive_release.unarchive("甲公司/DONE")
    last = _audit_lines(_isolated_audit)[-1]
    assert last["action"] == "archive.unarchive" and last["detail"]["names"] == ["国家局登记证书.pdf"]


def test_stdout_stays_one_json_line_when_the_log_cannot_be_written(
        server, _isolated_audit, monkeypatch, capsys):
    att, files = server
    _isolated_audit.parent.mkdir(parents=True, exist_ok=True)
    _isolated_audit.write_text("not a directory")
    items = [{"path": "甲公司/DONE/《登记公告通知书》.pdf", "sha256": files["甲公司/DONE/《登记公告通知书》.pdf"]}]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(items, ensure_ascii=False)))
    assert archive_release.main([]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and json.loads(out[0])["released"] == ["甲公司/DONE/《登记公告通知书》.pdf"]


# --- restore --certificates: many records at once, and the space check ---------

def test_unarchive_many_clears_each_record_and_reports_bad_dirs(server, _isolated_audit):
    att, files = server
    _mark_archived(att, "甲公司/DONE", ["国家局登记证书.pdf", "浙江省登记证书.pdf"])
    (att / "甲公司/DONE/国家局登记证书.pdf").write_bytes(b"cert")  # only this one came back

    result = archive_release.unarchive_many(["甲公司/DONE", "../甲公司", "甲公司/NOPE"])
    assert result == {
        "records": {"甲公司/DONE": {"restored": ["国家局登记证书.pdf"], "still_archived": ["浙江省登记证书.pdf"]}},
        "invalid": ["../甲公司", "甲公司/NOPE"],
    }
    assert [e["action"] for e in _audit_lines(_isolated_audit)] == ["archive.unarchive"]


def test_main_unarchive_stdin_holds_the_lock(server, monkeypatch, capsys):
    att, files = server
    _mark_archived(att, "甲公司/DONE", ["国家局登记证书.pdf"])
    (att / "甲公司/DONE/国家局登记证书.pdf").write_bytes(b"cert")
    holder = control.try_acquire(kind="run_all", trigger="daemon")
    try:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(["甲公司/DONE"], ensure_ascii=False)))
        assert archive_release.main(["--unarchive-stdin"]) == 2
    finally:
        holder.finish("done")
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(["甲公司/DONE"], ensure_ascii=False)))
    assert archive_release.main(["--unarchive-stdin"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["records"]["甲公司/DONE"]["restored"] == ["国家局登记证书.pdf"] and out["invalid"] == []


def test_main_unarchive_stdin_wants_a_list(server, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"甲公司/DONE": 1}, ensure_ascii=False)))
    assert archive_release.main(["--unarchive-stdin"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_space_reports_without_the_lock(server, monkeypatch, capsys):
    att, files = server
    holder = control.try_acquire(kind="run_all", trigger="daemon")
    try:
        assert archive_release.main(["--space"]) == 0
    finally:
        holder.finish("done")
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"free_mb", "min_free_mb", "attachments_bytes", "max_total_bytes"}
    assert out["attachments_bytes"] == sum(f.stat().st_size for f in att.rglob("*") if f.is_file())
    assert out["free_mb"] > 0 and out["max_total_bytes"] > 0
