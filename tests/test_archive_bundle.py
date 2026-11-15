"""python -m archive bundle: the 交付包 from this Mac's archive — the same bytes
as the console's download from the same snapshot and files."""

import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from archive import bundle, index
from archive.config import ArchiveConfig
from src import attachments, pdftext
from src.attachments import _safe_name
from tests.conftest import OPERATOR_A
from tests.test_records_ledger import _ms

REPO = Path(__file__).resolve().parent.parent


def _snap(root, company, records, ts="2026-11-14-1030", when="2026-11-14T02:30:00+00:00"):
    d = root / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{_safe_name(company)}_{ts}.json").write_text(json.dumps(
        {"company": company, "snapshot_time": when, "records": records}, ensure_ascii=False), encoding="utf-8")


def _rec(reg_no, status, name):
    return {"dataRegNo": reg_no, "dataRegName": name, "dataRegAuditStatus": status,
            "dataRegApplyTime": _ms("2026-03-01"), "nationalCertificateNo": f"NO-{reg_no}"}


def _cert(root, company, reg_no, name="国家局登记证书.pdf", archived=None):
    d = root / "attachments" / _safe_name(company) / reg_no
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(f"%PDF {company} {reg_no}".encode())
    manifest = {"status": 7, "synced_at": "2026-11-14T00:00:00+00:00"}
    if archived:
        manifest["archived"] = archived  # the server let it go; this Mac kept it
    (d / ".sync.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def arc(tmp_path):
    root = tmp_path / "arc"
    _snap(root, "甲 公司", [_rec("R1", 7, "甲的数据"), _rec("R2", 2, "甲在审")], ts="2026-11-13-1030")
    _snap(root, "甲 公司", [_rec("R1", 7, "甲的数据"), _rec("R2", 7, "甲的第二份")])
    _snap(root, "甲 公司_分部", [_rec("F1", 7, "分部的数据")])
    _snap(root, "乙·公司", [_rec("W1", 1, "乙在审")])
    _cert(root, "甲 公司", "R1", archived={"国家局登记证书.pdf": {"sha256": "0" * 64}})
    _cert(root, "甲 公司", "R2")
    _cert(root, "甲 公司_分部", "F1")
    (root / "pull.log").write_text(
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\troot@h\ttransferred=3\tok\n", encoding="utf-8")
    attachments._manifest_cache.clear()
    yield ArchiveConfig(server="root@h", remote="/opt/z", archive_dir=root, ssh_cmd="ssh")
    attachments._manifest_cache.clear()


@pytest.mark.parametrize("arg, company", [
    ("甲 公司", "甲 公司"), ("甲_公司", "甲 公司"),       # the name, or its folder name
    ("分部", "甲 公司_分部"), ("乙", "乙·公司"),          # a piece that fits one company
])
def test_resolve_company(arc, arg, company):
    name, path = bundle.resolve_company(arc.archive_dir / "snapshots", arg)
    assert name == company
    assert path.name.endswith("2026-11-14-1030.json")


@pytest.mark.parametrize("arg, says", [("公司", "对得上 3 家"), ("丁", "没有「丁」"), ("", "请给出")])
def test_resolve_company_refuses_what_is_unclear(arc, arg, says):
    with pytest.raises(LookupError, match=says):
        bundle.resolve_company(arc.archive_dir / "snapshots", arg)


def test_bundle_writes_the_package_into_its_own_folder(arc, capsys):
    assert bundle.run_bundle(arc, "甲 公司") == 0
    (zip_path,) = (arc.archive_dir / "交付包").glob("*.zip")
    assert zip_path.name == "数据知识产权登记交付包_甲 公司_20261114.zip"  # 最新快照：两条都已登记成功
    names = zipfile.ZipFile(zip_path).namelist()
    assert [n.split("/", 1)[1] for n in names] == [
        "数据知识产权登记证书清单_甲 公司_20261114.xlsx",
        "证书/甲的数据_R1_国家局登记证书.pdf",  # released from the server, still here
        "证书/甲的第二份_R2_国家局登记证书.pdf",
    ]
    out = capsys.readouterr().out
    assert "国家局证书 2 张" in out and "可能不是最新" not in out
    assert not list((arc.archive_dir / "交付包").glob(".*.part"))


def test_bundle_warns_when_the_archive_is_old(arc, capsys):
    (arc.archive_dir / "pull.log").write_text("2026-01-01T00:00:00+00:00\troot@h\ttransferred=1\tok\n", encoding="utf-8")
    assert bundle.run_bundle(arc, "分部") == 0
    assert "先运行 python -m archive pull" in capsys.readouterr().out


@pytest.mark.parametrize("sub", ["attachments", "snapshots", "events", ".overwritten/x", "audit"])
def test_bundle_never_writes_into_the_archive_data(arc, sub, capsys):
    assert bundle.run_bundle(arc, "甲 公司", out=arc.archive_dir / sub) == 1
    assert "不能放进归档目录" in capsys.readouterr().out
    assert not list(arc.archive_dir.rglob("*.zip"))


def test_a_company_with_nothing_registered(arc, capsys):
    assert bundle.run_bundle(arc, "乙") == 1
    assert "还没有登记成功" in capsys.readouterr().out


def test_index_ignores_the_packages(arc, monkeypatch):
    monkeypatch.setattr(pdftext, "extract_text", lambda path: "")
    index.build(arc.archive_dir, arc.db_path)
    before = index.summary(arc.db_path)
    assert bundle.run_bundle(arc, "甲 公司") == 0
    (arc.archive_dir / "交付包" / "stray.pdf").write_bytes(b"%PDF")
    index.build(arc.archive_dir, arc.db_path)
    assert index.summary(arc.db_path) == before


def test_the_cli_runs_bundle(arc, monkeypatch, tmp_path):
    from archive import __main__ as cli

    monkeypatch.setattr(cli, "load_config", lambda env=None: arc)
    assert cli.main(["bundle", "分部", "--out", str(tmp_path / "out")]) == 0
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["数据知识产权登记交付包_甲 公司_分部_20261114.zip"]


def test_the_console_and_this_mac_give_the_same_bytes(arc, fake_accounts, web_client, login_as, monkeypatch, tmp_path):
    """The server's data/ and the archive hold the same snapshot and files →
    the downloaded zip and the bundled one are identical, byte for byte."""
    from web.routes import records as records_routes
    from web.services import attachments_svc
    from web.services import snapshot as snap_svc

    monkeypatch.setattr(snap_svc, "_SNAPSHOTS_DIR", arc.archive_dir / "snapshots")
    monkeypatch.setattr(attachments_svc, "_ATTACHMENTS_DIR", arc.archive_dir / "attachments")
    monkeypatch.setattr(records_routes, "_HANDOVER_TMP", tmp_path / "tmp")
    monkeypatch.setattr(records_routes, "attachment_disposition", lambda name: "attachment")
    snap_svc._cache.clear()
    login_as(web_client, *OPERATOR_A)
    resp = web_client.get("/records/handover.zip?account=ua1")
    assert resp.status_code == 200, resp.text[:200]

    assert bundle.run_bundle(arc, "甲 公司", out=tmp_path / "mac") == 0
    (mac,) = (tmp_path / "mac").iterdir()
    assert mac.read_bytes() == resp.content
    snap_svc._cache.clear()


def test_bundle_does_not_load_the_web_framework():
    """The Mac's archive tools must not need FastAPI to run."""
    code = (f"import sys; sys.path.insert(0, {str(REPO)!r}); import archive.bundle, archive.schedule; "
            "bad = [m for m in ('fastapi', 'starlette', 'uvicorn') if m in sys.modules]; "
            "sys.exit(f'loaded {bad}' if bad else 0)")
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_a_broken_snapshot_is_a_message(arc, capsys):
    newest = sorted((arc.archive_dir / "snapshots").glob("甲_公司_2026-11-14*.json"))[-1]
    newest.write_text("{not json", encoding="utf-8")
    assert bundle.run_bundle(arc, "甲 公司") == 1
    assert "读不了最新快照" in capsys.readouterr().out
