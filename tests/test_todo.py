"""src/todo.py: corrections with deadlines and certificates due for renewal.

The deadline exists only in the 补正通知书 (the platform's auditDeadLine is
always null), stored by the scan as .sync.json → facts. A record without
facts must still be listed — with an unknown deadline, never dropped.
"""

import json
from datetime import date

import pytest

from src import attachments, todo
from src.config_loader import Account

TODAY = date(2026, 9, 27)
NOTICE = "《浙江省数据知识产权登记形式审查补正通知书》.pdf"
ANNOUNCEMENT = "《浙江省数据知识产权登记登记公告通知书》.pdf"
ACC = Account(company="甲 公司", username="ua1", password="p", group="group_a")


def _manifest(reg_no, facts, company=ACC.company):
    rec = attachments._ATTACHMENTS_DIR / attachments._safe_name(company) / reg_no
    rec.mkdir(parents=True, exist_ok=True)
    (rec / ".sync.json").write_text(json.dumps({"status": 2, "facts": facts}, ensure_ascii=False), encoding="utf-8")


def _record(reg_no, status, name="某数据"):
    return {"dataRegNo": reg_no, "dataRegAuditStatus": status, "dataRegName": name, "dataRegApplyTime": 1}


@pytest.mark.parametrize("deadline, days, urgency", [
    ("2026-09-26", -1, "overdue"), ("2026-09-27", 0, "red"), ("2026-09-30", 3, "red"),
    ("2026-10-01", 4, "amber"), ("2026-10-04", 7, "amber"), ("2026-10-05", 8, "normal"),
])
def test_correction_deadline_and_urgency(deadline, days, urgency):
    _manifest("R1", {NOTICE: {"kind": "补正", "deadline": deadline, "issued_on": "2026-09-16", "reasons": "针对：字段\n备注：无"}})
    c = todo.correction_for(ACC, _record("R1", 2), TODAY)
    assert (c.deadline, c.days_left, c.urgency) == (deadline, days, urgency)
    assert c.summary == "针对：字段" and c.reasons.startswith("针对：字段")
    assert c.issued_on == "2026-09-16"


def test_correction_without_facts_is_listed_with_unknown_deadline():
    c = todo.correction_for(ACC, _record("R9", 2), TODAY)
    assert c is not None
    assert (c.deadline, c.days_left, c.urgency, c.summary) == (None, None, "unknown", None)


def test_only_the_current_round_counts_not_the_dated_older_one():
    _manifest("R1", {
        NOTICE: {"kind": "补正", "deadline": "2026-10-12"},
        "《浙江省数据知识产权登记形式审查补正通知书》_2026-09-01.pdf": {"kind": "补正", "deadline": "2026-09-14"},
    })
    assert todo.correction_for(ACC, _record("R1", 2), TODAY).deadline == "2026-10-12"


def test_a_record_not_at_correction_status_is_no_correction():
    _manifest("R1", {NOTICE: {"kind": "补正", "deadline": "2026-10-12"}})
    assert todo.correction_for(ACC, _record("R1", 1), TODAY) is None
    assert todo.correction_for(ACC, _record("R1", "2"), TODAY) is not None  # string code coerced


@pytest.mark.parametrize("valid_to, expect_days, expired", [
    ("2027-03-26", 180, False), ("2026-09-20", -7, True), ("2026-10-27", 30, False),
])
def test_renewal_inside_the_window(valid_to, expect_days, expired):
    _manifest("R2", {ANNOUNCEMENT: {"kind": "公告", "valid_from": "2023-01-01", "valid_to": valid_to}})
    r = todo.renewal_for(ACC, _record("R2", 7), TODAY)
    assert (r.valid_to, r.days_left, r.expired) == (valid_to, expect_days, expired)


def test_renewal_ignores_facts_whose_validity_is_impossible():
    """Facts written before the parser refused inverted spans stay on the
    server; the reader must not turn them into '已过期 762 天'."""
    _manifest("R2", {ANNOUNCEMENT: {"kind": "公告", "valid_from": "2024-09-07", "valid_to": "2024-08-26"}})
    assert todo.renewal_for(ACC, _record("R2", 7), TODAY) is None


def test_renewal_outside_the_window_or_already_renewed_is_none():
    _manifest("R2", {ANNOUNCEMENT: {"kind": "公告", "valid_to": "2027-03-27"}})  # 181 days
    assert todo.renewal_for(ACC, _record("R2", 7), TODAY) is None
    _manifest("R3", {ANNOUNCEMENT: {"kind": "公告", "valid_to": "2026-10-01"}})
    assert todo.renewal_for(ACC, _record("R3", 13), TODAY) is None  # 续展登记: a new term
    assert todo.renewal_for(ACC, _record("R4", 7), TODAY) is None   # no 公告 facts


def test_build_sorts_and_names_the_next_renewal(isolated_snapshots):
    isolated_snapshots.write(ACC.company, [
        _record("C-late", 2, "晚"), _record("C-none", 2, "无"), _record("C-early", 2, "早"),
        _record("REG-far", 7, "远"), _record("REG-farther", 7, "更远"), _record("REG-renewed", 13, "已续"),
    ])
    _manifest("C-late", {NOTICE: {"kind": "补正", "deadline": "2026-10-14"}})
    _manifest("C-early", {NOTICE: {"kind": "补正", "deadline": "2026-10-12"}})
    _manifest("REG-far", {ANNOUNCEMENT: {"kind": "公告", "valid_to": "2027-07-26"}})
    _manifest("REG-farther", {ANNOUNCEMENT: {"kind": "公告", "valid_to": "2027-09-01"}})
    _manifest("REG-renewed", {ANNOUNCEMENT: {"kind": "公告", "valid_to": "2026-09-29"}})

    result = todo.build([ACC], TODAY)
    assert [c["reg_no"] for c in result["corrections"]] == ["C-early", "C-late", "C-none"]
    assert result["corrections"][0]["days_left"] == 15
    assert result["renewals"] == []
    assert result["next_renewal"] == {"company": ACC.company, "reg_no": "REG-far", "name": "远", "valid_to": "2027-07-26"}
    assert result["today"] == "2026-09-27"


def test_build_lists_renewals_and_then_names_no_next(isolated_snapshots):
    isolated_snapshots.write(ACC.company, [_record("REG-soon", 7, "近"), _record("REG-far", 7, "远")])
    _manifest("REG-soon", {ANNOUNCEMENT: {"kind": "公告", "valid_to": "2026-12-01"}})
    _manifest("REG-far", {ANNOUNCEMENT: {"kind": "公告", "valid_to": "2028-01-01"}})

    result = todo.build([ACC], TODAY)
    assert [r["reg_no"] for r in result["renewals"]] == ["REG-soon"]
    assert result["next_renewal"] is None


def test_build_with_no_snapshot_is_empty():
    assert todo.build([ACC], TODAY) == {"corrections": [], "renewals": [], "next_renewal": None, "today": "2026-09-27"}


def test_facts_follow_a_rewritten_manifest():
    _manifest("R1", {NOTICE: {"kind": "补正", "deadline": "2026-10-12"}})
    assert todo.correction_for(ACC, _record("R1", 2), TODAY).deadline == "2026-10-12"
    _manifest("R1", {NOTICE: {"kind": "补正", "deadline": "2026-10-20", "extra": 1}})
    assert todo.correction_for(ACC, _record("R1", 2), TODAY).deadline == "2026-10-20"
