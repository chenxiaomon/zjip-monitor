"""src/differ.py — the sole judge of what counts as a change.

A miss here is a missed notification; a false positive is a 「消失」 alarm on a
record that never went anywhere. The module had no direct test before.
"""

from src.differ import diff_snapshots


def _rec(no, status, *, rid=None, name="数据名称"):
    rec = {"dataRegAuditStatus": status, "dataRegName": name}
    if no is not None:
        rec["dataRegNo"] = no
    if rid is not None:
        rec["id"] = rid
    return rec


def test_first_run_has_no_diff():
    d = diff_snapshots("甲", [_rec("R1", 1, rid=1)], [])
    assert (d.added, d.changed, d.removed) == ([], [], [])
    assert d.has_changes is False
    assert d.summary() == "无变化"


def test_added_changed_removed():
    prev = [_rec("R1", 1, rid=1), _rec("R2", 1, rid=2)]
    curr = [_rec("R1", 7, rid=1), _rec("R3", 1, rid=3)]

    d = diff_snapshots("甲", curr, prev)

    assert [r["dataRegNo"] for r in d.added] == ["R3"]
    assert [r["dataRegNo"] for r in d.removed] == ["R2"]
    assert len(d.changed) == 1
    assert (d.changed[0].reg_no, d.changed[0].old_status_code, d.changed[0].new_status_code) == (
        "R1",
        1,
        7,
    )
    assert d.changed[0].old_status == "待审核" and d.changed[0].new_status == "登记成功"
    assert d.has_changes is True


def test_status_code_type_change_is_not_a_change():
    """Platform sending 7 as the string "7" must not flip the whole account
    into 「状态变更」 (a raw != would report every record)."""
    prev = [_rec("R1", 7, rid=1), _rec("R2", 2, rid=2)]
    curr = [_rec("R1", "7", rid=1), _rec("R2", " 2 ", rid=2)]

    d = diff_snapshots("甲", curr, prev)
    assert d.changed == []
    assert d.has_changes is False


def test_record_that_gains_its_number_is_not_removed_and_added():
    """A pending record without a dataRegNo gets one later — the row id keeps
    it the same record instead of one 消失 plus one 新增."""
    prev = [_rec(None, 1, rid=42)]
    curr = [_rec("DIP2026000000042", 1, rid=42)]

    d = diff_snapshots("甲", curr, prev)
    assert (d.added, d.changed, d.removed) == ([], [], [])


def test_number_gained_together_with_a_status_change_is_one_change():
    prev = [_rec(None, 1, rid=42)]
    curr = [_rec("DIP2026000000042", 3, rid=42)]

    d = diff_snapshots("甲", curr, prev)
    assert d.added == [] and d.removed == []
    assert len(d.changed) == 1
    assert d.changed[0].reg_no == "DIP2026000000042"
    assert (d.changed[0].old_status_code, d.changed[0].new_status_code) == (1, 3)


def test_records_without_any_key_are_skipped_not_invented():
    """No dataRegNo and no id: uncomparable. It must not surface as 新增 or
    as 消失 — both would be fabricated."""
    prev = [_rec("R1", 1, rid=1), _rec(None, 1)]
    curr = [_rec("R1", 1, rid=1), _rec(None, 2)]

    d = diff_snapshots("甲", curr, prev)
    assert (d.added, d.changed, d.removed) == ([], [], [])


def test_id_only_records_still_diff():
    """Snapshots predating dataRegNo assignment still compare by row id."""
    d = diff_snapshots("甲", [_rec(None, 3, rid=7)], [_rec(None, 1, rid=7)])
    assert len(d.changed) == 1
    assert d.changed[0].reg_no == "id:7"
    assert (d.changed[0].old_status_code, d.changed[0].new_status_code) == (1, 3)

    d2 = diff_snapshots("甲", [], [_rec(None, 1, rid=7)])
    assert len(d2.removed) == 1


def test_old_snapshots_without_id_field_still_work():
    """Historical snapshots keyed only by dataRegNo must behave exactly as before."""
    prev = [{"dataRegNo": "R1", "dataRegAuditStatus": 1, "dataRegName": "旧"}]
    curr = [{"dataRegNo": "R1", "dataRegAuditStatus": 7, "dataRegName": "旧"}]

    d = diff_snapshots("甲", curr, prev)
    assert len(d.changed) == 1 and d.added == [] and d.removed == []


def test_summary_lists_every_bucket():
    prev = [_rec("R1", 1, rid=1), _rec("R2", 1, rid=2)]
    curr = [_rec("R1", 7, rid=1), _rec("R3", 1, rid=3)]
    assert diff_snapshots("甲", curr, prev).summary() == "状态变更 1 条；新增 1 条；消失 1 条"
