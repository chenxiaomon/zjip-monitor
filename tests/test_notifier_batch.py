"""Aggregated notification tests (no network — senders are stubbed)."""

from datetime import datetime, timezone

import pytest

from src import notifier
from src.differ import DiffResult


def _diff(company, added=0):
    return DiffResult(
        company=company,
        added=[{"dataRegNo": f"R{i}", "dataRegName": "记录", "dataRegAuditStatus": 1}
               for i in range(added)],
        changed=[],
        removed=[],
    )


@pytest.fixture
def sent(monkeypatch):
    calls = {"dingtalk": [], "wechat": [], "email": [], "single": []}
    monkeypatch.setattr(notifier, "_load_notify_cfg", lambda: {
        "dingtalk": {"enabled": True, "webhook": "https://x"},
        "wechat": {"enabled": True, "webhook": "https://x"},
        "email": {"enabled": True},
    })
    monkeypatch.setattr(notifier, "_send_dingtalk_text",
                        lambda title, md, cfg, tag: calls["dingtalk"].append(md) or True)
    monkeypatch.setattr(notifier, "_send_wechat_text",
                        lambda md, cfg, tag: calls["wechat"].append(md) or True)
    monkeypatch.setattr(notifier, "_send_email_text",
                        lambda subject, body, cfg, tag: calls["email"].append(body) or True)
    monkeypatch.setattr(notifier, "notify",
                        lambda diff, ts=None: calls["single"].append(diff.company))
    return calls


def test_batch_sends_one_message_per_channel(sent):
    diffs = {f"公司{i}": _diff(f"公司{i}", added=2) for i in range(5)}
    notifier.notify_batch(diffs, datetime.now(timezone.utc))
    assert len(sent["dingtalk"]) == 1
    assert len(sent["wechat"]) == 1
    assert len(sent["email"]) == 1
    assert sent["single"] == []
    for i in range(5):
        assert f"公司{i}" in sent["dingtalk"][0]


def test_single_company_uses_per_company_format(sent):
    notifier.notify_batch({"公司A": _diff("公司A", added=1)})
    assert sent["single"] == ["公司A"]
    assert sent["dingtalk"] == []


def test_no_changes_sends_nothing(sent):
    notifier.notify_batch({"公司A": _diff("公司A", added=0)})
    assert sent["single"] == []
    assert sent["dingtalk"] == []


def test_oversized_batch_truncated(sent):
    diffs = {f"公司{i}": _diff(f"公司{i}", added=50) for i in range(20)}
    notifier.notify_batch(diffs)
    md = sent["wechat"][0]
    assert len(md.encode("utf-8")) <= notifier._MD_BYTE_LIMIT
    assert "截断" in md
    # email is never truncated
    assert len(sent["email"][0].encode("utf-8")) > notifier._MD_BYTE_LIMIT
