"""src/scan_plan.py: 办结, automatic resume, and which accounts a round scans."""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import attachments, scan_plan
from src.config_loader import Account

TODAY = date(2026, 11, 14)
NOW = datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc)   # 09:00 Beijing
ANNOUNCEMENT = "《浙江省数据知识产权登记登记公告通知书》.pdf"
ACC = Account(company="甲 公司", username="ua1", password="p", group="group_a")
OK = {"status": "ok"}


def _rec(no, status, **extra):
    return {"dataRegNo": no, "dataRegName": f"数据{no}", "dataRegAuditStatus": status, **extra}


def _synced(no, status, company=ACC.company, valid_to=None, synced_at=None):
    """The .sync.json a successful documents pass leaves behind."""
    rec = attachments._ATTACHMENTS_DIR / attachments._safe_name(company) / no
    rec.mkdir(parents=True, exist_ok=True)
    facts = {ANNOUNCEMENT: {"kind": "公告", "valid_from": "2024-01-01", "valid_to": valid_to}} if valid_to else {}
    (rec / ".sync.json").write_text(json.dumps({
        "status": status, "synced_at": synced_at or "2026-11-01T00:00:00+00:00", "facts": facts,
    }, ensure_ascii=False), encoding="utf-8")


def _snap(*records, at="2026-11-14T01:00:00+00:00"):
    return {"snapshot_time": at, "records": list(records)}


# --- 办结 ---------------------------------------------------------------------

def test_finished_account_is_done():
    _synced("R1", 7, valid_to="2029-01-01")
    _synced("R2", 6)
    a = scan_plan.assess(ACC, _snap(_rec("R1", 7), _rec("R2", 6), _rec("R3", 5)), OK, today=TODAY)
    assert a.done and a.reasons == [] and a.record_count == 3


@pytest.mark.parametrize("status", [1, 2, 3, 4, 12, 14, 15, 16, 17, 99, None, "x"])
def test_anything_not_terminal_keeps_it_open(status):
    a = scan_plan.assess(ACC, _snap(_rec("R1", status)), OK, today=TODAY)
    assert not a.done and a.reasons == ["1 条在审"]


def test_no_baseline_no_records_and_a_bad_last_scan_are_not_done():
    assert scan_plan.assess(ACC, None, None, today=TODAY).reasons == ["还没有巡检过"]
    assert scan_plan.assess(ACC, _snap(), OK, today=TODAY).reasons == ["暂无登记记录"]
    _synced("R1", 6)
    failed = scan_plan.assess(ACC, _snap(_rec("R1", 6)), {"status": "login_failed"}, today=TODAY)
    assert failed.reasons == ["上次巡检失败（数据截至 11-14）"]
    partial = scan_plan.assess(ACC, _snap(_rec("R1", 6)), {"status": "partial"}, today=TODAY)
    assert partial.reasons == ["上次巡检不完整（数据截至 11-14）"]
    # No status entry (the status file was lost): not assumed fine.
    assert not scan_plan.assess(ACC, _snap(_rec("R1", 6)), None, today=TODAY).done


def test_documents_still_owed():
    # No manifest yet, a manifest from another status, and one the platform touched since.
    _synced("R2", 3)
    _synced("R3", 6, synced_at="2026-11-01T00:00:00+00:00")
    touched = int(datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp() * 1000)
    snap = _snap(_rec("R1", 6), _rec("R2", 6), _rec("R3", 6, updateTime=touched), _rec("R4", 5))
    a = scan_plan.assess(ACC, snap, OK, today=TODAY)
    assert a.docs_pending == 3 and a.reasons == ["3 条文件未下载完"]   # 5 主动撤回 has none to owe
    assert scan_plan.assess(ACC, snap, OK, today=TODAY, docs_check=False).done  # downloads switched off


def test_certificates_in_the_renewal_window_and_unknown_expiry():
    _synced("R1", 7, valid_to="2027-03-01")          # 107 days: in the window
    _synced("R2", 7)                                 # no 公告 facts
    a = scan_plan.assess(ACC, _snap(_rec("R1", 7), _rec("R2", 7)), OK, today=TODAY)
    assert a.reasons == ["证书 2027-03-01 到期，在续展期内"] and a.notes == ["1 张证书到期日未知"]
    _synced("R1", 7, valid_to="2026-10-01")          # expired, still 登记成功
    a = scan_plan.assess(ACC, _snap(_rec("R1", 7)), OK, today=TODAY)
    assert a.reasons == ["证书 2026-10-01 已过期，未见续展"]
    _synced("R1", 7, valid_to="2027-06-01")          # 199 days: not yet
    assert scan_plan.assess(ACC, _snap(_rec("R1", 7)), OK, today=TODAY).done


# --- automatic resume -----------------------------------------------------------

def _change(no, code, label):
    return SimpleNamespace(reg_no=no, new_status_code=code, new_status=label)


def test_resume_on_new_open_records_and_moves_back_into_review():
    diff = SimpleNamespace(added=[_rec("N1", 1)], changed=[])
    assert scan_plan.resume_cause(diff, [], had_baseline=True) == "新申请 N1"
    diff = SimpleNamespace(added=[], changed=[_change("R1", 2, "待补正")])
    assert scan_plan.resume_cause(diff, [], had_baseline=True) == "R1 变为待补正"
    # Finished on arrival, or moving into a finished status: logged, not resumed.
    diff = SimpleNamespace(added=[_rec("N2", 7)], changed=[_change("R1", 13, "续展登记"), _change("R2", 9, "登记撤销")])
    assert scan_plan.resume_cause(diff, [], had_baseline=True) is None
    # No previous snapshot: the diff is empty, any open record counts.
    empty = SimpleNamespace(added=[], changed=[])
    assert scan_plan.resume_cause(empty, [_rec("R1", 3), _rec("R2", 7)], had_baseline=False) == "1 条在审（之前没有快照）"
    assert scan_plan.resume_cause(empty, [_rec("R2", 7)], had_baseline=False) is None


def test_renewal_resume_only_for_windows_opening_after_the_stop():
    _synced("R1", 7, valid_to="2027-03-01")          # window opened 2026-09-02
    snap = _snap(_rec("R1", 7))
    assert scan_plan.renewal_resume_cause(ACC, snap, "2026-08-01T00:00:00+00:00", TODAY) == \
        "证书 2027-03-01 到期，进入续展期"
    # Stopped while the window was already open: the administrator's call.
    assert scan_plan.renewal_resume_cause(ACC, snap, "2026-10-01T00:00:00+00:00", TODAY) is None
    # Not open yet.
    _synced("R1", 7, valid_to="2027-08-01")
    assert scan_plan.renewal_resume_cause(ACC, snap, "2026-08-01T00:00:00+00:00", TODAY) is None


# --- one round --------------------------------------------------------------------

def _accounts(n):
    return [Account(company=f"公司{i}", username=f"u{i}", password="p") for i in range(n)]


def _dormant(last_success=None, last_attempt=None, **extra):
    return {"dormant": True, "dormant_since": "2026-01-01T00:00:00+00:00", "status": "ok",
            "last_success_at": last_success, "last_attempt_at": last_attempt, **extra}


def _plan(accounts, statuses, snaps=None, **kw):
    snaps = snaps or {}
    return scan_plan.plan_round(accounts, statuses, lambda c: snaps.get(c), now=NOW, today=TODAY, **kw)


def test_active_accounts_and_the_weekly_sweep():
    accs = _accounts(5)
    week_ago = (NOW - timedelta(days=8)).isoformat()
    fresh = (NOW - timedelta(days=2)).isoformat()
    statuses = {
        "u1": _dormant(week_ago, week_ago),          # due, tried a week ago
        "u2": _dormant(fresh),                       # swept two days ago
        "u3": _dormant(None),                        # never succeeded: due
        "u4": _dormant(week_ago, login_paused=True), # paused: a sweep would only be skipped
    }
    plan = _plan(accs, statuses, sweep_days=7)
    assert [a.username for a in plan.active] == ["u0"]
    assert plan.dormant_total == 4 and scan_plan.sweep_cap(4, 7) == 1
    assert [a.username for a in plan.swept] == ["u3"]   # never attempted sorts first
    assert [a.username for a in plan.targets] == ["u0", "u3"] and plan.skipped == 3


def test_oldest_attempt_first_and_the_cap():
    accs = _accounts(21)
    old = (NOW - timedelta(days=30)).isoformat()
    statuses = {a.username: _dormant(old, (NOW - timedelta(hours=i)).isoformat()) for i, a in enumerate(accs)}
    plan = _plan(accs, statuses, sweep_days=7)
    assert len(plan.swept) == 3                                     # ceil(21 / 7)
    assert [a.username for a in plan.swept] == ["u20", "u19", "u18"]  # least recently tried first
    assert _plan(accs, statuses, sweep_days=0).swept == []           # 0 = never sweep
    assert len(_plan(accs, statuses, include_dormant=True).targets) == 21


def _ms(dt):
    return int(dt.timestamp() * 1000)


def test_documents_owed_are_retried_every_round_for_a_few_days():
    accs = _accounts(4)
    fresh = (NOW - timedelta(days=1)).isoformat()
    statuses = {a.username: _dormant(fresh) for a in accs}
    statuses["u3"]["login_paused"] = True
    recent, old = _ms(NOW - timedelta(days=1)), _ms(NOW - timedelta(days=10))
    snaps = {
        "公司0": _snap(_rec("R1", 7, updateTime=recent)),   # just became 登记成功, documents owed
        "公司1": _snap(_rec("R9", 7, updateTime=recent)),   # owed nothing
        "公司2": _snap(_rec("R5", 7, updateTime=old)),      # owed for 10 days: the weekly sweep's job
        "公司3": _snap(_rec("R7", 7, updateTime=recent)),   # paused: a scan would only be skipped
    }
    _synced("R9", 7, company="公司1", synced_at=(NOW - timedelta(hours=1)).isoformat())
    plan = _plan(accs, statuses, snaps, sweep_days=7)
    assert [a.username for a in plan.retry] == ["u0"] and plan.swept == []
    assert _plan(accs, statuses, snaps, sweep_days=7, disk_low=True).retry == []
    assert _plan(accs, statuses, snaps, sweep_days=7, docs_check=False).retry == []


def test_due_by_beijing_date_not_to_the_minute():
    accs = _accounts(1)
    # Swept a week ago at 09:05 Beijing; this round starts at 09:00 — due all the same.
    last = datetime(2026, 11, 7, 1, 5, tzinfo=timezone.utc)
    assert [a.username for a in _plan(accs, {"u0": _dormant(last.isoformat())}, sweep_days=7).swept] == ["u0"]
    six_days = datetime(2026, 11, 8, 1, 5, tzinfo=timezone.utc)
    assert _plan(accs, {"u0": _dormant(six_days.isoformat())}, sweep_days=7).swept == []
    # A stamp from the future (a clock that stepped back) must not postpone it forever.
    future = (NOW + timedelta(days=30)).isoformat()
    assert [a.username for a in _plan(accs, {"u0": _dormant(future)}, sweep_days=7).swept] == ["u0"]


def test_one_switch_for_downloads(isolated_settings, monkeypatch):
    def put(att):
        data = isolated_settings.read()
        data.pop("attachments", None)
        if att is not ...:
            data["attachments"] = att
        isolated_settings.write(data)

    for att, expected in ((..., True), (None, True), ({}, True), ({"enabled": True}, True),
                          ({"enabled": False}, False), ({"enabled": 0}, False), ({"enabled": None}, False)):
        put(att)
        assert attachments.enabled() is expected, att
    put(None)   # an empty `attachments:` section: the defaults, not a crash

    class Reached(Exception):
        pass

    def session(token):
        raise Reached(token)

    monkeypatch.setattr(attachments, "_build_session", session)
    with pytest.raises(Reached):   # it got as far as opening a session: downloads are on
        attachments.sync_attachments("tok", "甲", [_rec("R1", 7)])
    put({"enabled": False})
    assert attachments.sync_attachments("tok", "甲", [_rec("R1", 7)]) == []


def test_renewal_window_resume_joins_the_round():
    accs = _accounts(1)
    _synced("R1", 7, company="公司0", valid_to="2027-03-01")
    fresh = (NOW - timedelta(days=1)).isoformat()
    plan = _plan(accs, {"u0": _dormant(fresh, dormant_since="2026-08-01T00:00:00+00:00")},
                 {"公司0": _snap(_rec("R1", 7))})
    assert [a.username for a in plan.active] == ["u0"]
    assert plan.resumes == [(accs[0], "证书 2027-03-01 到期，进入续展期")] and plan.dormant_total == 0


def test_success_threshold():
    active, stopped = {"status": "ok"}, {"dormant": True}
    assert scan_plan.success_threshold_hours({"a": active, "b": stopped}, 36, 7) == 36
    assert scan_plan.success_threshold_hours({"b": stopped}, 36, 7) == 2 * 7 * 24 + 36
    assert scan_plan.success_threshold_hours({"b": stopped}, 36, 0) is None
    assert scan_plan.success_threshold_hours({}, 36, 7) == 36
