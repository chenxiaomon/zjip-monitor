"""/partials/todo and the records-page deadline pill: scoped to what the viewer may see."""

import json

import pytest

from src import attachments
from tests.conftest import ADMIN, GROUP_B_USER

NOTICE = "《浙江省数据知识产权登记形式审查补正通知书》.pdf"


@pytest.fixture
def pending(fake_accounts, isolated_snapshots):
    """甲 公司 (group_a) has a 待补正 with a parsed deadline; 丙公司 (group_b) has none."""
    isolated_snapshots.write("甲 公司", [
        {"dataRegNo": "DIP-A1", "dataRegName": "甲的数据", "dataRegAuditStatus": 2, "dataRegApplyTime": 1},
    ])
    isolated_snapshots.write("丙公司", [
        {"dataRegNo": "DIP-B1", "dataRegName": "丙的数据", "dataRegAuditStatus": 7, "dataRegApplyTime": 1},
    ])
    rec = attachments._ATTACHMENTS_DIR / attachments._safe_name("甲 公司") / "DIP-A1"
    rec.mkdir(parents=True)
    (rec / ".sync.json").write_text(json.dumps({"status": 2, "facts": {
        NOTICE: {"kind": "补正", "deadline": "2099-10-12", "issued_on": "2099-09-21",
                 "reasons": "针对：风险分级区间边界存在空档\n备注：无"},
    }}, ensure_ascii=False), encoding="utf-8")


def test_admin_sees_the_correction_with_its_deadline(pending, web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/partials/todo").text
    assert "待补正 1" in html
    assert "截止 10-12 · 剩" in html
    assert "甲 公司 · 甲的数据" in html and "DIP-A1" in html
    assert "针对：风险分级区间边界存在空档" in html
    assert "180 天内没有到期的证书" in html


def test_group_user_only_sees_their_own(pending, web_client, login_as):
    html = login_as(web_client, *GROUP_B_USER).get("/partials/todo").text
    assert "待补正 0" in html and "DIP-A1" not in html


def test_dashboard_page_includes_the_todo_section(pending, web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/").text
    assert 'id="todo-section"' in html and "DIP-A1" in html


def test_records_page_shows_the_deadline_pill(pending, web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/partials/records-table?q=DIP-A1").text
    assert "截止 10-12 · 剩" in html
    assert 'title="针对：风险分级区间边界存在空档' in html
