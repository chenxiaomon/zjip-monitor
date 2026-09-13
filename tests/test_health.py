"""GET /health — the unauthenticated probe deploy.sh polls after every restart.

Three properties matter and each is load-bearing somewhere else:

* PUBLIC — it is outside web/app.py's login gate, so a regression that drops
  it from the allow-list turns the probe into a 302 to /login, which reads as
  "up" to anything that only checks for a non-5xx. Being public also means the
  payload must never carry a company name, username, password or absolute
  path: the box answers on a bare IP over plain HTTP.
* NEVER 500 — deploy.sh treats a 5xx as "the service never came up" and would
  fail a deploy that actually worked. Every collector is isolated, so a broken
  one has to degrade its own check and nothing else.
* strict=1 → 503 when degraded, which is the signal deploy.sh branches on.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import diskstat, health, status_store
from src.config_loader import Account, ConfigError

_MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _write_status(entries: dict) -> None:
    """Drop an accounts_status.json into the temp path conftest's autouse
    fixture already points status_store at.

    Written directly rather than through record_scan(): half of these tests
    need a last_success_at from 40 hours ago, and record_scan can only ever
    stamp "now".
    """
    path = status_store._STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "accounts": entries}, ensure_ascii=False), encoding="utf-8"
    )
    status_store._read_cache = None


def _entry(*, company: str = "甲 公司", status: str = "ok",
           hours_ago: float = 0.5, paused: bool = False, error: str | None = None) -> dict:
    seen = _hours_ago(hours_ago)
    return {
        "company": company,
        "status": status,
        "error": error,
        "last_attempt_at": seen,
        "last_success_at": seen if status in ("ok", "partial") else None,
        "login_paused": paused,
        "consecutive_login_failures": 3 if paused else 0,
    }


def _write_state(control_dir, **over) -> None:
    """A state.json in the temp control dir. The default carries `progress`
    (which names the company being scanned) and an error field on purpose:
    the leak test asserts neither reaches the response."""
    now = datetime.now(timezone.utc)
    state = {
        "run_id": "20260913-050000-abcd",
        "kind": "run_all",
        "trigger": "daemon",
        "pid": os.getpid(),
        "started_at": (now - timedelta(hours=1)).isoformat(),
        # Older than control.HEARTBEAT_STALE_SECONDS, so a live pid + this
        # heartbeat is exactly the "wedged" shape read_state() reports.
        "heartbeat_at": (now - timedelta(hours=1)).isoformat(),
        "status": "running",
        "progress": {"account": "甲 公司", "index": 2, "total": 3},
        "error": None,
        "finished_at": None,
    }
    state.update(over)
    (control_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )


def _write_disk_stats(**over) -> None:
    """A disk_stats.json where conftest's autouse fixture points diskstat."""
    stats = {
        "collected_at": "2026-09-13T04:00:00+00:00",
        "root": {"total_bytes": 1, "used_bytes": 1, "free_bytes": 1,
                 "free_mb": 1.0, "percent_used": 50.0},
        "dirs": {"attachments": {"bytes": 200 * _MIB, "files": 12}},
        "attachments_over_limit": False,
        "attachments_max_total_bytes": 1024 * _MIB,
        "low_disk": False,
        "min_free_mb": 500,
    }
    stats.update(over)
    path = diskstat._STATS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
    diskstat._read_cache = None


def _check(body: dict, name: str) -> dict:
    matches = [c for c in body["checks"] if c["name"] == name]
    assert len(matches) == 1, f"check {name!r} missing from {body['checks']}"
    return matches[0]


@pytest.fixture
def health_env(monkeypatch, tmp_path, fake_accounts, isolated_settings, isolated_control_dir):
    """A healthy box: three accounts, all scanned successfully half an hour ago.

    disk.min_free_mb is pinned to 0 so the disk check exercises the real
    statvfs path without the result depending on how full the developer's
    laptop happens to be.
    """
    release = tmp_path / "RELEASE"  # deliberately NOT created — the server has none either
    monkeypatch.setattr(health, "_RELEASE_FILE", release)
    monkeypatch.setattr(health, "load_accounts", lambda: list(fake_accounts))
    isolated_settings.write({"disk": {"min_free_mb": 0}, "ops_alerts": {"stale_hours": 36}})
    _write_status({
        "ua1": _entry(company="甲 公司"),
        "ua2": _entry(company="乙·公司"),
        "ub1": _entry(company="丙公司"),
    })
    return SimpleNamespace(
        release=release, settings=isolated_settings, control=isolated_control_dir
    )


# ---------------------------------------------------------------------------
# Public access + the healthy shape
# ---------------------------------------------------------------------------

def test_health_needs_no_login_and_is_never_cached(web_client, health_env):
    resp = web_client.get("/health", follow_redirects=False)
    assert resp.status_code == 200, "not in the public allow-list → 302 to /login"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"


def test_healthy_box_reports_ok_in_both_modes(web_client, health_env):
    resp = web_client.get("/health")
    body = resp.json()

    assert body["status"] == "ok"
    assert [c["name"] for c in body["checks"]] == [
        "accounts_configured", "recent_success", "run_not_wedged",
        "disk_free", "attachments_size",
    ]
    assert all(c["ok"] for c in body["checks"]), body["checks"]
    assert body["scans"]["accounts"] == 3
    assert body["scans"]["status_counts"] == {"ok": 3}
    assert body["scans"]["paused"] == 0
    assert 0 <= body["scans"]["hours_since_success"] < 1
    assert body["run"] is None  # no state.json yet
    assert body["disk"]["low"] is False
    assert body["disk"]["free_mb"] > 0
    assert body["web"]["uptime_seconds"] >= 0
    assert body["web"]["started_at"].endswith("+00:00")

    assert web_client.get("/health?strict=1").status_code == 200


def test_collect_health_is_usable_without_the_web_stack(health_env):
    """src.health is a plain function — no FastAPI, so the daemon could reuse it."""
    snapshot = health.collect_health()
    assert set(snapshot) == {"status", "version", "web", "run", "scans", "disk", "checks"}
    assert snapshot["status"] == "ok"


def test_paused_account_is_counted_without_degrading(web_client, health_env):
    """One dead account is the dashboard's problem; the deploy probe only asks
    whether the system as a whole is still scanning."""
    _write_status({
        "ua1": _entry(company="甲 公司"),
        "ua2": _entry(company="乙·公司", status="login_failed", paused=True, error="密码错误"),
    })
    body = web_client.get("/health").json()
    assert body["scans"]["paused"] == 1
    assert body["scans"]["status_counts"] == {"login_failed": 1, "ok": 1}
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# version / RELEASE
# ---------------------------------------------------------------------------

def test_missing_release_file_is_null_not_degraded(web_client, health_env):
    assert not health_env.release.exists()
    body = web_client.get("/health").json()
    assert body["version"] is None
    assert body["status"] == "ok"
    assert web_client.get("/health?strict=1").status_code == 200


def test_release_file_is_parsed_with_a_short_sha(web_client, health_env):
    sha = "0123456789abcdef0123456789abcdef01234567"
    health_env.release.write_text(
        f"{sha} b3/health 2026-09-13T05:12:33Z build-host\n", encoding="utf-8"
    )
    body = web_client.get("/health").json()
    assert body["version"] == {
        "sha": "0123456789ab",
        "branch": "b3/health",
        "deployed_at": "2026-09-13T05:12:33Z",
        "deployed_by": "build-host",
    }
    assert body["status"] == "ok"


def test_malformed_release_file_is_null_not_degraded(web_client, health_env):
    health_env.release.write_text("only-one-field\n", encoding="utf-8")
    body = web_client.get("/health").json()
    assert body["version"] is None
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# degraded paths — each must be 200 plain and 503 under ?strict=1
# ---------------------------------------------------------------------------

def test_stale_last_success_is_degraded(web_client, health_env):
    _write_status({"ua1": _entry(company="甲 公司", hours_ago=40)})
    body = web_client.get("/health").json()

    assert body["scans"]["hours_since_success"] > 36
    assert body["scans"]["last_success_at"] is not None
    assert _check(body, "recent_success")["ok"] is False
    assert "36" in _check(body, "recent_success")["detail"]
    assert body["status"] == "degraded"
    assert web_client.get("/health?strict=1").status_code == 503


def test_never_scanned_box_is_degraded(web_client, health_env):
    """A fresh install has no successful scan yet: honest 503 under strict=1,
    which deploy.sh reports as 'up but degraded' rather than a failed deploy."""
    _write_status({})
    body = web_client.get("/health").json()
    assert body["scans"]["last_success_at"] is None
    assert body["scans"]["hours_since_success"] is None
    assert _check(body, "recent_success")["ok"] is False
    assert body["status"] == "degraded"
    assert web_client.get("/health?strict=1").status_code == 503


def test_low_disk_is_degraded(web_client, health_env):
    # 1e12 MB is larger than any real filesystem, so check_free() is exercised
    # for real and still reports "low" on every machine.
    health_env.settings.write(
        {"disk": {"min_free_mb": 10 ** 12}, "ops_alerts": {"stale_hours": 36}}
    )
    body = web_client.get("/health").json()

    assert body["disk"]["low"] is True
    assert body["disk"]["min_free_mb"] == 10 ** 12
    assert _check(body, "disk_free")["ok"] is False
    assert body["status"] == "degraded"
    assert web_client.get("/health?strict=1").status_code == 503


def test_wedged_run_is_degraded(web_client, health_env):
    """Live pid + a dead heartbeat: the process still owns run.lock, so no
    further scan can start until zjip-daemon is restarted."""
    _write_state(health_env.control)
    body = web_client.get("/health").json()

    assert body["run"]["status"] == "stale"
    assert body["run"]["wedged"] is True
    assert body["run"]["trigger"] == "daemon"
    assert body["run"]["pid"] == os.getpid()
    assert _check(body, "run_not_wedged")["ok"] is False
    assert body["status"] == "degraded"
    assert web_client.get("/health?strict=1").status_code == 503


def test_finished_run_is_not_wedged(web_client, health_env):
    _write_state(
        health_env.control,
        status="done",
        finished_at=_hours_ago(0.2),
        heartbeat_at=_hours_ago(0.2),
    )
    body = web_client.get("/health").json()
    assert body["run"]["status"] == "done"
    assert body["run"]["wedged"] is False
    assert _check(body, "run_not_wedged")["ok"] is True
    assert body["status"] == "ok"


def test_oversized_attachments_dir_is_degraded(web_client, health_env):
    _write_disk_stats(
        dirs={"attachments": {"bytes": 2048 * _MIB, "files": 900}},
        attachments_over_limit=True,
    )
    body = web_client.get("/health").json()

    assert body["disk"]["attachments_mb"] == 2048
    assert body["disk"]["stats_collected_at"] == "2026-09-13T04:00:00+00:00"
    assert _check(body, "attachments_size")["ok"] is False
    assert body["status"] == "degraded"
    assert web_client.get("/health?strict=1").status_code == 503


def test_attachment_size_is_null_until_a_run_writes_the_snapshot(web_client, health_env):
    body = web_client.get("/health").json()
    assert body["disk"]["attachments_mb"] is None
    assert body["disk"]["stats_collected_at"] is None
    assert _check(body, "attachments_size")["ok"] is True
    assert body["status"] == "ok"


@pytest.mark.parametrize("query,expected", [
    ("strict=1", 503),
    ("strict=true", 503),
    ("strict=TRUE", 503),
    ("strict=yes", 503),
    ("strict=0", 200),
    ("strict=", 200),
    ("", 200),
])
def test_strict_flag_controls_the_status_code(web_client, health_env, query, expected):
    _write_status({})  # degraded: no successful scan on record
    resp = web_client.get(f"/health?{query}" if query else "/health")
    assert resp.status_code == expected
    assert resp.json()["status"] == "degraded"


# ---------------------------------------------------------------------------
# Nothing sensitive may cross a public boundary
# ---------------------------------------------------------------------------

def test_payload_leaks_no_company_username_password_or_path(
    web_client, health_env, monkeypatch, tmp_path
):
    """The endpoint is reachable by anyone who can reach the server's IP."""
    accounts = [
        Account(company="浙江某某数据科技有限公司", username="zjip_leaky_user_1",
                password="zjip_leaky_pass_1", contact="张三经理", group="group_a"),
        Account(company="上海某某信息服务有限公司", username="zjip_leaky_user_2",
                password="zjip_leaky_pass_2", contact="李四主管", group="group_b"),
    ]
    monkeypatch.setattr(health, "load_accounts", lambda: list(accounts))
    _write_status({
        "zjip_leaky_user_1": _entry(company="浙江某某数据科技有限公司"),
        "zjip_leaky_user_2": _entry(
            company="上海某某信息服务有限公司", status="login_failed",
            error="登录失败：密码 zjip_leaky_pass_2 已失效",
        ),
    })
    _write_state(health_env.control, progress={"account": "浙江某某数据科技有限公司"})
    _write_disk_stats()
    health_env.release.write_text(
        "0123456789abcdef0123456789abcdef01234567 main 2026-09-13T05:12:33Z build-host\n",
        encoding="utf-8",
    )

    resp = web_client.get("/health")
    raw = resp.text
    reserialized = json.dumps(resp.json(), ensure_ascii=False)

    forbidden = [
        "浙江某某数据科技有限公司", "上海某某信息服务有限公司",
        "zjip_leaky_user_1", "zjip_leaky_user_2",
        "zjip_leaky_pass_1", "zjip_leaky_pass_2",
        "张三经理", "李四主管",
        str(tmp_path), str(health_env.release), "accounts_status.json",
    ]
    for needle in forbidden:
        assert needle not in raw, f"{needle!r} leaked into the public /health body"
        assert needle not in reserialized, f"{needle!r} leaked into the public /health body"

    # …and it really did collect, so the assertions above are not vacuous.
    body = resp.json()
    assert body["scans"]["accounts"] == 2
    assert body["scans"]["status_counts"] == {"login_failed": 1, "ok": 1}
    assert body["run"]["wedged"] is True
    assert body["version"]["sha"] == "0123456789ab"


# ---------------------------------------------------------------------------
# A broken collector degrades itself, never the process
# ---------------------------------------------------------------------------

_BOOM_CASES = [
    ("control", "read_state", "run_not_wedged"),
    ("status_store", "get_all", "recent_success"),
    ("diskstat", "check_free", "disk_free"),
    ("diskstat", "read_stats", "attachments_size"),
]


@pytest.mark.parametrize("module_attr,func_name,check_name", _BOOM_CASES)
def test_broken_collector_degrades_instead_of_500(
    web_client, health_env, monkeypatch, module_attr, func_name, check_name
):
    def boom(*args, **kwargs):
        raise RuntimeError("模拟采集失败 /private/var/folders/secret/path")

    monkeypatch.setattr(getattr(health, module_attr), func_name, boom)

    resp = web_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    failed = _check(body, check_name)
    assert failed["ok"] is False
    # The exception CLASS is useful; its text is not, and repr() of an OS
    # error carries an absolute path.
    assert "RuntimeError" in failed["detail"]
    assert "/private/var" not in resp.text
    assert web_client.get("/health?strict=1").status_code == 503


def test_unreadable_account_config_degrades_only_its_own_check(
    web_client, health_env, monkeypatch
):
    def boom():
        raise ConfigError("FERNET_KEY not set in /opt/zjip-monitor/.env")

    monkeypatch.setattr(health, "load_accounts", boom)

    resp = web_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scans"]["accounts"] is None
    assert body["scans"]["status_counts"] == {"ok": 3}  # the status store still read fine
    assert _check(body, "accounts_configured")["ok"] is False
    assert "ConfigError" in _check(body, "accounts_configured")["detail"]
    assert "FERNET_KEY" not in resp.text and "/opt/zjip-monitor" not in resp.text
    assert _check(body, "recent_success")["ok"] is True
    assert body["status"] == "degraded"
    assert web_client.get("/health?strict=1").status_code == 503


def test_empty_account_config_degrades(web_client, health_env, monkeypatch):
    monkeypatch.setattr(health, "load_accounts", list)
    body = web_client.get("/health").json()
    assert body["scans"]["accounts"] == 0
    assert _check(body, "accounts_configured")["ok"] is False
    assert body["status"] == "degraded"


def test_endpoint_answers_even_if_the_whole_collector_explodes(
    web_client, health_env, monkeypatch
):
    """Last-ditch net in the route: a bug in collect_health() itself must not
    turn into a 5xx, which deploy.sh would read as 'never came up'."""
    from web.routes import health as health_route

    def boom():
        raise RuntimeError("collect_health 内部出错")

    monkeypatch.setattr(health_route, "collect_health", boom)

    resp = web_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"
    assert _check(resp.json(), "health_collect")["ok"] is False
    assert web_client.get("/health?strict=1").status_code == 503
