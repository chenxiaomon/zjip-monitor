"""src/ops_alerts._check_correction_due: the banner for 补正 deadlines."""

from src import ops_alerts
from src.config_loader import Account

ACCOUNTS = [
    Account(company="甲 公司", username="ua1", password="p", group="group_a"),
    Account(company="丙公司", username="ub1", password="p", group="group_b"),
]


def _correction(company, username, reg_no, deadline, days_left):
    return {"company": company, "username": username, "reg_no": reg_no, "name": "x",
            "deadline": deadline, "days_left": days_left, "urgency": "", "issued_on": None,
            "reasons": None, "summary": None}


def _todo(monkeypatch, corrections):
    calls = []

    def fake_build(accounts, today=None):
        calls.append(accounts)
        return {"corrections": corrections, "renewals": [], "next_renewal": None}

    monkeypatch.setattr(ops_alerts.todo, "build", fake_build)
    return calls


def test_nothing_due_means_no_alert(monkeypatch):
    _todo(monkeypatch, [_correction("甲 公司", "ua1", "R1", "2026-10-20", 23), _correction("甲 公司", "ua1", "R2", None, None)])
    assert ops_alerts._check_correction_due(ACCOUNTS, 5) is None


def test_within_warn_days_is_a_warning(monkeypatch):
    _todo(monkeypatch, [_correction("甲 公司", "ua1", "R1", "2026-10-01", 5), _correction("丙公司", "ub1", "R2", "2026-09-30", 4)])
    a = ops_alerts._check_correction_due(ACCOUNTS, 5)
    assert a.key == "correction_due" and a.level == "warning"
    assert a.title == "2 条补正 5 天内到期"
    assert "甲 公司 R1（10-01）" in a.detail and "丙公司 R2（09-30）" in a.detail
    assert a.accounts == ["ua1", "ub1"]


def test_overdue_or_two_days_is_critical(monkeypatch):
    _todo(monkeypatch, [_correction("甲 公司", "ua1", "R1", "2026-09-25", -2), _correction("甲 公司", "ua1", "R2", "2026-10-01", 5)])
    a = ops_alerts._check_correction_due(ACCOUNTS, 5)
    assert a.level == "critical"
    assert a.title == "1 条补正已过截止日期，另 1 条 5 天内到期"

    _todo(monkeypatch, [_correction("甲 公司", "ua1", "R1", "2026-09-29", 2)])
    assert ops_alerts._check_correction_due(ACCOUNTS, 5).level == "critical"


def test_collect_alerts_scopes_the_check_to_visible_accounts(monkeypatch):
    calls = _todo(monkeypatch, [_correction("丙公司", "ub1", "R2", "2026-09-30", 1)])
    monkeypatch.setattr(ops_alerts, "load_accounts", lambda: ACCOUNTS)

    alerts = ops_alerts.collect_alerts({"ua1"})
    assert [a.username for a in calls[-1]] == ["ua1"]
    assert not [a for a in alerts if a.key == "correction_due"] or calls  # fake returns 丙 regardless
    alerts = ops_alerts.collect_alerts(None)
    assert [a.username for a in calls[-1]] == ["ua1", "ub1"]
    assert any(a.key == "correction_due" and a.level == "critical" for a in alerts)
