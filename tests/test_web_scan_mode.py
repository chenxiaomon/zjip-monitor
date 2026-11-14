"""停巡 in the console: the per-row switch, 按在审情况设置, 去平台办理's resume,
and how a stopped account looks on the accounts page, the dashboard and /api."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from src import attachments, status_store
from tests.conftest import ADMIN, API_KEY, OPERATOR_A
from web.routes import accounts as accounts_routes


def _audit(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


def _synced(company, no, status):
    rec = attachments._ATTACHMENTS_DIR / attachments._safe_name(company) / no
    rec.mkdir(parents=True, exist_ok=True)
    (rec / ".sync.json").write_text(json.dumps({"status": status, "synced_at": "2026-11-01T00:00:00+00:00"}),
                                    encoding="utf-8")


@pytest.fixture
def world(fake_accounts, isolated_snapshots, isolated_settings, monkeypatch):
    # 甲 公司 (ua1): finished — 办结. 乙·公司 (ua2): one record still in review.
    # 丙公司 (ub1, the other group): no snapshot yet.
    isolated_snapshots.write("甲 公司", [{"dataRegNo": "A1", "dataRegName": "甲一", "dataRegAuditStatus": 6}])
    _synced("甲 公司", "A1", 6)
    isolated_snapshots.write("乙·公司", [{"dataRegNo": "B1", "dataRegName": "乙一", "dataRegAuditStatus": 2}])
    for u, c in (("ua1", "甲 公司"), ("ua2", "乙·公司")):
        status_store.record_scan(u, c, ok=True)
    monkeypatch.setattr(accounts_routes, "spawn", lambda coro: coro.close())
    return fake_accounts


@pytest.fixture
def admin(world, web_client, login_as):
    return login_as(web_client, *ADMIN)


def _toast(resp):
    return json.loads(resp.headers["hx-trigger"])["toast"]["msg"]


def _dormant(u):
    return status_store.is_dormant(status_store.get_status(u))


# --- the per-row switch ------------------------------------------------------------

def test_admin_stops_a_finished_account_and_resumes_it(admin, _isolated_audit):
    page = admin.get("/accounts").text
    assert "可停巡：全部办结" in page and "按在审情况设置" in page
    resp = admin.post("/accounts/ua1/scan-mode", data={"on": "0"})
    assert resp.status_code == 200 and _dormant("ua1")
    assert "已停巡" in resp.text and "恢复巡检" in resp.text and _toast(resp).startswith("已停巡")
    resp = admin.post("/accounts/ua1/scan-mode", data={"on": "1"})
    assert not _dormant("ua1") and _toast(resp) == "已恢复每轮巡检"
    lines = [e for e in _audit(_isolated_audit) if e["action"] == "account.scan_mode"]
    assert [e["detail"] for e in lines] == [{"scan": ["每轮巡检", "停巡"]}, {"scan": ["停巡", "每轮巡检"]}]
    assert {e["company"] for e in lines} == {"甲 公司"}


def test_unfinished_needs_force_and_no_baseline_is_refused(admin, _isolated_audit):
    resp = admin.post("/accounts/ua2/scan-mode", data={"on": "0"})
    assert not _dormant("ua2") and "1 条在审" in _toast(resp)
    assert "停巡" in resp.text                         # the row comes back, current reasons inside
    resp = admin.post("/accounts/ub1/scan-mode", data={"on": "0", "force": "1"})
    assert not _dormant("ub1") and "还没有巡检过" in _toast(resp)
    assert [e for e in _audit(_isolated_audit) if e["action"] == "account.scan_mode"] == []
    admin.post("/accounts/ua2/scan-mode", data={"on": "0", "force": "1"})
    assert _dormant("ua2")
    (line,) = [e for e in _audit(_isolated_audit) if e["action"] == "account.scan_mode"]
    assert line["detail"] == {"scan": ["每轮巡检", "停巡"], "force": True}


def test_the_row_offers_force_only_through_the_confirm_dialog(admin):
    html = admin.get("/accounts").text
    row = re.search(r'<tr id="row-ua2".*?</tr>', html, re.S).group(0)
    assert 'hx-confirm="这家还没有办结：1 条在审' in row and '"force": "1"' in row
    row = re.search(r'<tr id="row-ua1".*?</tr>', html, re.S).group(0)
    assert "hx-confirm" not in row.split("scan-mode")[1].split("</button>")[0]
    row = re.search(r'<tr id="row-ub1".*?</tr>', html, re.S).group(0)
    assert "scan-mode" not in row                      # no baseline: nothing to stop yet


def test_only_admins_switch(world, web_client, login_as):
    operator = login_as(web_client, *OPERATOR_A)
    assert operator.post("/accounts/ua1/scan-mode", data={"on": "0"}).status_code == 403
    assert operator.get("/accounts/scan-plan").status_code == 403
    assert operator.post("/accounts/scan-plan", data={"stop": ["ua1"]}).status_code == 403
    assert not _dormant("ua1")
    assert "scan-mode" not in operator.get("/accounts").text


# --- 按在审情况设置 ---------------------------------------------------------------------

def test_scan_plan_suggests_and_applies_after_reassessing(admin, isolated_snapshots, _isolated_audit):
    status_store.set_scan_mode("ua2", "乙·公司", dormant=True, by="admin")   # stopped, not done
    form = admin.get("/accounts/scan-plan").text
    assert "建议停巡（1 家）" in form and 'name="stop" value="ua1"' in form
    assert "建议恢复每轮巡检（1 家）" in form and 'name="resume" value="ua2"' in form and "1 条在审" in form
    # Between showing and applying, 甲 got a new application: its stop no longer holds.
    isolated_snapshots.write("甲 公司", [{"dataRegNo": "A1", "dataRegAuditStatus": 6},
                                       {"dataRegNo": "A2", "dataRegAuditStatus": 1}], ts="2026-01-02-0900")
    resp = admin.post("/accounts/scan-plan", data={"stop": ["ua1"], "resume": ["ua2"]})
    assert resp.status_code == 200 and resp.text == ""
    assert not _dormant("ua1") and not _dormant("ua2")
    trigger = json.loads(resp.headers["hx-trigger"])
    assert trigger["accountsRefresh"] is True
    assert trigger["toast"]["msg"] == "已停巡 0 家，恢复每轮巡检 1 家；1 家情况有变，没有改动"
    (line,) = [e for e in _audit(_isolated_audit) if e["action"] == "account.scan_plan"]
    assert line["detail"] == {"resumed": ["乙·公司"], "skipped": ["甲 公司"]}


# --- 去平台办理 resumes ---------------------------------------------------------------

def test_platform_access_resumes_a_stopped_account(world, web_client, login_as, _isolated_audit):
    status_store.set_scan_mode("ua1", "甲 公司", dormant=True, by="admin")
    operator = login_as(web_client, *OPERATOR_A)       # cannot switch, but this is automatic
    resp = operator.post("/accounts/ua1/platform-access", data={"reg_no": "A1"})
    assert resp.status_code == 200 and "已恢复每轮巡检" in resp.text
    assert json.loads(resp.headers["hx-trigger"]) == {"accountsRefresh": True}
    entry = status_store.get_status("ua1")
    assert not status_store.is_dormant(entry) and entry["resumed_reason"] == "去平台办理"
    assert entry["resumed_by"] == OPERATOR_A[0]
    (line,) = _audit(_isolated_audit)[-1:]
    assert line["action"] == "account.reveal" and line["detail"] == {"reg_no": "A1", "scan_resumed": True}
    # An account already scanned every round: the detail stays as it always was.
    resp = operator.post("/accounts/ua1/platform-access", data={"reg_no": "A1"})
    assert "已恢复每轮巡检" not in resp.text and "hx-trigger" not in resp.headers
    assert _audit(_isolated_audit)[-1]["detail"] == {"reg_no": "A1"}


# --- how a stopped account looks ---------------------------------------------------------

def test_stopped_accounts_are_not_failing_or_stale_anywhere(admin, monkeypatch):
    status_store.set_scan_mode("ua1", "甲 公司", dormant=True, by="admin")
    status_store.record_scan("ua1", "甲 公司", ok=False, error="平台 502", kind="scrape_failed")
    expired = {"expires_at": (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()}
    monkeypatch.setattr(accounts_routes, "get_session", lambda u: expired)

    row = re.search(r'<tr id="row-ua1".*?</tr>', admin.get("/accounts").text, re.S).group(0)
    assert "pill pill-neutral\">已过期" in row          # expected for a stopped account, not red
    assert "上次巡检失败" in row                          # the accounts page still says what happened
    assert "已停巡" in row and "恢复巡检" in row

    dash = admin.get("/").text
    card = dash[dash.index("甲 公司"):]
    card = card[:card.index("最后巡检")]
    assert "已停巡 · 数据截至" in card and "上次抽查失败" in card
    assert "上次巡检失败，数据为旧快照" not in card and "数据可能过期" not in card
    assert "每轮巡检 2 家 · 停巡 1 家" in dash

    status = admin.get("/api/status", headers={"X-API-Key": API_KEY}).json()
    a = next(x for x in status["accounts"] if x["company"] == "甲 公司")
    assert (a["stale"], a["scan_failed"], a["scan_partial"], a["dormant"]) == (False, False, False, True)
    assert {"company", "dot", "total", "status_counts", "last_check", "owner", "mine"} <= set(a)


def test_run_button_counts_this_rounds_accounts(admin):
    assert "立即巡检全部" in admin.get("/accounts").text
    status_store.set_scan_mode("ua1", "甲 公司", dormant=True, by="admin")
    status_store.record_scan("ua1", "甲 公司", ok=True)   # swept just now: not due
    assert "立即巡检（2 家）" in admin.get("/accounts").text


def test_a_stop_from_an_older_page_is_refused(admin, isolated_snapshots):
    html = admin.get("/accounts").text
    row = re.search(r'<tr id="row-ua2".*?</tr>', html, re.S).group(0)
    seen = re.search(r'"seen": "([0-9a-f]+)"', row).group(1)
    # A second application arrives after the page was rendered.
    isolated_snapshots.write("乙·公司", [{"dataRegNo": "B1", "dataRegAuditStatus": 2},
                                       {"dataRegNo": "B2", "dataRegAuditStatus": 1}], ts="2026-01-02-0900")
    resp = admin.post("/accounts/ua2/scan-mode", data={"on": "0", "force": "1", "seen": seen})
    assert not _dormant("ua2") and "情况变了（现在：2 条在审）" in _toast(resp)
    fresh = re.search(r'"seen": "([0-9a-f]+)"', resp.text).group(1)
    assert fresh != seen
    admin.post("/accounts/ua2/scan-mode", data={"on": "0", "force": "1", "seen": fresh})
    assert _dormant("ua2")


def test_a_stop_after_a_resume_meanwhile_is_refused(admin, world):
    html = admin.get("/accounts").text
    seen = re.search(r'"seen": "([0-9a-f]+)"', re.search(r'<tr id="row-ua1".*?</tr>', html, re.S).group(0)).group(1)
    # Someone stops 甲 and 去平台办理 resumes it, all while this page was open.
    status_store.set_scan_mode("ua1", "甲 公司", dormant=True, by="other")
    status_store.resume_if_dormant("ua1", "甲 公司", by="opa", reason="去平台办理")
    resp = admin.post("/accounts/ua1/scan-mode", data={"on": "0", "seen": seen})
    assert not _dormant("ua1") and "情况变了" in _toast(resp)


# --- telling the two kinds apart -----------------------------------------------------------

def _row_html(html, u):
    return re.search(rf'<tr id="row-{u}".*?</tr>', html, re.S).group(0)


def test_accounts_page_lists_every_round_first_then_stopped(admin):
    html = admin.get("/accounts").text
    assert "tbl-group" not in html and "row-dormant" not in html   # nothing stopped: the table as it was
    status_store.set_scan_mode("ua1", "甲 公司", dormant=True, by="admin")   # 办结
    status_store.set_scan_mode("ua2", "乙·公司", dormant=True, by="admin")   # forced: 1 条在审
    html = admin.get("/accounts").text
    order = [html.index(s) for s in ("每轮巡检 · 1 家", 'id="row-ub1"', "已停巡 · 2 家",
                                     'id="row-ua2"', 'id="row-ua1"')]
    assert order == sorted(order)          # 建议恢复 heads the 停巡 group
    assert 'class="row-dormant"' in _row_html(html, "ua1") and "row-dormant" not in _row_html(html, "ub1")
    assert "每 7 天顺带抽查一次" in html
    assert "建议恢复：1 条在审" in _row_html(html, "ua2")
    # The same grouping when only the rows are fetched (after a save or a switch).
    rows = admin.get("/accounts/rows").text
    assert rows.index('id="row-ub1"') < rows.index("已停巡 · 2 家") < rows.index('id="row-ua2"')


def test_a_stopped_accounts_token_is_grey_even_while_valid(admin, monkeypatch):
    valid = {"expires_at": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()}
    monkeypatch.setattr(accounts_routes, "get_session", lambda u: valid)
    status_store.set_scan_mode("ua1", "甲 公司", dormant=True, by="admin")
    html = admin.get("/accounts").text
    assert 'pill pill-neutral">有效' in _row_html(html, "ua1")
    assert 'pill pill-good">有效' in _row_html(html, "ua2")


def test_switching_redraws_the_table_so_the_row_changes_group(admin):
    resp = admin.post("/accounts/ua1/scan-mode", data={"on": "0"})
    assert json.loads(resp.headers["hx-trigger"]).get("accountsRefresh") is True
    resp = admin.post("/accounts/ua1/scan-mode", data={"on": "0"})          # nothing changed
    assert "accountsRefresh" not in json.loads(resp.headers["hx-trigger"])
    resp = admin.post("/accounts/ua1/scan-mode", data={"on": "1"})
    assert json.loads(resp.headers["hx-trigger"]).get("accountsRefresh") is True


def test_a_manual_scan_that_resumes_redraws_the_table(admin, monkeypatch):
    status_store.set_scan_mode("ua1", "甲 公司", dormant=True, by="admin")
    monkeypatch.setitem(accounts_routes._scan_tasks, "ua1", "running")
    row = _row_html(admin.get("/accounts").text, "ua1")
    assert 'hx-get="/accounts/ua1/scan-status?was=dormant"' in row
    # Still stopped while polling: just the row.
    assert "hx-trigger" not in admin.get("/accounts/ua1/scan-status?was=dormant").headers
    # The scan found a new application and switched it back on.
    status_store.resume_if_dormant("ua1", "甲 公司", by="scan", reason="新申请 A2")
    accounts_routes._scan_tasks["ua1"] = "done"
    resp = admin.get("/accounts/ua1/scan-status?was=dormant")
    assert json.loads(resp.headers["hx-trigger"]) == {"accountsRefresh": True}
    assert "hx-trigger" not in admin.get("/accounts/ua1/scan-status").headers


def _name(company):
    return f'class="acc-name truncate">{company}</a>'


def _card(html, company):
    start = html.rindex('<div class="card acc', 0, html.index(_name(company)))
    return html[start:html.index("最后巡检", start)]


def test_dashboard_folds_finished_stopped_accounts_away(admin):
    assert 'id="dormant-cards"' not in admin.get("/").text
    status_store.set_scan_mode("ua1", "甲 公司", dormant=True, by="admin")   # 办结
    status_store.record_scan("ua1", "甲 公司", ok=False, error="平台 502", kind="scrape_failed")
    status_store.set_scan_mode("ua2", "乙·公司", dormant=True, by="admin")   # forced: 1 条在审
    dash = admin.get("/").text
    fold = dash[dash.index('id="dormant-cards"'):dash.index("</details>")]
    assert _name("甲 公司") in fold and _name("乙·公司") not in fold and _name("丙公司") not in fold
    assert "已停巡 1 家" in fold and "1 家上次抽查失败" in fold and "每 7 天顺带抽查一次" in fold
    assert "<details" in dash and " open" not in dash[dash.index("<details"):dash.index("<summary")]
    assert "3 家 · 1 家停巡，收在下方" in dash
    # Stopped with something still in review: up top, its real dot, 建议恢复.
    assert dash.index(_name("乙·公司")) < dash.index('id="dormant-cards"')
    yi = _card(dash, "乙·公司")
    assert "已停巡 · 建议恢复：1 条在审" in yi and "dot-grey" not in yi and "acc-dormant" not in yi
    jia = _card(dash, "甲 公司")
    assert "dot-grey" in jia and "acc-dormant" in jia and "已停巡 · 数据截至" in jia
    # The standalone partial draws the same two parts.
    part = admin.get("/partials/account-grid").text
    assert 'id="dormant-cards"' in part and part.index(_name("乙·公司")) < part.index('id="dormant-cards"')


def test_view_board_says_stopped_and_puts_it_last(admin):
    status_store.set_scan_mode("ua1", "甲 公司", dormant=True, by="admin")
    body = admin.get("/view/").text
    assert body.index("乙·公司") < body.index("甲 公司")
    jia = body[body.index("甲 公司"):]
    assert "已停巡 · 数据截至" in jia[:jia.index("acc-foot") + 60]
    yi = body[body.index("乙·公司"):body.index("甲 公司")]
    assert "最后巡检：" in yi and "已停巡" not in yi
