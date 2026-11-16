"""仪表盘「排序与显示」: 来源分段, the admin's order, 已隐藏 — cards only."""

import json
import re

import pytest

from src import status_store
from src.config_loader import Account
from tests.conftest import ADMIN, GROUP_A_USER, OPERATOR_A
from web.services import audit_log, dash_layout
from web.services import events as ev

ACCOUNTS = [
    Account(company="甲公司", username="ua1", password="p", group="group_a"),
    Account(company="乙公司", username="ua2", password="p", group="group_a"),
    Account(company="丙公司", username="ub1", password="p", group="group_b"),
    Account(company="丁公司", username="uu1", password="p", group=""),
    Account(company="戊公司", username="ux1", password="p", group="g9"),
]


@pytest.fixture
def world(monkeypatch, isolated_settings, isolated_snapshots, tmp_path):
    from web import auth as web_auth

    accounts = [Account(**a.__dict__) for a in ACCOUNTS]
    monkeypatch.setattr(web_auth, "load_accounts", lambda: list(accounts))
    for i, a in enumerate(accounts):
        isolated_snapshots.write(a.company, [
            {"dataRegNo": f"R{i}{j}", "dataRegName": f"{a.company}{j}", "dataRegAuditStatus": 7}
            for j in range(i + 1)
        ])
    statuses = {a.username: {"status": "ok", "company": a.company} for a in accounts}
    monkeypatch.setattr(status_store, "get_all", lambda: statuses)
    monkeypatch.setattr(ev, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ev, "_EVENTS_FILE", tmp_path / "events.jsonl")
    ev._cache.clear()
    return {"accounts": accounts, "statuses": statuses, "snapshots": isolated_snapshots,
            "settings": isolated_settings}


def _cards(html: str) -> list[str]:
    return re.findall(r'class="acc-name truncate">([^<]+)</a>', html)


def _parts(html: str) -> dict[str, list[str]]:
    """The grid's three parts: main (above the folds), hidden, dormant."""
    grid = html[html.index('id="account-grid-section"'):]
    hidden_at = grid.find('id="hidden-cards"')
    dormant_at = grid.find('id="dormant-cards"')
    ends = [i for i in (hidden_at, dormant_at) if i >= 0]
    main_end = min(ends) if ends else len(grid)
    return {
        "main": _cards(grid[:main_end]),
        "hidden": _cards(grid[hidden_at:dormant_at if dormant_at > hidden_at else len(grid)]) if hidden_at >= 0 else [],
        "dormant": _cards(grid[dormant_at:]) if dormant_at >= 0 else [],
    }


def _segments(html: str) -> list[str]:
    return re.findall(r'<span class="acc-seg-name">([^<]+)</span>', html)


def _layout(world) -> dict:
    return ((world["settings"].read().get("display") or {}).get("dashboard")) or {}


def _audit(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


def _save(client, groups, accounts, shown):
    return client.post("/dashboard/layout", data={"group": groups, "account": accounts, "shown": shown},
                       follow_redirects=False)


# --- arrange() ---------------------------------------------------------------

def test_default_order_follows_settings_groups_then_unknown_then_ungrouped(isolated_settings):
    segs = dash_layout.arrange(list(ACCOUNTS), dash_layout.Layout())
    assert [s.key for s in segs] == ["group_a", "group_b", "g9", ""]
    assert [s.label for s in segs] == ["来源一", "来源二", "g9", "未分组"]
    assert [a.username for a in segs[0].items] == ["ua1", "ua2"]  # accounts.enc order


def test_layout_order_wins_and_names_that_no_longer_exist_are_ignored(isolated_settings):
    layout = dash_layout.Layout(group_order=["gone", "", "group_b"], account_order=["ghost", "ua2"])
    segs = dash_layout.arrange(list(ACCOUNTS), layout)
    assert [s.key for s in segs] == ["", "group_b", "group_a", "g9"]
    assert [a.username for a in segs[2].items] == ["ua2", "ua1"]


def test_a_company_added_later_lands_at_the_end_of_its_segment(isolated_settings):
    late = Account(company="己公司", username="ua9", password="p", group="group_a")
    layout = dash_layout.Layout(account_order=["ua2", "ua1"])
    segs = dash_layout.arrange([late, *ACCOUNTS], layout)
    assert [a.username for a in segs[0].items] == ["ua2", "ua1", "ua9"]


def test_a_malformed_section_reads_as_the_default(isolated_settings):
    isolated_settings.write({"display": {"dashboard": {"group_order": "group_b", "hidden": [{"x": 1}]}}})
    assert dash_layout.current().is_default


# --- the dashboard -----------------------------------------------------------

def test_admin_saves_a_layout_and_the_dashboard_follows_it(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *ADMIN)
    before = web_client.get("/partials/metrics").text
    assert _parts(web_client.get("/").text)["main"] == ["甲公司", "乙公司", "丙公司", "戊公司", "丁公司"]

    resp = _save(web_client, ["group_b", "group_a", "g9", ""], ["ub1", "ua2", "ua1", "ux1", "uu1"],
                 ["ub1", "ua2", "ux1", "uu1"])
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    assert _layout(world) == {"group_order": ["group_b", "group_a", "g9", ""],
                              "account_order": ["ub1", "ua2", "ua1", "ux1", "uu1"], "hidden": ["ua1"]}

    page = web_client.get("/").text
    assert _segments(page) == ["来源二", "来源一", "g9", "未分组"]
    assert _parts(page) == {"main": ["丙公司", "乙公司", "戊公司", "丁公司"], "hidden": ["甲公司"], "dormant": []}
    assert "1 家已隐藏" in page and 'href="/dashboard/layout"' in page
    # Only the cards move: every number above them is what it was.
    assert web_client.get("/partials/metrics").text == before

    entry = [e for e in _audit(_isolated_audit) if e["action"] == "settings.dashboard_layout"][-1]
    assert entry["detail"]["hidden"] == [[], ["甲公司"]] and entry["detail"]["reordered"] is True
    line = audit_log.describe(entry)
    assert "来源先后 来源一、来源二、g9、未分组 → 来源二、来源一、g9、未分组" in line
    assert "调整了企业先后" in line and "隐藏 （空） → 甲公司" in line


def test_the_standalone_grid_partial_follows_the_layout_too(world, web_client, login_as):
    login_as(web_client, *ADMIN)
    _save(web_client, ["group_b"], ["ub1"], ["ub1"])
    assert _parts(web_client.get("/partials/account-grid").text)["main"][0] == "丙公司"


def test_resume_cards_lead_their_segment_and_hidden_beats_dormant(world, web_client, login_as):
    world["statuses"]["ua2"]["dormant"] = True  # stopped, but a record is still in review: 建议恢复
    world["snapshots"].write("乙公司", [{"dataRegNo": "OPEN", "dataRegName": "n", "dataRegAuditStatus": 1}])
    world["statuses"]["ub1"]["dormant"] = True  # finished, and hidden
    world["statuses"]["uu1"]["dormant"] = True  # finished
    login_as(web_client, *ADMIN)
    _save(web_client, [], ["ua1", "ua2", "ub1", "uu1", "ux1"], ["ua1", "ua2", "uu1", "ux1"])

    parts = _parts(web_client.get("/").text)
    assert parts["main"] == ["乙公司", "甲公司", "戊公司"]
    assert parts["hidden"] == ["丙公司"] and parts["dormant"] == ["丁公司"]


def test_a_colleague_gets_the_order_without_headings_link_or_editor(world, web_client, login_as):
    login_as(web_client, *ADMIN)
    _save(web_client, ["group_b", "group_a"], ["ua2", "ua1", "ub1", "ux1", "uu1"], ["ua2", "ua1"])
    web_client.cookies.clear()

    login_as(web_client, *GROUP_A_USER)
    page = web_client.get("/").text
    assert _parts(page) == {"main": ["乙公司", "甲公司"], "hidden": [], "dormant": []}
    assert _segments(page) == [] and "/dashboard/layout" not in page
    # The hidden count is theirs alone: none of the three hidden companies is in group_a.
    assert "已隐藏" not in page and "丙公司" not in page

    assert web_client.get("/dashboard/layout").status_code == 403
    assert _save(web_client, ["group_a"], ["ua1"], []).status_code == 403
    assert web_client.post("/dashboard/layout/reset").status_code == 403
    assert _layout(world)["hidden"] == ["ub1", "uu1", "ux1"]


def test_mine_filters_before_the_layout_arranges(world, web_client, login_as):
    world["accounts"][1].owner = OPERATOR_A[0]  # 乙公司
    login_as(web_client, *ADMIN)
    _save(web_client, [], ["ua1", "ua2"], ["ua2"])  # 甲 hidden
    web_client.cookies.clear()

    login_as(web_client, *OPERATOR_A)
    page = web_client.get("/").text
    assert _parts(page) == {"main": ["乙公司"], "hidden": [], "dormant": []}


# --- the editor --------------------------------------------------------------

def test_the_editor_lists_everyone_in_layout_order_with_hidden_unticked(world, web_client, login_as):
    login_as(web_client, *ADMIN)
    _save(web_client, ["", "group_a"], ["uu1", "ua2", "ua1"], ["uu1", "ua2"])
    page = web_client.get("/dashboard/layout").text
    assert re.findall(r'name="group" value="([^"]*)"', page) == ["", "group_a", "group_b", "g9"]
    assert re.findall(r'name="account" value="([^"]*)"', page) == ["uu1", "ua2", "ua1", "ub1", "ux1"]
    assert re.findall(r'name="shown" value="([^"]*)" checked', page) == ["uu1", "ua2", "ub1", "ux1"]
    assert "恢复默认" in page


def test_a_company_missing_from_the_form_is_not_hidden_and_unknown_names_are_dropped(world, web_client, login_as):
    """An editor opened before a company was added lists it nowhere: saving
    must not hide it. Names that do not exist are not stored."""
    login_as(web_client, *ADMIN)
    _save(web_client, ["nope", "group_b"], ["ghost", "ua1"], [])
    assert _layout(world) == {"group_order": ["group_b"], "account_order": ["ua1"], "hidden": ["ua1"]}


def test_saving_the_same_layout_twice_logs_once_and_reset_clears_it(world, web_client, login_as, _isolated_audit):
    login_as(web_client, *ADMIN)
    args = (["group_b"], ["ub1", "ua1"], ["ub1"])
    _save(web_client, *args)
    _save(web_client, *args)
    assert len([e for e in _audit(_isolated_audit) if e["action"] == "settings.dashboard_layout"]) == 1

    resp = web_client.post("/dashboard/layout/reset", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/dashboard/layout"
    assert "dashboard" not in (world["settings"].read().get("display") or {})
    assert world["settings"].read()["groups"] == {"group_a": "来源一", "group_b": "来源二"}
    assert "恢复默认" not in web_client.get("/dashboard/layout").text
    assert web_client.post("/dashboard/layout/reset", follow_redirects=False).status_code == 303
    actions = [e for e in _audit(_isolated_audit) if e["action"] == "settings.dashboard_layout"]
    assert len(actions) == 2 and audit_log.describe(actions[-1]) == "恢复默认"
    assert not [e for e in _audit(_isolated_audit) if e["action"] == "http.unaudited"]


def test_a_broken_settings_file_is_refused_and_left_alone(world, web_client, login_as):
    login_as(web_client, *ADMIN)
    broken = "groups: [unclosed\n"
    world["settings"].path.write_text(broken, encoding="utf-8")
    resp = _save(web_client, ["group_b"], ["ub1"], [])
    assert resp.status_code == 409 and "没有保存" in resp.text
    assert web_client.post("/dashboard/layout/reset").status_code == 409
    assert world["settings"].path.read_text(encoding="utf-8") == broken
