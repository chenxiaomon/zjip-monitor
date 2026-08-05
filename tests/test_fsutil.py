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
