"""mtime-cache behaviour of src/settings_cache.py."""

import os

import pytest
from loguru import logger

from src import settings_cache


def _point_at(monkeypatch, path):
    monkeypatch.setattr(settings_cache, "_SETTINGS_FILE", path)
    settings_cache._cache = None


def test_missing_file_gives_empty(tmp_path, monkeypatch):
    _point_at(monkeypatch, tmp_path / "nope.yaml")
    assert settings_cache.get_settings() == {}


def test_cache_hit_and_invalidation_on_mtime(tmp_path, monkeypatch):
    f = tmp_path / "settings.yaml"
    f.write_text("base_url: https://a\n", encoding="utf-8")
    _point_at(monkeypatch, f)

    assert settings_cache.get_settings()["base_url"] == "https://a"

    f.write_text("base_url: https://b\n", encoding="utf-8")
    os.utime(f, (1e9, 2e9))  # force a different mtime regardless of clock resolution
    assert settings_cache.get_settings()["base_url"] == "https://b"


def test_mutating_returned_dict_does_not_poison_cache(tmp_path, monkeypatch):
    f = tmp_path / "settings.yaml"
    f.write_text("login: {headless: true}\n", encoding="utf-8")
    _point_at(monkeypatch, f)

    first = settings_cache.get_settings()
    first["login"]["headless"] = False
    assert settings_cache.get_settings()["login"]["headless"] is True


def test_invalid_yaml_gives_empty(tmp_path, monkeypatch):
    f = tmp_path / "settings.yaml"
    f.write_text("{broken: [", encoding="utf-8")
    _point_at(monkeypatch, f)
    assert settings_cache.get_settings() == {}


# ---------------------------------------------------------------------------
# (mtime_ns, size) key, bad-YAML caching, stat→read→stat consistency
# ---------------------------------------------------------------------------

@pytest.fixture
def warnings_sink():
    """Collect loguru WARNING+ messages emitted during the test."""
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    yield lines
    logger.remove(sink_id)


def test_invalid_yaml_warns_only_once(tmp_path, monkeypatch, warnings_sink):
    """A broken settings.yaml is read on every request by the web console;
    re-parsing (and re-warning) each time would flood the log."""
    f = tmp_path / "settings.yaml"
    f.write_text("{broken: [", encoding="utf-8")
    _point_at(monkeypatch, f)

    for _ in range(3):
        assert settings_cache.get_settings() == {}

    assert len([m for m in warnings_sink if "解析失败" in m]) == 1

    # Fixing the file must still take effect (the empty result is keyed too).
    f.write_text("base_url: https://fixed\n", encoding="utf-8")
    os.utime(f, (1e9, 2e9))
    assert settings_cache.get_settings()["base_url"] == "https://fixed"


def test_size_change_with_identical_mtime_invalidates(tmp_path, monkeypatch):
    """Coarse-mtime filesystems (or two writes in one tick) must not pin stale
    content: the cache key includes st_size as well as st_mtime_ns."""
    f = tmp_path / "settings.yaml"
    f.write_text("base_url: https://a\n", encoding="utf-8")
    os.utime(f, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    _point_at(monkeypatch, f)
    assert settings_cache.get_settings()["base_url"] == "https://a"

    f.write_text("base_url: https://a-longer\n", encoding="utf-8")
    os.utime(f, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    assert settings_cache.get_settings()["base_url"] == "https://a-longer"


def test_writer_landing_between_stat_and_read_is_not_cached_under_old_key(
    tmp_path, monkeypatch
):
    """TOCTOU: if the file is replaced after we stat it but before we read it,
    the new content must not be filed under the old key (which would make the
    cache ignore the change forever, since the key already matches)."""
    f = tmp_path / "settings.yaml"
    f.write_text("base_url: https://old\n", encoding="utf-8")
    os.utime(f, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    _point_at(monkeypatch, f)

    real_key = settings_cache._file_key
    calls: list[int] = []

    def racy_key(path):
        calls.append(1)
        key = real_key(path)
        if len(calls) == 1:
            # The writer lands right after our first stat: what we are about
            # to read is newer than the key we hold.
            path.write_text("base_url: https://new\n", encoding="utf-8")
            os.utime(path, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
        return key

    monkeypatch.setattr(settings_cache, "_file_key", racy_key)

    assert settings_cache.get_settings()["base_url"] == "https://new"
    # initial stat, post-read stat (mismatch), post-re-read stat (match)
    assert len(calls) == 3
    assert settings_cache._cache is not None
    assert settings_cache._cache[0] == real_key(f)

    parses: list[int] = []
    real_load = settings_cache.yaml.safe_load
    monkeypatch.setattr(settings_cache.yaml, "safe_load", lambda s: (parses.append(1), real_load(s))[1])
    assert settings_cache.get_settings()["base_url"] == "https://new"
    assert parses == []  # served from cache under the correct key


def test_file_that_never_settles_is_returned_but_not_cached(tmp_path, monkeypatch):
    f = tmp_path / "settings.yaml"
    f.write_text("base_url: https://x\n", encoding="utf-8")
    _point_at(monkeypatch, f)

    counter = iter(range(1, 1000))
    monkeypatch.setattr(settings_cache, "_file_key", lambda p: (next(counter), 0))

    assert settings_cache.get_settings()["base_url"] == "https://x"
    assert settings_cache._cache is None
