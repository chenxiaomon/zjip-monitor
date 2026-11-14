"""run_all and scan_account_and_notify with 停巡 accounts (src/scan_plan.py).

Only this round's targets are scanned; automatic resumes are persisted and
written to the operation log once, as 系统."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src import attachments, control, main, scan_plan, status_store
from src.config_loader import Account
from src.scraper import FetchResult

ANNOUNCEMENT = "《浙江省数据知识产权登记登记公告通知书》.pdf"


def _accounts(n=4):
    return [Account(company=f"公司{i}", username=f"u{i}", password="x") for i in range(n)]


def _ok(account):
    return {"account": account.username, "company": account.company,
            "records": [], "snapshot_path": None, "error": None}


def _audit(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


def _set(username, **fields):
    """Shape a status entry directly (times, flags) for the scenario."""
    def fn(data):
        data["accounts"].setdefault(username, {}).update(fields)
    status_store._mutate(fn)


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def world(isolated_control_dir, isolated_settings, monkeypatch):
    accs = _accounts()
    scanned: list[str] = []
    reports: list[dict] = []

    def fake_scan(account, cancel=None, *, force_login=False, notify_inline=True):
        scanned.append(account.username)
        return _ok(account)

    monkeypatch.setattr(main, "load_accounts", lambda: accs)
    monkeypatch.setattr(main, "scan_account_and_notify", fake_scan)
    monkeypatch.setattr(main, "generate_report", lambda *a, **k: reports.append(k))
    monkeypatch.setattr(main, "notify_batch", lambda *a, **k: None)
    monkeypatch.setattr(main.random, "uniform", lambda a, b: 0.0)

    class World:
        accounts, settings = accs, isolated_settings
    World.scanned, World.reports = scanned, reports
    return World


def _stop(username, *, last_success_days=1):
    status_store.set_scan_mode(username, dormant=True, by="admin")
    _set(username, status="ok", last_success_at=_ago(last_success_days), last_attempt_at=_ago(last_success_days))


def test_only_this_rounds_targets_are_scanned(world):
    _stop("u1")
    _stop("u2", last_success_days=8)    # due for the weekly sweep
    out = main.run_all(trigger="test")
    assert world.scanned == ["u0", "u2", "u3"]
    assert [r.get("swept", False) for r in out["results"]] == [False, True, False]
    assert world.reports == [{"report_time": world.reports[0]["report_time"], "dormant": 1}]
    assert control.read_state()["status"] == "done"


def test_serial_or_pool_is_decided_by_the_targets(world, monkeypatch):
    world.settings.write({**world.settings.read(), "run": {"serial_max_accounts": 2}})
    for u in ("u1", "u2"):
        _stop(u)

    async def no_pool(*a, **k):
        raise AssertionError("2 targets must run serially")

    monkeypatch.setattr(main, "_run_concurrent", no_pool)
    main.run_all(trigger="test")
    assert world.scanned == ["u0", "u3"]


def test_include_dormant_scans_everyone(world):
    for u in ("u1", "u2"):
        _stop(u)
    main.run_all(trigger="test", include_dormant=True)
    assert world.scanned == ["u0", "u1", "u2", "u3"]


def test_a_planner_failure_scans_everyone(world, monkeypatch):
    _stop("u1")

    def broken(*a, **k):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(scan_plan, "plan_round", broken)
    main.run_all(trigger="test")
    assert world.scanned == ["u0", "u1", "u2", "u3"]


def test_a_round_with_nothing_to_scan_writes_no_report_but_finalizes(world, monkeypatch):
    finalized = []
    monkeypatch.setattr(main, "_record_daily_stats", lambda: finalized.append(True))
    for a in world.accounts:
        _stop(a.username)
    out = main.run_all(trigger="test")
    assert world.scanned == [] and out["results"] == [] and world.reports == []
    assert finalized == [True] and control.read_state()["status"] == "done"


def test_renewal_window_resume_is_saved_logged_and_scanned(world, isolated_snapshots, _isolated_audit):
    today = date.today()
    valid_to = (today + timedelta(days=100)).isoformat()   # window opened 80 days ago
    isolated_snapshots.write("公司1", [{"dataRegNo": "R1", "dataRegAuditStatus": 7}])
    rec = attachments._ATTACHMENTS_DIR / attachments._safe_name("公司1") / "R1"
    rec.mkdir(parents=True)
    (rec / ".sync.json").write_text(json.dumps({"status": 7, "synced_at": _ago(1), "facts": {
        ANNOUNCEMENT: {"kind": "公告", "valid_from": "2024-01-01", "valid_to": valid_to}}}), encoding="utf-8")
    _stop("u1")
    _set("u1", dormant_since=_ago(120))
    main.run_all(trigger="test")
    assert "u1" in world.scanned
    assert not status_store.is_dormant(status_store.get_status("u1"))
    (line,) = [e for e in _audit(_isolated_audit) if e["action"] == "account.scan_resume"]
    assert (line["actor"], line["via"], line["target"], line["company"]) == ("系统", "system", "u1", "公司1")
    assert line["detail"] == {"reason": f"证书 {valid_to} 到期，进入续展期"}
    # Next round: nothing more to resume, nothing more logged.
    main.run_all(trigger="test")
    assert len([e for e in _audit(_isolated_audit) if e["action"] == "account.scan_resume"]) == 1


# --- the resume hook inside a real scan ------------------------------------------------

@pytest.fixture
def scan(isolated_snapshots, monkeypatch):
    acc = Account(company="甲公司", username="ua", password="x")
    monkeypatch.setattr(main, "_EVENTS_FILE", isolated_snapshots.dir.parent / "events.jsonl")
    monkeypatch.setattr(main, "get_token", lambda *a, **k: "tok")
    monkeypatch.setattr(main, "has_valid_cached_token", lambda u: True)
    monkeypatch.setattr(main, "notify", lambda *a, **k: None)
    monkeypatch.setattr(main, "sync_attachments", lambda *a, **k: [])
    monkeypatch.setattr(main, "load_accounts", lambda: [acc])

    def run(previous, current):
        isolated_snapshots.write(acc.company, previous)
        monkeypatch.setattr(main, "fetch_all_records",
                            lambda tok, cancel=None: FetchResult(records=current, reported_total=len(current)))
        return main.scan_account_and_notify(acc)

    run.acc = acc
    return run


DONE = {"dataRegNo": "R1", "dataRegName": "一", "dataRegAuditStatus": 7}


def test_a_sweep_that_finds_a_new_application_resumes(scan, _isolated_audit):
    status_store.set_scan_mode("ua", "甲公司", dormant=True, by="admin")
    scan([DONE], [DONE, {"dataRegNo": "N1", "dataRegName": "新", "dataRegAuditStatus": 1}])
    entry = status_store.get_status("ua")
    assert not status_store.is_dormant(entry) and entry["resumed_reason"] == "新申请 N1"
    (line,) = [e for e in _audit(_isolated_audit) if e["action"] == "account.scan_resume"]
    assert line["actor"] == "系统" and line["detail"] == {"reason": "新申请 N1"}


def test_nothing_new_stays_stopped_and_active_accounts_log_nothing(scan, _isolated_audit):
    status_store.set_scan_mode("ua", "甲公司", dormant=True, by="admin")
    scan([DONE], [DONE, {"dataRegNo": "R2", "dataRegName": "二", "dataRegAuditStatus": 7}])
    assert status_store.is_dormant(status_store.get_status("ua"))
    status_store.set_scan_mode("ua", "甲公司", dormant=False, by="admin")
    scan([DONE], [DONE, {"dataRegNo": "N1", "dataRegAuditStatus": 2}])   # active: nothing to resume
    assert [e for e in _audit(_isolated_audit) if e["action"] == "account.scan_resume"] == []
