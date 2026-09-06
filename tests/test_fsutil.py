"""Tests for atomic write helpers (src/fsutil.py)."""

from src.fsutil import atomic_write_bytes, atomic_write_text


def test_atomic_write_text_creates_parents(tmp_path):
    target = tmp_path / "a" / "b" / "f.json"
    atomic_write_text(target, '{"k": 1}')
    assert target.read_text() == '{"k": 1}'


def test_atomic_write_overwrites_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "f.txt"
    atomic_write_text(target, "old")
    atomic_write_text(target, "new")
    assert target.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]


def test_atomic_write_bytes(tmp_path):
    target = tmp_path / "f.bin"
    atomic_write_bytes(target, b"\x00\x01")
    assert target.read_bytes() == b"\x00\x01"


def test_concurrent_atomic_writes_to_same_file_never_fail(tmp_path):
    """Regression: several threads writing the same target at once (the
    heartbeat pattern) must all succeed and leave a valid file behind."""
    import threading
    from src.fsutil import atomic_write_text

    target = tmp_path / "state.json"
    errors: list[BaseException] = []

    def writer(i: int):
        try:
            for _ in range(50):
                atomic_write_text(target, f'{{"n": {i}}}')
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert errors == []
    assert target.read_text().startswith('{"n": ')
    assert list(tmp_path.glob(".*.tmp")) == []  # no temp files left behind
