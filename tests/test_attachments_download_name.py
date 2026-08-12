"""download_name(): 浏览器下载名 = 数据名称_登记号_证书类型,跨记录唯一。"""

import pytest

from web.services import attachments_svc
from web.services.attachments_svc import download_name

SNAP = {
    "records": [
        {"dataRegNo": "浙DR20240001", "dataRegName": "出行数据集"},
        {"dataRegNo": "浙DR2024-0002", "dataRegName": "气象 数据/合集"},
        {"dataRegNo": "浙DR20240003", "dataRegName": ""},
    ]
}


@pytest.fixture
def snap(monkeypatch):
    monkeypatch.setattr(attachments_svc, "get_latest_snapshot", lambda company: SNAP)


def test_hit_builds_full_name(snap):
    assert (
        download_name("某公司", "浙DR20240001", "浙江省登记证书.pdf")
        == "出行数据集_浙DR20240001_浙江省登记证书.pdf"
    )


def test_notification_gets_same_treatment(snap):
    assert (
        download_name("某公司", "浙DR20240001", "补正通知书.pdf")
        == "出行数据集_浙DR20240001_补正通知书.pdf"
    )


def test_illegal_chars_in_data_name_replaced(snap):
    # 数据名称含空格和斜杠;URL 中 reg_no 已被 _safe_name 规范化("-" 保留)
    name = download_name("某公司", "浙DR2024-0002", "国家局登记证书.pdf")
    assert name == "气象_数据_合集_浙DR2024-0002_国家局登记证书.pdf"
    assert "/" not in name and " " not in name


def test_normalized_reg_no_still_matches(monkeypatch):
    snap = {"records": [{"dataRegNo": "浙DR/2024·04", "dataRegName": "工业数据集"}]}
    monkeypatch.setattr(attachments_svc, "get_latest_snapshot", lambda company: snap)
    # URL 里的 reg_no 是 _safe_name 后的形态,应仍能匹配并还原原始登记号
    safe = attachments_svc._safe_name("浙DR/2024·04")
    name = download_name("某公司", safe, "浙江省登记证书.pdf")
    # 匹配成功(拿到数据名称),原始登记号里的 "/" 被清洗,合法字符 "·" 保留
    assert name == "工业数据集_浙DR_2024·04_浙江省登记证书.pdf"


def test_empty_data_name_falls_back_to_reg_no(snap):
    assert (
        download_name("某公司", "浙DR20240003", "浙江省登记证书.pdf")
        == "浙DR20240003_浙江省登记证书.pdf"
    )


def test_record_missing_falls_back(snap):
    assert (
        download_name("某公司", "浙DR9999", "浙江省登记证书.pdf")
        == "浙DR9999_浙江省登记证书.pdf"
    )


def test_snapshot_none_falls_back(monkeypatch):
    monkeypatch.setattr(attachments_svc, "get_latest_snapshot", lambda company: None)
    assert (
        download_name("某公司", "浙DR0001", "国家局登记证书.pdf")
        == "浙DR0001_国家局登记证书.pdf"
    )


def test_no_extension_ok(snap):
    assert (
        download_name("某公司", "浙DR20240001", "浙江省登记证书")
        == "出行数据集_浙DR20240001_浙江省登记证书"
    )
