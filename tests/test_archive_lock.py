"""archive/lock.py: one archive command at a time (bundles may share)."""

import multiprocessing as mp

import pytest

from archive.lock import ArchiveBusy, archive_lock


def _hold(root, shared, ready, release):
    with archive_lock(root, what="另一个进程", shared=shared):
        ready.set()
        release.wait(10)


@pytest.fixture
def holder(tmp_path):
    """Hold the lock in another process (flock is per open file, and the test
    process must not block itself)."""
    ctx = mp.get_context("spawn")
    procs = []

    def start(shared=False):
        ready, release = ctx.Event(), ctx.Event()
        p = ctx.Process(target=_hold, args=(tmp_path, shared, ready, release))
        p.start()
        assert ready.wait(20)
        procs.append((p, release))
        return release

    yield start
    for p, release in procs:
        release.set()
        p.join(10)


def test_a_second_writer_is_told_who_holds_it(tmp_path, holder):
    holder()
    with pytest.raises(ArchiveBusy, match="另一个进程"):
        with archive_lock(tmp_path, what="archive pull"):
            pass


def test_readers_share(tmp_path, holder):
    holder(shared=True)
    with archive_lock(tmp_path, what="archive bundle", shared=True):
        pass
    with pytest.raises(ArchiveBusy):
        with archive_lock(tmp_path, what="archive pull"):
            pass


def test_waits_then_gives_up(tmp_path, holder):
    holder()
    slept = []
    with pytest.raises(ArchiveBusy):
        with archive_lock(tmp_path, what="定时归档", wait=0.05, poll=0.01, sleep=slept.append):
            pass
    assert slept and all(s <= 0.05 for s in slept)


def test_free_after_use(tmp_path):
    with archive_lock(tmp_path, what="archive pull"):
        pass
    with archive_lock(tmp_path, what="archive index"):
        pass
    assert (tmp_path / ".archive.lock").exists()


def test_the_cli_refuses_while_another_command_runs(tmp_path, holder, monkeypatch, capsys):
    from archive import __main__ as cli
    from archive.config import ArchiveConfig

    cfg = ArchiveConfig(server="root@h", remote="/opt/z", archive_dir=tmp_path, ssh_cmd="ssh")
    monkeypatch.setattr(cli, "load_config", lambda env=None: cfg)
    holder()
    assert cli.main(["index"]) == 1
    assert "另一个归档命令正在运行" in capsys.readouterr().out


def test_a_finished_writer_leaves_no_name_behind(tmp_path):
    with archive_lock(tmp_path, what="archive pull"):
        assert "archive pull" in (tmp_path / ".archive.lock").read_text(encoding="utf-8")
    assert (tmp_path / ".archive.lock").read_text(encoding="utf-8") == ""
    with pytest.raises(RuntimeError):
        with archive_lock(tmp_path, what="archive index"):
            raise RuntimeError("boom")
    assert (tmp_path / ".archive.lock").read_text(encoding="utf-8") == ""
