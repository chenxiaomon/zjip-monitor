"""mtime-cache behaviour of src/settings_cache.py."""

import os

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
