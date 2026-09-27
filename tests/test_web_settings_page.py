"""Settings page saves: unticked boxes, strict read-modify-write, schedule, thresholds."""

import os
import threading
import time
from datetime import datetime, timezone

import pytest

from src import control
from tests.conftest import ADMIN
from web.routes import settings as settings_routes
from web.services import settings_svc


@pytest.fixture
def admin(isolated_settings, isolated_control_dir, web_client, login_as):
    return login_as(web_client, *ADMIN)


# --- checkboxes -------------------------------------------------------------

def test_unticking_headless_is_saved(admin, isolated_settings):
    admin.post("/settings/login", data={"method": "auto", "token_max_age_hours": "12"})
    assert isolated_settings.read()["login"]["headless"] is False
    html = admin.post("/settings/login", data={"method": "auto", "token_max_age_hours": "12", "headless": "on"}).text
    assert isolated_settings.read()["login"]["headless"] is True
    assert "服务器没有显示器" in html


def test_unticking_ssl_is_saved(admin, isolated_settings):
    admin.post("/notify/email", data={"smtp_host": "smtp.example.invalid", "smtp_port": "587"})
    email = isolated_settings.read()["notify"]["email"]
    assert email["use_tls"] is False and email["smtp_port"] == 587


def test_a_refused_email_save_keeps_what_was_typed(admin, isolated_settings):
    html = admin.post("/notify/email", data={
        "enabled": "on", "smtp_host": "smtp.example.invalid", "smtp_user": "me", "smtp_password": "secret-pw",
    }).text
    assert "启用前必须填写" in html and "密码出于安全不回显" in html
    assert 'value="smtp.example.invalid"' in html and 'value="me"' in html
    assert "secret-pw" not in html
    assert "已保存，留空则不修改" not in html        # nothing is stored yet
    assert "smtp_host" not in isolated_settings.read()["notify"].get("email", {})


# --- strict read-modify-write ------------------------------------------------

def test_a_broken_settings_file_is_never_overwritten(admin, isolated_settings):
    isolated_settings.path.write_text("notify: {dingtalk: {webhook: 'https://x'}\nschedule: [broken", encoding="utf-8")
    before = isolated_settings.path.read_text(encoding="utf-8")
    for url, data in (("/settings/login", {"method": "auto", "token_max_age_hours": "12"}),
                      ("/settings/schedule", {"crons": ["0 9 * * *"]}),
                      ("/notify/wechat", {}),
                      ("/settings/thresholds", {"min_failed_accounts": "3", "stale_hours": "36", "correction_warn_days": "5",
                                                "max_consecutive_failures": "3", "min_free_mb": "500", "max_total_mb": "1024"})):
        html = admin.post(url, data=data).text
        assert "格式错误" in html and "本次没有保存" in html, url
    assert isolated_settings.path.read_text(encoding="utf-8") == before


def test_saves_keep_the_file_permissions(admin, isolated_settings):
    os.chmod(isolated_settings.path, 0o600)
    admin.post("/settings/login", data={"method": "http", "token_max_age_hours": "8"})
    assert isolated_settings.path.stat().st_mode & 0o777 == 0o600
    assert isolated_settings.read()["login"]["method"] == "http"


def test_one_editor_at_a_time(isolated_settings):
    order = []
    inside = threading.Event()

    def first():
        with settings_svc.editing() as data:
            inside.set()
            time.sleep(0.2)
            data["a"] = 1
            settings_svc.save_settings(data)
            order.append("first saved")

    def second():
        inside.wait()
        with settings_svc.editing() as data:
            order.append("second read")
            data["b"] = 2
            settings_svc.save_settings(data)

    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert order == ["first saved", "second read"]
    saved = isolated_settings.read()
    assert saved["a"] == 1 and saved["b"] == 2          # neither change was lost


def test_a_null_section_is_treated_as_empty(admin, isolated_settings):
    isolated_settings.write({"schedule": None, "login": None})
    assert admin.get("/settings").status_code == 200
    admin.post("/settings/schedule", data={"crons": ["0 8 * * *"]})
    assert isolated_settings.read()["schedule"]["crons"] == ["0 8 * * *"]


# --- schedule -----------------------------------------------------------------

def test_schedule_rejects_an_empty_list_and_deduplicates(admin, isolated_settings):
    html = admin.post("/settings/schedule", data={"crons": ["", "  "]}).text
    assert "至少保留一个时间点" in html
    assert isolated_settings.read()["schedule"]["crons"] == ["0 9 * * *", "0 18 * * *"]
    admin.post("/settings/schedule", data={"crons": ["0 9 * * *", "0  9 * * *", "0 20 * * *"]})
    assert isolated_settings.read()["schedule"]["crons"] == ["0 9 * * *", "0 20 * * *"]


def test_schedule_shows_next_runs_and_whether_the_daemon_adopted_it(admin, isolated_settings):
    html = admin.get("/settings").text
    assert html.count('<li><span class="mono">') == 5 and "还没有上报" in html
    assert "需重启" not in html and "重启 daemon" not in html

    control.write_schedule_state(["0 9 * * *", "0 18 * * *"], "settings")
    assert "daemon 正在按这份计划运行" in admin.get("/settings").text

    html = admin.post("/settings/schedule", data={"crons": ["0 7 * * *"]}).text
    assert "约 1 分钟内采用" in html and "等待 daemon 读取新计划" in html


def test_next_runs_expose_apscheduler_weekday_numbering():
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)   # a Sunday in Beijing
    runs = settings_routes.next_runs(["0 9 * * 1"], n=3, now=now)
    assert [r["weekday"] for r in runs] == ["二", "二", "二"]     # 1 = Tuesday for APScheduler
    both = settings_routes.next_runs(["0 9 * * *", "0 18 * * *"], n=4, now=now)
    assert [r["when"][-5:] for r in both] == ["09:00", "18:00", "09:00", "18:00"]


# --- thresholds -----------------------------------------------------------------

GOOD = {"min_failed_accounts": "05", "stale_hours": "48.5", "correction_warn_days": "7",
        "max_consecutive_failures": "0", "min_free_mb": "800", "max_total_mb": "2048"}


def test_thresholds_save_into_their_sections(admin, isolated_settings):
    html = admin.post("/settings/thresholds", data=GOOD).text
    assert "已保存，立即生效" in html
    saved = isolated_settings.read()
    assert saved["ops_alerts"] == {"min_failed_accounts": 5, "stale_hours": 48.5, "correction_warn_days": 7}
    assert saved["login"]["max_consecutive_failures"] == 0 and saved["login"]["method"] == "auto"
    assert saved["disk"] == {"min_free_mb": 800} and saved["attachments"] == {"max_total_mb": 2048}
    assert 'value="48.5"' in html and 'value="5"' in html


@pytest.mark.parametrize("field, value, message", [
    ("min_failed_accounts", "0", "不小于 1"),
    ("min_failed_accounts", "1.5", "请填写整数"),
    ("stale_hours", "0", "必须大于 0"),
    ("stale_hours", "abc", "请填写数字"),
    ("stale_hours", "inf", "请填写数字"),
    ("min_free_mb", "999999", "不能超过 51200"),
    ("max_total_mb", "", "不能为空"),
])
def test_bad_thresholds_are_refused_field_by_field(admin, isolated_settings, field, value, message):
    before = isolated_settings.read()
    html = admin.post("/settings/thresholds", data={**GOOD, field: value}).text
    assert message in html and "本次没有保存" in html and 'aria-invalid="true"' in html
    assert f'value="{value}"' in html                  # what was typed stays in the box
    assert isolated_settings.read() == before


def test_settings_page_shows_the_effective_thresholds(admin, isolated_settings):
    isolated_settings.write({"ops_alerts": {"stale_hours": "not a number"}})
    html = admin.get("/settings").text
    assert 'id="th-stale_hours" name="stale_hours" value="36"' in html   # the default actually in force
