"""archive/release.py: the laptop side of freeing server space.

The laptop picks candidates and re-hashes its own copies; everything it sends
is re-checked by the server. These tests fake the ssh/rsync transport.
"""

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from archive import index, pdftext, release
from archive.config import ArchiveConfig

OLD = int((datetime.now(timezone.utc) - timedelta(days=200)).timestamp() * 1000)
NEW = int((datetime.now(timezone.utc) - timedelta(days=5)).timestamp() * 1000)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(pdftext, "extract_text", lambda path: "")
    root = tmp_path / "arc"
    snaps = root / "snapshots"
    snaps.mkdir(parents=True)
    records = [
        {"dataRegNo": "DONE", "dataRegAuditStatus": 7, "updateTime": OLD},
        {"dataRegNo": "FRESH", "dataRegAuditStatus": 6, "updateTime": NEW},
        {"dataRegNo": "ACTIVE", "dataRegAuditStatus": 2, "updateTime": OLD},
    ]
    (snaps / "甲公司_2026-09-26-1000.json").write_text(json.dumps(
        {"company": "甲公司", "snapshot_time": "2026-09-26T02:00:00+00:00", "records": records},
        ensure_ascii=False), encoding="utf-8")
    for reg, name in [("DONE", "国家局登记证书.pdf"), ("FRESH", "不予登记.pdf"),
                      ("ACTIVE", "补正.pdf"), ("ACTIVE", "补正_2026-09-01.pdf")]:
        d = root / "attachments" / "甲公司" / reg
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(f"{reg}/{name}".encode())
    c = ArchiveConfig(server="root@h", remote="/opt/z", archive_dir=root, ssh_cmd="ssh")
    index.build(root, c.db_path)
    return c


def _fake_transport(monkeypatch, reply):
    calls = []

    def fake_run(argv, input=None, **kw):
        calls.append({"argv": argv, "input": input})
        out = reply(argv, input) if callable(reply) else reply
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out, ensure_ascii=False) + "\n", stderr="")

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    return calls


def test_select_follows_the_policy(cfg):
    picked = {(c["reg_no"], c["filename"]) for c in release.select(cfg.db_path, days=90)}
    assert picked == {("DONE", "国家局登记证书.pdf"), ("ACTIVE", "补正_2026-09-01.pdf")}
    assert release.select(cfg.db_path, days=90, company="乙公司") == []


def test_dry_run_asks_the_server_to_verify_only(cfg, monkeypatch, capsys):
    calls = _fake_transport(monkeypatch, lambda argv, stdin: {
        "released": [i["path"] for i in json.loads(stdin)], "skipped": [], "bytes": 10, "dry_run": True})

    assert release.run_release(cfg) == 0
    (call,) = calls
    assert call["argv"][:2] == ["ssh", "root@h"]
    assert call["argv"][2] == "cd /opt/z && .venv/bin/python scripts/archive_release.py --dry-run"
    sent = json.loads(call["input"])
    assert {i["path"] for i in sent} == {"甲公司/DONE/国家局登记证书.pdf", "甲公司/ACTIVE/补正_2026-09-01.pdf"}
    assert sent[0]["sha256"] == hashlib.sha256(sent[0]["path"].split("/", 1)[1].encode()).hexdigest()
    assert "预演" in capsys.readouterr().out
    assert release.select(cfg.db_path, days=90)  # nothing marked released


def test_yes_records_what_the_server_released(cfg, monkeypatch):
    calls = _fake_transport(monkeypatch, {
        "released": ["甲公司/DONE/国家局登记证书.pdf"],
        "skipped": [{"path": "甲公司/ACTIVE/补正_2026-09-01.pdf", "reason": "哈希与本机副本不一致"}],
        "bytes": 30, "dry_run": False,
    })

    assert release.run_release(cfg, yes=True) == 0
    assert "--dry-run" not in calls[0]["argv"][2]
    left = {(c["reg_no"], c["filename"]) for c in release.select(cfg.db_path, days=90)}
    assert left == {("ACTIVE", "补正_2026-09-01.pdf")}


def test_a_changed_local_copy_is_never_offered(cfg, monkeypatch, capsys):
    (cfg.archive_dir / "attachments/甲公司/DONE/国家局登记证书.pdf").write_bytes(b"tampered")
    calls = _fake_transport(monkeypatch, {"released": [], "skipped": [], "bytes": 0})

    release.run_release(cfg)
    assert [i["path"] for i in json.loads(calls[0]["input"])] == ["甲公司/ACTIVE/补正_2026-09-01.pdf"]
    assert "本机副本缺失或已变" in capsys.readouterr().out


def test_a_server_error_is_reported(cfg, monkeypatch, capsys):
    _fake_transport(monkeypatch, {"error": "另一巡检进行中，稍后再试"})
    assert release.run_release(cfg, yes=True) == 1
    assert "另一巡检进行中" in capsys.readouterr().out
    assert len(release.select(cfg.db_path, days=90)) == 2


def test_restore_sends_files_back_and_clears_the_marks(cfg, monkeypatch):
    _fake_transport(monkeypatch, {"released": ["甲公司/DONE/国家局登记证书.pdf"], "skipped": [], "bytes": 1})
    release.run_release(cfg, yes=True)

    calls = _fake_transport(monkeypatch, lambda argv, stdin: (
        {"restored": ["国家局登记证书.pdf"], "still_archived": []} if argv[0] == "ssh" else {}))
    assert release.run_restore(cfg, "甲公司", "DONE") == 0

    rsync, ssh = calls
    assert rsync["argv"][:3] == ["rsync", "-rt", "--ignore-existing"]  # no -a: the server owns the files
    assert rsync["argv"][-2].endswith("attachments/甲公司/DONE/国家局登记证书.pdf")
    assert rsync["argv"][-1] == "root@h:/opt/z/data/attachments/甲公司/DONE/"
    assert ssh["argv"][2].endswith("scripts/archive_release.py --unarchive '甲公司/DONE'")
    assert ("DONE", "国家局登记证书.pdf") in {(c["reg_no"], c["filename"]) for c in release.select(cfg.db_path, days=90)}
