"""src/ops_alerts.py — 控制台运维横幅.

钉钉/企业微信/邮件三个渠道在生产上全是 enabled: false，所以这块横幅是运维告警
唯一的出口：每一条检查项的阈值、文案里的数字、以及分组用户看不到别组企业名，
都是这里要钉死的东西。
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from loguru import logger

from src import control, diskstat, notifier, ops_alerts, status_store
from src.config_loader import Account
from tests.conftest import ADMIN, GROUP_A_USER

MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _control(isolated_control_dir):
    """state.json / pause.flag are read by two of the checks — never the real ones."""
    return isolated_control_dir


@pytest.fixture(autouse=True)
def _accounts(monkeypatch):
    """Three accounts with distinct on-disk names: the collision check stays
    quiet unless a test says otherwise. Usernames match tests/conftest's
    `fake_accounts` so the web-level test lines up with this one."""
    monkeypatch.setattr(ops_alerts, "load_accounts", lambda: [
        Account(company="甲 公司", username="ua1", password="p", group="group_a"),
        Account(company="乙·公司", username="ua2", password="p", group="group_a"),
        Account(company="丙公司", username="ub1", password="p", group="group_b"),
    ])


@pytest.fixture(autouse=True)
def _healthy_disk():
    """A disk_stats.json with nothing wrong, so the disk checks never depend on
    how full the machine running the suite happens to be."""
    write_disk_stats()


@pytest.fixture
def ops_logs():
    seen: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: seen.append((m.record["level"].name, m.record["message"])),
        level="INFO",
        filter=lambda r: r["name"] == "src.ops_alerts",
    )
    yield seen
    logger.remove(sink)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def write_disk_stats(
    *,
    low_disk: bool = False,
    free_mb: float = 9000.0,
    min_free_mb: float = 500.0,
    attachments_bytes: int = 0,
    over_limit: bool = False,
    max_total_bytes: int = 1024 * MIB,
) -> None:
    """Drop a disk_stats.json exactly as src.diskstat.write_stats() would."""
    payload = {
        "collected_at": _iso(datetime.now(timezone.utc)),
        "root": {
            "total_bytes": 10000 * MIB,
            "used_bytes": int((10000 - free_mb) * MIB),
            "free_bytes": int(free_mb * MIB),
            "free_mb": free_mb,
            "percent_used": 50.0,
        },
        "dirs": {"attachments": {"bytes": attachments_bytes, "files": 3}},
        "attachments_over_limit": over_limit,
        "attachments_max_total_bytes": max_total_bytes,
        "low_disk": low_disk,
        "min_free_mb": min_free_mb,
    }
    path = diskstat._STATS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    diskstat._read_cache = None


def write_status(entries: dict[str, dict]) -> None:
    path = status_store._STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "accounts": entries}, ensure_ascii=False), encoding="utf-8"
    )
    status_store._read_cache = None


def account_entry(company: str, status: str = "ok", success_hours_ago: float | None = 1.0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "company": company,
        "status": status,
        "last_attempt_at": _iso(now),
        "last_success_at": (
            None if success_hours_ago is None else _iso(now - timedelta(hours=success_hours_ago))
        ),
    }


def by_key(alerts) -> dict[str, ops_alerts.Alert]:
    return {a.key: a for a in alerts}


def write_state(control_dir, **overrides) -> None:
    state = {
        "run_id": "20260904-0900-abcd",
        "kind": "run_all",
        "trigger": "daemon",
        "pid": os.getpid(),
        "started_at": _iso(datetime.now(timezone.utc) - timedelta(hours=28)),
        "heartbeat_at": _iso(datetime.now(timezone.utc) - timedelta(hours=28)),
        "status": "running",
        "progress": {},
        "error": None,
        "finished_at": None,
    }
    state.update(overrides)
    (control_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


# ---------------------------------------------------------------------------
# 基线
# ---------------------------------------------------------------------------

def test_healthy_deployment_has_no_alerts():
    write_status({"ua1": account_entry("甲 公司"), "ub1": account_entry("丙公司")})
    assert ops_alerts.collect_alerts() == []


# ---------------------------------------------------------------------------
# 1. 本轮失败账号过多
# ---------------------------------------------------------------------------

def test_failed_accounts_below_the_threshold_stay_quiet():
    write_status({
        "ua1": account_entry("甲 公司", status="login_failed"),
        "ua2": account_entry("乙·公司", status="timeout"),
        "ub1": account_entry("丙公司"),
    })
    assert "failed_accounts" not in by_key(ops_alerts.collect_alerts())


def test_failed_accounts_alert_names_the_count_and_the_threshold():
    write_status({
        "ua1": account_entry("甲 公司", status="login_failed"),
        "ua2": account_entry("乙·公司", status="scrape_failed"),
        "ub1": account_entry("丙公司", status="platform_error"),
    })
    alert = by_key(ops_alerts.collect_alerts())["failed_accounts"]
    assert alert.level == "critical"
    assert "3 个账号失败" in alert.title
    assert "ops_alerts.min_failed_accounts=3" in alert.detail
    assert "甲 公司" in alert.detail and "丙公司" in alert.detail
    assert sorted(alert.accounts) == ["ua1", "ua2", "ub1"]
    # partial = 抓到了但不完整，不算失败
    assert "「账号」页" in alert.hint


def test_failed_accounts_threshold_comes_from_settings(isolated_settings):
    isolated_settings.write({"ops_alerts": {"min_failed_accounts": 2}})
    write_status({
        "ua1": account_entry("甲 公司", status="error"),
        "ua2": account_entry("乙·公司", status="captcha_required"),
        "ub1": account_entry("丙公司", status="partial"),
    })
    alert = by_key(ops_alerts.collect_alerts())["failed_accounts"]
    assert "2 个账号失败" in alert.title
    assert "ops_alerts.min_failed_accounts=2" in alert.detail
    assert "丙公司" not in alert.detail  # partial 不计入


# ---------------------------------------------------------------------------
# 2. 长时间没有成功巡检
# ---------------------------------------------------------------------------

def test_recent_success_keeps_the_stale_alert_quiet():
    write_status({"ua1": account_entry("甲 公司", success_hours_ago=2)})
    assert "no_recent_success" not in by_key(ops_alerts.collect_alerts())


def test_no_recent_success_alert_names_the_gap_and_the_threshold():
    write_status({
        "ua1": account_entry("甲 公司", success_hours_ago=40),
        "ub1": account_entry("丙公司", success_hours_ago=50),
    })
    alert = by_key(ops_alerts.collect_alerts())["no_recent_success"]
    assert alert.level == "critical"
    assert "40 小时" in alert.title
    assert "ops_alerts.stale_hours=36" in alert.detail
    assert "zjip-daemon" in alert.hint


def test_no_success_at_all_is_its_own_wording():
    write_status({
        "ua1": account_entry("甲 公司", status="error", success_hours_ago=None),
        "ub1": account_entry("丙公司", status="error", success_hours_ago=None),
    })
    alert = by_key(ops_alerts.collect_alerts())["no_recent_success"]
    assert alert.title == "没有任何账号巡检成功过"
    assert "2 个账号" in alert.detail
    assert "ops_alerts.stale_hours=36" in alert.detail


def test_an_empty_status_store_is_a_fresh_install_not_a_stall():
    assert "no_recent_success" not in by_key(ops_alerts.collect_alerts())


# ---------------------------------------------------------------------------
# 3. 磁盘告急
# ---------------------------------------------------------------------------

def test_disk_low_alert_gives_the_free_space_and_the_floor():
    write_disk_stats(low_disk=True, free_mb=120.0, min_free_mb=500.0)
    alert = by_key(ops_alerts.collect_alerts())["disk_low"]
    assert alert.level == "critical"
    assert "剩余 120MB" in alert.detail
    assert "disk.min_free_mb=500MB" in alert.detail
    assert "附件下载已暂停" in alert.detail


def test_disk_low_falls_back_to_a_live_check_without_a_stats_file(monkeypatch):
    diskstat._STATS_FILE.unlink(missing_ok=True)
    diskstat._read_cache = None
    monkeypatch.setattr(
        diskstat, "check_free",
        lambda *a, **k: diskstat.DiskCheck(low=True, free_mb=42.0, min_free_mb=500.0),
    )
    alert = by_key(ops_alerts.collect_alerts())["disk_low"]
    assert "剩余 42MB" in alert.detail and "实时测得" in alert.detail


def test_no_disk_alert_when_the_snapshot_says_there_is_room():
    assert "disk_low" not in by_key(ops_alerts.collect_alerts())


# ---------------------------------------------------------------------------
# 4. 附件目录超量
# ---------------------------------------------------------------------------

def test_attachments_over_limit_alert_says_it_will_not_delete_anything():
    write_disk_stats(attachments_bytes=1500 * MIB, over_limit=True, max_total_bytes=1024 * MIB)
    alert = by_key(ops_alerts.collect_alerts())["attachments_over_limit"]
    assert alert.level == "warning"
    assert "1500MB" in alert.detail
    assert "attachments.max_total_mb=1024MB" in alert.detail
    assert "不会自动删除" in alert.hint and "人工清理" in alert.hint


def test_attachments_under_the_limit_stay_quiet():
    write_disk_stats(attachments_bytes=10 * MIB, over_limit=False)
    assert "attachments_over_limit" not in by_key(ops_alerts.collect_alerts())


# ---------------------------------------------------------------------------
# 5. 巡检进程卡死
# ---------------------------------------------------------------------------

def test_wedged_run_alert(isolated_control_dir):
    write_state(isolated_control_dir)  # 本进程持锁，心跳 28 小时前 → stale + holder_alive
    alert = by_key(ops_alerts.collect_alerts())["run_wedged"]
    assert alert.level == "critical"
    assert f"pid={os.getpid()}" in alert.detail
    assert "20260904-0900-abcd" in alert.detail
    assert "每一轮都会被跳过" in alert.detail
    assert "systemctl restart zjip-daemon" in alert.hint


def test_a_dead_holder_is_not_a_wedge(isolated_control_dir):
    """进程已经死了 = 锁自动释放，下一轮能跑；只有活着但没心跳才是卡死。"""
    write_state(isolated_control_dir, pid=4194303)
    assert "run_wedged" not in by_key(ops_alerts.collect_alerts())


def test_a_finished_run_is_not_a_wedge(isolated_control_dir):
    write_state(isolated_control_dir, status="done", finished_at=_iso(datetime.now(timezone.utc)))
    assert "run_wedged" not in by_key(ops_alerts.collect_alerts())


# ---------------------------------------------------------------------------
# 6. 定时巡检已暂停
# ---------------------------------------------------------------------------

def test_paused_daemon_is_an_info_not_an_alarm():
    control.pause_daemon()
    alert = by_key(ops_alerts.collect_alerts())["daemon_paused"]
    assert alert.level == "info"
    assert "pause.flag" in alert.detail
    assert "「设置」页" in alert.hint


def test_running_daemon_has_no_pause_alert():
    assert "daemon_paused" not in by_key(ops_alerts.collect_alerts())


# ---------------------------------------------------------------------------
# 7. 公司名脱敏后重名
# ---------------------------------------------------------------------------

def test_company_name_collision_alert(monkeypatch):
    monkeypatch.setattr(ops_alerts, "load_accounts", lambda: [
        Account(company="甲 公司", username="ua1", password="p", group="group_a"),
        Account(company="甲·公司", username="ua2", password="p", group="group_a"),
        Account(company="丙公司", username="ub1", password="p", group="group_b"),
    ])
    alert = by_key(ops_alerts.collect_alerts())["company_name_collision"]
    assert alert.level == "warning"
    assert "甲 公司" in alert.detail and "甲·公司" in alert.detail
    assert "甲_公司" in alert.detail          # 磁盘上的目录名
    assert "互相覆盖" in alert.detail
    assert "丙公司" not in alert.detail


def test_distinct_company_names_have_no_collision_alert():
    assert "company_name_collision" not in by_key(ops_alerts.collect_alerts())


def test_a_cross_group_collision_is_not_spelled_out_to_a_group_user(monkeypatch):
    monkeypatch.setattr(ops_alerts, "load_accounts", lambda: [
        Account(company="甲 公司", username="ua1", password="p", group="group_a"),
        Account(company="甲·公司", username="ub1", password="p", group="group_b"),
    ])
    assert "company_name_collision" in by_key(ops_alerts.collect_alerts())
    assert "company_name_collision" not in by_key(ops_alerts.collect_alerts({"ua1"}))


# ---------------------------------------------------------------------------
# 排序与权限过滤
# ---------------------------------------------------------------------------

def test_alerts_are_sorted_most_severe_first():
    write_disk_stats(
        low_disk=True, free_mb=10.0, attachments_bytes=2000 * MIB, over_limit=True
    )
    control.pause_daemon()
    alerts = ops_alerts.collect_alerts()
    assert [a.level for a in alerts] == ["critical", "warning", "info"]
    assert [a.key for a in alerts] == ["disk_low", "attachments_over_limit", "daemon_paused"]


def test_group_scope_counts_and_names_only_its_own_accounts(isolated_settings):
    isolated_settings.write({"ops_alerts": {"min_failed_accounts": 2}})
    write_status({
        "ua1": account_entry("甲 公司", status="login_failed"),
        "ua2": account_entry("乙·公司", status="timeout"),
        "ub1": account_entry("丙公司", status="error"),
    })
    admin = by_key(ops_alerts.collect_alerts())["failed_accounts"]
    assert "3 个账号失败" in admin.title and "丙公司" in admin.detail

    scoped = by_key(ops_alerts.collect_alerts({"ua1", "ua2"}))["failed_accounts"]
    assert "2 个账号失败" in scoped.title
    assert "丙公司" not in scoped.detail
    assert sorted(scoped.accounts) == ["ua1", "ua2"]


def test_system_wide_alerts_stay_visible_to_group_users():
    """磁盘/卡死/暂停与账号无关，分组用户也必须看得到。"""
    write_disk_stats(low_disk=True, free_mb=10.0)
    control.pause_daemon()
    keys = by_key(ops_alerts.collect_alerts({"ua1"}))
    assert "disk_low" in keys and "daemon_paused" in keys


# ---------------------------------------------------------------------------
# 端到端：控制台横幅的分组隔离
# ---------------------------------------------------------------------------

def test_dashboard_banner_never_leaks_another_group(
    web_client, login_as, fake_accounts, isolated_settings
):
    isolated_settings.write({"ops_alerts": {"min_failed_accounts": 1}})
    write_status({
        "ua1": account_entry("甲 公司", status="scrape_failed"),
        "ub1": account_entry("丙公司", status="scrape_failed"),
    })

    login_as(web_client, *ADMIN)
    admin_body = web_client.get("/partials/ops-banner").text
    assert "甲 公司" in admin_body and "丙公司" in admin_body

    login_as(web_client, *GROUP_A_USER)
    group_body = web_client.get("/partials/ops-banner").text
    assert "甲 公司" in group_body
    assert "丙公司" not in group_body


def test_dashboard_banner_renders_an_empty_but_refreshable_container(
    web_client, login_as, fake_accounts, isolated_snapshots
):
    login_as(web_client, *ADMIN)
    body = web_client.get("/").text
    # 没有告警时不渲染任何一条，但容器要留着——它带着 hx-get，被换掉就再也收不到
    # 下一次 sse:update。
    assert 'id="ops-banner"' in body
    assert 'hx-get="/partials/ops-banner"' in body
    assert "处理建议：" not in body


def test_dashboard_banner_shows_up_on_the_full_page(
    web_client, login_as, fake_accounts, isolated_snapshots
):
    write_disk_stats(low_disk=True, free_mb=88.0)
    login_as(web_client, *ADMIN)
    body = web_client.get("/").text
    assert "磁盘剩余空间告急" in body and "剩余 88MB" in body
    assert "处理建议：" in body


# ---------------------------------------------------------------------------
# 激活 / 恢复去重
# ---------------------------------------------------------------------------

def test_a_standing_alert_is_logged_once(ops_logs):
    write_disk_stats(low_disk=True, free_mb=10.0)
    ops_alerts.refresh_and_log()
    critical = [m for lvl, m in ops_logs if lvl == "CRITICAL"]
    assert len(critical) == 1
    assert "disk_low" in critical[0] and "磁盘剩余空间告急" in critical[0]

    ops_alerts.refresh_and_log()
    assert len([m for lvl, m in ops_logs if lvl == "CRITICAL"]) == 1


def test_recovery_is_logged_and_re_activation_logs_again(ops_logs):
    write_disk_stats(low_disk=True, free_mb=10.0)
    ops_alerts.refresh_and_log()

    write_disk_stats()  # 磁盘清理完了
    assert ops_alerts.refresh_and_log() == []
    assert any(lvl == "INFO" and "已恢复" in m for lvl, m in ops_logs)

    write_disk_stats(low_disk=True, free_mb=10.0)
    ops_alerts.refresh_and_log()
    assert len([m for lvl, m in ops_logs if lvl == "CRITICAL"]) == 2


def test_an_info_alert_is_not_logged_as_a_warning(ops_logs):
    control.pause_daemon()
    ops_alerts.refresh_and_log()
    levels = {lvl for lvl, m in ops_logs if "daemon_paused" in m}
    assert levels == {"INFO"}


def test_only_newly_activated_alerts_are_pushed(monkeypatch):
    sent: list[list] = []
    monkeypatch.setattr(ops_alerts.notifier, "notify_ops", lambda alerts: sent.append(list(alerts)))
    write_disk_stats(low_disk=True, free_mb=10.0)
    ops_alerts.refresh_and_log()
    assert [a.key for a in sent[0]] == ["disk_low"]

    control.pause_daemon()
    ops_alerts.refresh_and_log()
    assert [a.key for a in sent[1]] == ["daemon_paused"]  # 磁盘那条不再重复推

    ops_alerts.refresh_and_log()
    assert len(sent) == 2


def test_notify_ops_is_silent_when_no_channel_is_enabled(isolated_settings):
    """三个渠道全关是生产现状——既不能报错，也不能每轮刷一行日志。"""
    seen: list[str] = []
    sink = logger.add(
        lambda m: seen.append(m.record["message"]),
        level="DEBUG",
        filter=lambda r: r["name"] == "src.notifier",
    )
    try:
        alert = ops_alerts.Alert(
            key="disk_low", level="critical", title="磁盘剩余空间告急",
            detail="剩余 10MB", hint="清理 data/attachments/",
        )
        assert notifier.notify_ops([alert]) == {}
        assert notifier.notify_ops([]) == {}
    finally:
        logger.remove(sink)
    assert seen == []


def test_notify_ops_uses_the_enabled_channels(isolated_settings, monkeypatch):
    isolated_settings.write({"notify": {"wechat": {"enabled": True, "webhook": "https://x.invalid"}}})
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        notifier, "_send_wechat_text",
        lambda md, cfg, tag: calls.append((md, tag)) or True,
    )
    alert = ops_alerts.Alert(
        key="run_wedged", level="critical", title="巡检进程卡死",
        detail="pid=1 已停止心跳", hint="systemctl restart zjip-daemon",
    )
    assert notifier.notify_ops([alert]) == {"wechat": True}
    assert "巡检进程卡死" in calls[0][0] and "systemctl restart zjip-daemon" in calls[0][0]


# ---------------------------------------------------------------------------
# 韧性：横幅在磁盘满 / 文件坏掉时最需要能跑
# ---------------------------------------------------------------------------

def test_an_unwritable_state_file_breaks_nothing(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("这是个文件，不是目录", encoding="utf-8")
    monkeypatch.setattr(ops_alerts, "_STATE_FILE", blocker / "ops_alerts.json")
    write_disk_stats(low_disk=True, free_mb=10.0)

    assert [a.key for a in ops_alerts.collect_alerts()] == ["disk_low"]
    assert [a.key for a in ops_alerts.refresh_and_log()] == ["disk_low"]


def test_a_corrupt_state_file_is_rewritten_not_raised():
    path = ops_alerts._STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    write_disk_stats(low_disk=True, free_mb=10.0)

    assert [a.key for a in ops_alerts.collect_alerts()] == ["disk_low"]
    assert [a.key for a in ops_alerts.refresh_and_log()] == ["disk_low"]
    assert "disk_low" in json.loads(path.read_text(encoding="utf-8"))["alerts"]


def test_a_state_file_with_the_wrong_shape_is_ignored():
    path = ops_alerts._STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"alerts": ["不是字典"]}), encoding="utf-8")
    assert ops_alerts.refresh_and_log() == []


def test_a_broken_check_does_not_cost_the_other_alerts(monkeypatch):
    def boom():
        raise RuntimeError("state.json 读炸了")

    monkeypatch.setattr(ops_alerts, "_check_run_wedged", boom)
    write_disk_stats(low_disk=True, free_mb=10.0)
    assert [a.key for a in ops_alerts.collect_alerts()] == ["disk_low"]


def test_a_broken_status_store_still_yields_system_alerts(monkeypatch):
    monkeypatch.setattr(
        status_store, "get_all", lambda: (_ for _ in ()).throw(RuntimeError("坏了")),
    )
    write_disk_stats(low_disk=True, free_mb=10.0)
    assert [a.key for a in ops_alerts.collect_alerts()] == ["disk_low"]


def test_refresh_and_log_never_raises(monkeypatch):
    monkeypatch.setattr(
        ops_alerts, "collect_alerts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("坏了")),
    )
    assert ops_alerts.refresh_and_log() == []
