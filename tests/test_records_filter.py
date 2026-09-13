"""/records filtering: status-code type coercion, keyword search, missing accounts."""

from types import SimpleNamespace

import pytest

from web.routes import records


def _acc(company="测试公司", username="u1"):
    return SimpleNamespace(company=company, username=username)


def _snap(recs, fetch=None):
    snap = {"company": "测试公司", "record_count": len(recs), "records": recs}
    if fetch:
        snap["fetch"] = fetch
    return snap


@pytest.fixture(autouse=True)
def _no_attachments(monkeypatch):
    monkeypatch.setattr(records, "list_attachments", lambda company, reg_no: [])


def test_string_status_code_matches_int_filter(monkeypatch):
    """The API sometimes returns dataRegAuditStatus as a string — the filter
    must still match, and the pill CSS class must get the int."""
    recs = [
        {"dataRegNo": "R1", "dataRegName": "甲", "dataRegAuditStatus": "7", "dataRegApplyTime": 1},
        {"dataRegNo": "R2", "dataRegName": "乙", "dataRegAuditStatus": 2, "dataRegApplyTime": 2},
    ]
    monkeypatch.setattr(records, "get_latest_snapshot", lambda company: _snap(recs))

    rows, missing, warnings = records._build_records([_acc()], None, 7)
    assert [r["reg_no"] for r in rows] == ["R1"]
    assert rows[0]["status_code"] == 7  # coerced for the s-pill class
    assert rows[0]["status_label"] == "登记成功"


def test_keyword_search_matches_name_regno_company_case_insensitive(monkeypatch):
    recs = [
        {"dataRegNo": "ZJ2026-001", "dataRegName": "用户行为数据集", "dataRegAuditStatus": 7, "dataRegApplyTime": 3},
        {"dataRegNo": "ZJ2026-002", "dataRegName": "Foo Dataset", "dataRegAuditStatus": 7, "dataRegApplyTime": 2},
        {"dataRegNo": "ZJ2026-003", "dataRegName": "其他", "dataRegAuditStatus": 7, "dataRegApplyTime": 1,
         "dataRegApplyName": "杭州申请人"},
    ]
    monkeypatch.setattr(records, "get_latest_snapshot", lambda company: _snap(recs))
    accs = [_acc()]

    rows, _, _ = records._build_records(accs, None, None, q="行为数据")
    assert [r["reg_no"] for r in rows] == ["ZJ2026-001"]

    rows, _, _ = records._build_records(accs, None, None, q="foo dataset")  # case-insensitive
    assert [r["reg_no"] for r in rows] == ["ZJ2026-002"]

    rows, _, _ = records._build_records(accs, None, None, q="2026-003")  # reg_no substring
    assert [r["reg_no"] for r in rows] == ["ZJ2026-003"]

    rows, _, _ = records._build_records(accs, None, None, q="杭州申请人")  # applicant
    assert [r["reg_no"] for r in rows] == ["ZJ2026-003"]

    rows, _, _ = records._build_records(accs, None, None, q="测试公司")  # company matches all
    assert len(rows) == 3

    rows, _, _ = records._build_records(accs, None, None, q="不存在的关键字")
    assert rows == []


def test_account_without_snapshot_is_reported_not_silent(monkeypatch):
    monkeypatch.setattr(records, "get_latest_snapshot", lambda company: None)
    rows, missing, warnings = records._build_records([_acc()], None, None)
    assert rows == []
    assert [a.username for a in missing] == ["u1"]


def test_fetch_warnings_surface_partial_and_mismatch(monkeypatch):
    recs = [{"dataRegNo": "R1", "dataRegName": "甲", "dataRegAuditStatus": 7, "dataRegApplyTime": 1}]

    monkeypatch.setattr(
        records, "get_latest_snapshot",
        lambda company: _snap(recs, fetch={"reported_total": 20, "fetched_count": 5,
                                           "complete": False, "error": "page 2: boom"}),
    )
    _, _, warnings = records._build_records([_acc()], None, None)
    assert warnings[0]["kind"] == "partial"

    monkeypatch.setattr(
        records, "get_latest_snapshot",
        lambda company: _snap(recs, fetch={"reported_total": 20, "fetched_count": 18,
                                           "complete": True, "error": None}),
    )
    _, _, warnings = records._build_records([_acc()], None, None)
    assert warnings[0]["kind"] == "mismatch"
    assert warnings[0]["reported"] == 20 and warnings[0]["fetched"] == 18

    # Complete + matching count → no warning
    monkeypatch.setattr(
        records, "get_latest_snapshot",
        lambda company: _snap(recs, fetch={"reported_total": 1, "fetched_count": 1,
                                           "complete": True, "error": None}),
    )
    _, _, warnings = records._build_records([_acc()], None, None)
    assert warnings == []
