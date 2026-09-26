"""archive/report.py: search and the filing report, over a small archive."""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from archive import index, pdftext, report
from tests.test_archive_pdftext import ANNOUNCEMENT, CORRECTION

BJ = timezone(timedelta(hours=8))


def _ms(y, m, d):
    return int(datetime(y, m, d, 10, tzinfo=BJ).timestamp() * 1000)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    root = tmp_path / "arc"
    (root / "snapshots").mkdir(parents=True)
    records = [
        {"dataRegNo": "R1", "dataRegName": "某日度统计分析数据", "dataRegAuditStatus": 2,
         "dataRegApplyTime": _ms(2026, 8, 28)},
        {"dataRegNo": "R2", "dataRegName": "某港区出运窗口安全评估数据", "dataRegAuditStatus": 7,
         "dataRegApplyTime": _ms(2026, 5, 28)},
    ]
    (root / "snapshots" / "甲公司_2026-09-26-1000.json").write_text(json.dumps(
        {"company": "甲公司", "snapshot_time": "2026-09-26T02:00:00+00:00", "records": records},
        ensure_ascii=False), encoding="utf-8")
    (root / "events").mkdir()
    lines = [
        {"company": "甲公司", "timestamp": "2026-09-01T01:00:00+00:00", "added": [], "removed": [],
         "changed": [{"reg_no": "R1", "old_status_code": 1, "new_status_code": 2}]},
        {"company": "甲公司", "timestamp": "2026-09-22T01:00:00+00:00", "added": [], "removed": [],
         "changed": [{"reg_no": "R1", "old_status_code": 1, "new_status_code": 2},
                     {"reg_no": "R2", "old_status_code": 3, "new_status_code": 7}]},
    ]
    (root / "events" / "events.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    for reg, name, body in [("R1", "《浙江省数据知识产权登记形式审查补正通知书》.pdf", b"c"),
                            ("R2", "《浙江省数据知识产权登记登记公告通知书》.pdf", b"a")]:
        d = root / "attachments" / "甲公司" / reg
        d.mkdir(parents=True)
        (d / name).write_bytes(body)
    monkeypatch.setattr(pdftext, "extract_text",
                        lambda path: {b"c": CORRECTION, b"a": ANNOUNCEMENT}[path.read_bytes()])
    path = root / "archive.sqlite"
    index.build(root, path)
    return path


def test_search_three_or_more_characters_uses_the_index(db_path):
    (hit,) = report.search(db_path, "数据样例")
    assert (hit["reg_no"], hit["kind"], hit["deadline"]) == ("R1", "补正", "2026-10-14")
    assert "数据样例" in hit["snippet"]


def test_search_short_terms_and_kind_filter(db_path):
    assert [h["reg_no"] for h in report.search(db_path, "样例")] == ["R1"]
    assert [h["reg_no"] for h in report.search(db_path, "有效期", kind="公告")] == ["R2"]
    assert report.search(db_path, "有效期", kind="补正") == []
    assert report.search(db_path, 'a"b') == []  # FTS syntax characters are just text


def test_report_sections(db_path):
    text = report.build_report(db_path, today=date(2026, 9, 27))
    assert "企业 1 家，登记申请 2 条" in text
    assert "- 待补正：1" in text and "- 登记成功：1" in text
    assert "样例数据与算法不一致：1 份（100%）" in text
    assert "- 2 轮：1 条申请" in text
    assert "中位 72 天" in text  # 2026-05-28 → 2026-08-08
    assert text.rstrip().endswith("- 无")  # nothing expires within a year of 2026-09-27


def test_report_lists_certificates_due_for_renewal(db_path):
    text = report.build_report(db_path, today=date(2029, 1, 1))
    assert "- 2029-08-08 甲公司 R2 某港区出运窗口安全评估数据" in text


def test_a_renewed_certificate_is_not_reported_as_expiring(db_path):
    """The 公告 states the first term only; 续展登记 extends it by three years.
    Listing renewed records would raise a false alarm (it nearly did, 2026-09)."""
    db = sqlite3.connect(db_path)
    db.execute("UPDATE records SET status_code = 13, status = '续展登记' WHERE reg_no = 'R2'")
    db.commit()
    db.close()

    text = report.build_report(db_path, today=date(2029, 1, 1))
    assert "R2" not in text.split("尚未续展的登记证书")[1]
    assert "已续展的 1 条不在此列" in text


def test_the_two_pending_review_codes_share_one_line(db_path):
    db = sqlite3.connect(db_path)
    db.execute("UPDATE records SET status_code = 12 WHERE reg_no = 'R2'")
    db.execute("UPDATE records SET status_code = 1 WHERE reg_no = 'R1'")
    db.commit()
    db.close()
    assert report.build_report(db_path, today=date(2026, 9, 27)).count("- 待审核：2") == 1
