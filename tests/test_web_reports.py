"""/reports: the generate → poll → list chain, pages of 20, delete."""

import os
import time

import pytest

from tests.conftest import ADMIN, GROUP_A_USER
from web.routes import reports


@pytest.fixture
def reports_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "_REPORTS_DIR", tmp_path)
    monkeypatch.setattr(reports, "spawn", lambda coro: coro.close())  # never run the real generator
    reports._gen_status.update(status="idle", msg="")
    now = time.time()
    for i in range(25):
        f = tmp_path / f"report_2026-09-{i + 1:02d}_0900.html"
        f.write_text("<html></html>", encoding="utf-8")
        os.utime(f, (now - i * 60, now - i * 60))
    yield tmp_path
    reports._gen_status.update(status="idle", msg="")


def test_list_is_paged_newest_first(reports_dir, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    html = client.get("/reports").text
    assert html.count("/reports/view/") == 20
    assert "report_2026-09-01_0900.html" in html and "report_2026-09-25_0900.html" not in html
    assert "共 25 份 · 第 1/2 页" in html and "25 份报表" in html

    page2 = client.get("/reports/list?page=2").text
    assert page2.count("/reports/view/") == 5 and "report_2026-09-25_0900.html" in page2


def test_generate_polls_then_the_list_arrives_out_of_band(reports_dir, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    html = client.post("/reports/generate").text
    assert 'id="gen-status"' in html and 'hx-trigger="every 3s"' in html and "生成中" in html
    assert "hx-swap-oob" not in html

    reports._gen_status.update(status="done", msg="")
    html = client.get("/reports/gen-status").text
    assert 'id="gen-status"' in html and "已生成" in html
    assert "hx-trigger" not in html  # polling stops
    assert 'id="reports-list" class="grid gap-3" hx-swap-oob="true"' in html  # the list refreshes itself

    # The next page load starts clean instead of announcing an old result
    page = client.get("/reports").text
    assert "已生成" not in page and "25 份报表" in page
    assert reports._gen_status["status"] == "idle"


def test_error_is_shown_once(reports_dir, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    reports._gen_status.update(status="error", msg="暂无快照数据，请先执行一次巡检")
    html = client.get("/reports/gen-status").text
    assert "生成失败：暂无快照数据" in html and 'hx-swap-oob="true"' in html


def test_page_load_while_running_keeps_polling(reports_dir, web_client, login_as):
    reports._gen_status.update(status="running", msg="")
    html = login_as(web_client, *ADMIN).get("/reports").text
    assert 'hx-trigger="every 3s"' in html and reports._gen_status["status"] == "running"


def test_delete_removes_only_a_real_report(reports_dir, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    (reports_dir / "notes.txt").write_text("x", encoding="utf-8")

    html = client.post("/reports/delete/report_2026-09-01_0900.html?page=1").text
    assert not (reports_dir / "report_2026-09-01_0900.html").exists()
    assert 'id="reports-list"' in html and "共 24 份" in html

    for bad in ("notes.txt", "..%2Fnotes.txt", "missing.html", "%2Fetc%2Fpasswd"):
        assert client.post(f"/reports/delete/{bad}").status_code == 404
    assert (reports_dir / "notes.txt").exists()


def test_reports_stay_admin_only(reports_dir, web_client, login_as):
    client = login_as(web_client, *GROUP_A_USER)
    assert client.get("/reports").status_code == 403
    assert client.post("/reports/delete/report_2026-09-02_0900.html").status_code == 403
    assert (reports_dir / "report_2026-09-02_0900.html").exists()
