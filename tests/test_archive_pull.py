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

    assert [j[-1] for j in jobs] == [f"{cfg.archive_dir / n}/" for n in ("attachments", "snapshots", "events")]
    for argv in jobs:
        assert not any(a.startswith("--delete") for a in argv)
        assert argv[:5] == ["rsync", "-az", "--stats", "-e", "ssh -p 22"]

    attachments, snapshots, events = jobs
    assert "--exclude=*.part" in attachments
    assert attachments[-2] == "root@h:/opt/z/data/attachments/"
    for argv, name in ((attachments, "attachments"), (snapshots, "snapshots")):
        assert "--backup" in argv
        assert f"--backup-dir={cfg.archive_dir / '.overwritten' / 'S' / name}" in argv
    assert "--backup" not in events
    assert events[-2] == "root@h:/opt/z/data/events*.jsonl"


def test_reports_only_on_request(tmp_path):
    assert len(pull.plan(_cfg(tmp_path), reports=True)) == 4
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
    assert len(calls) == 3
    log = (cfg.archive_dir / "pull.log").read_text(encoding="utf-8")
    assert "transferred=9\tok" in log


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
