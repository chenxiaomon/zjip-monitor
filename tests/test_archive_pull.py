"""archive/pull.py + archive/config.py: an append-only copy of the server's data/.

The local archive is the long-term record — the one thing pull must never do
is delete or silently overwrite what is already here.
"""

import subprocess
from pathlib import Path

from archive import pull
from archive.config import ArchiveConfig, load_config


def _cfg(tmp_path):
    return ArchiveConfig(server="root@h", remote="/opt/z", archive_dir=tmp_path / "arc", ssh_cmd="ssh -p 22")


def test_plan_is_append_only(tmp_path):
    cfg = _cfg(tmp_path)
    jobs = pull.plan(cfg, stamp="S")

    assert [j[-1] for j in jobs] == [f"{cfg.archive_dir / n}/"
                                     for n in ("attachments", "snapshots", "events", "audit")]
    for argv in jobs:
        assert not any(a.startswith("--delete") for a in argv)
        assert argv[:6] == ["rsync", "-az", "--stats", "-e", "ssh -p 22", "--timeout=300"]

    attachments, snapshots, events, audit_log = jobs
    assert "--exclude=*.part" in attachments
    assert attachments[-2] == "root@h:/opt/z/data/attachments/"
    for argv, name in ((attachments, "attachments"), (snapshots, "snapshots")):
        assert "--backup" in argv
        assert f"--backup-dir={cfg.archive_dir / '.overwritten' / 'S' / name}" in argv
    assert "--backup" not in events
    assert events[-2] == "root@h:/opt/z/data/events*.jsonl"
    assert "--backup" not in audit_log
    assert audit_log[-2] == "root@h:/opt/z/data/control/audit/"


def test_reports_only_on_request(tmp_path):
    assert len(pull.plan(_cfg(tmp_path), reports=True)) == 5
    assert pull.plan(_cfg(tmp_path), reports=True)[-1][-1].endswith("/reports/")


def _fake_rsync(monkeypatch, codes):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        code = codes.get(argv[-1].rstrip("/").rsplit("/", 1)[-1], 0)
        return subprocess.CompletedProcess(argv, code, stdout="Number of files transferred: 3\n", stderr="boom")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    return calls


def test_run_logs_what_was_transferred(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    calls = _fake_rsync(monkeypatch, {})

    assert pull.run(cfg) == 0
    assert len(calls) == 4
    log = (cfg.archive_dir / "pull.log").read_text(encoding="utf-8")
    assert "transferred=12\tok" in log


def test_run_reports_a_failed_rsync(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    _fake_rsync(monkeypatch, {"snapshots": 23})

    assert pull.run(cfg) == 1
    assert "failed: snapshots" in (cfg.archive_dir / "pull.log").read_text(encoding="utf-8")
    assert "snapshots" in capsys.readouterr().out


def test_config_defaults_and_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ZJIP_ARCHIVE_ENV", raising=False)
    assert load_config(tmp_path / "missing.env").server == "root@104.249.172.32"

    env = tmp_path / "archive.env"
    env.write_text(
        "# comment\n"
        "SERVER=me@example   # trailing comment\n"
        "export REMOTE=/srv/z/\n"
        'SSH_CMD="ssh -p 2222 -o ConnectTimeout=10"\n'
        "ARCHIVE_DIR=~/somewhere\n"
        "UNKNOWN=ignored\n"
        "BROKEN='unterminated\n",
        encoding="utf-8",
    )
    cfg = load_config(env)
    assert cfg.server == "me@example"
    assert cfg.remote == "/srv/z"
    assert cfg.ssh_cmd == "ssh -p 2222 -o ConnectTimeout=10"
    assert cfg.archive_dir == Path("~/somewhere").expanduser()
    assert cfg.db_path == cfg.archive_dir / "archive.sqlite"

    monkeypatch.setenv("ZJIP_ARCHIVE_ENV", str(env))
    assert load_config().server == "me@example"


# --- notices first, retries, new notices -----------------------------------------

def test_the_notice_pass_leaves_certificates_out(tmp_path):
    cfg = _cfg(tmp_path)
    (job,) = pull.plan(cfg, stamp="S", notices_only=True)
    assert job[-2:] == ["root@h:/opt/z/data/attachments/", f"{cfg.archive_dir / 'attachments'}/"]
    assert {"--exclude=*.part", "--exclude=国家局登记证书*", "--exclude=浙江省登记证书*"} <= set(job)
    assert f"--backup-dir={cfg.archive_dir / '.overwritten' / 'S' / 'attachments'}" in job
    assert not any(a.startswith("--delete") for a in job)


def test_vanished_files_are_not_a_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fake_rsync(monkeypatch, {"attachments": 24})
    assert pull.run(cfg) == 0


def test_a_failed_job_is_retried(tmp_path, monkeypatch):
    codes = iter([12, 12, 0])
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, next(codes), stdout="Number of files transferred: 2\n", stderr="x")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    slept = []
    result = pull.pull_jobs(pull.plan(_cfg(tmp_path), notices_only=True), retries=2, sleep=slept.append)
    assert len(calls) == 3 and slept == [60, 300]
    assert result.failed == [] and result.transferred == 2

    codes = iter([12, 12, 12])
    result = pull.pull_jobs(pull.plan(_cfg(tmp_path), notices_only=True), tag="（通知书）", retries=2,
                            sleep=lambda s: None)
    assert result.failed == ["attachments（通知书）"] and "退出码 12" in result.errors[0]


def _doc(root, rel, body):
    f = root / "attachments" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(body)
    return f


def test_new_notices_counts_each_round_once(tmp_path):
    import sqlite3

    root = tmp_path / "arc"
    _doc(root, "甲/R1/《补正通知书》.pdf", b"round-1")
    _doc(root, "甲/R1/国家局登记证书.pdf", b"cert")
    before = pull.notice_files(root)
    assert list(before) == ["attachments/甲/R1/《补正通知书》.pdf"]  # certificates are not notices

    # The server renamed round 1 to a dated name and fetched round 2; a new
    # record got its first notice; another certificate arrived.
    db_path = root / "archive.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE documents (sha256 TEXT)")
    db.execute("INSERT INTO documents VALUES (?)", (pull._sha256(root / "attachments/甲/R1/《补正通知书》.pdf"),))
    db.commit()
    db.close()
    _doc(root, "甲/R1/《补正通知书》_2026-09-01.pdf", b"round-1")
    _doc(root, "甲/R1/《补正通知书》.pdf", b"round-2-longer")
    _doc(root, "甲/R2/《公示通知书》.pdf", b"publicity")
    _doc(root, "甲/R2/国家局登记证书_2026-07-19.pdf", b"old-cert")
    _doc(root, "甲/R2/.sync.json", b"{}")
    _doc(root, "甲/R2/x.pdf.part", b"partial")

    fresh = pull.new_notices(root, before, db_path)
    assert fresh == ["attachments/甲/R1/《补正通知书》.pdf", "attachments/甲/R2/《公示通知书》.pdf"]
    assert pull.new_notices(root, pull.notice_files(root), db_path) == []  # nothing new since


def test_pull_log_gets_the_notice_count(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _fake_rsync(monkeypatch, {})
    pull.run(cfg)
    line = (cfg.archive_dir / "pull.log").read_text(encoding="utf-8").splitlines()[-1]
    assert line.split("\t")[2:] == ["transferred=12", "ok", "notices=0", "via=manual"]


def test_a_hung_transfer_is_ended_and_counted_as_failed(tmp_path, monkeypatch):
    def fake_run(argv, **kw):
        assert kw["timeout"] == pull.RUN_LIMIT_SECONDS
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    result = pull.pull_jobs(pull.plan(_cfg(tmp_path), notices_only=True), retries=1, sleep=lambda s: None)
    assert result.failed == ["attachments"] and "已终止" in result.errors[0]
