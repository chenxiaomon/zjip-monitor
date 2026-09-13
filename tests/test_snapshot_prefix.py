"""Snapshot filename anchoring: "公司" must not match "公司_分部_…" files."""

import json

from src import main
from web.services import snapshot as snap_svc


def _write(dirpath, stem, company):
    payload = {"company": company, "snapshot_time": "2026-01-01T01:00:00+00:00",
               "record_count": 1, "records": [{"dataRegNo": company}]}
    (dirpath / f"{stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_main_load_latest_snapshot_prefix_anchored(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", tmp_path)
    _write(tmp_path, "公司_2026-01-01-0900", "公司")
    _write(tmp_path, "公司_分部_2026-01-02-0900", "公司_分部")  # later timestamp, other company

    snap = main.load_latest_snapshot("公司")
    assert snap["company"] == "公司"

    snap = main.load_latest_snapshot("公司_分部")
    assert snap["company"] == "公司_分部"


def test_web_get_latest_snapshot_prefix_anchored(monkeypatch, tmp_path):
    monkeypatch.setattr(snap_svc, "_SNAPSHOTS_DIR", tmp_path)
    snap_svc._cache.clear()
    _write(tmp_path, "公司_2026-01-01-0900", "公司")
    _write(tmp_path, "公司_分部_2026-01-02-0900", "公司_分部")

    snap = snap_svc.get_latest_snapshot("公司")
    assert snap["company"] == "公司"

    snap = snap_svc.get_latest_snapshot("公司_分部")
    assert snap["company"] == "公司_分部"


def test_missing_snapshot_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", tmp_path)
    monkeypatch.setattr(snap_svc, "_SNAPSHOTS_DIR", tmp_path)
    snap_svc._cache.clear()
    assert main.load_latest_snapshot("无此公司") is None
    assert snap_svc.get_latest_snapshot("无此公司") is None
