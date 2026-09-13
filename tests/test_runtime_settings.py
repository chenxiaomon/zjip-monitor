"""src/runtime_settings.py — the typed view of the tunables in settings.yaml.

Three contracts: every default equals the literal it replaced (a file without
the new keys behaves exactly as before); an invalid value falls back and is
logged once, not on every read; and config/settings.yaml.example cannot
drift from the code — it must parse and read back as the defaults.

The second half wires the consumers: main / scraper / attachments / dashboard
must actually pick the values up from the file.
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from loguru import logger

from src import attachments, main, runtime_settings as rs, scraper, settings_cache
from src.config_loader import Account
from tests.conftest import ADMIN
from web.routes import dashboard

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config" / "settings.yaml.example"

SECTIONS = {
    "run": (rs.RunSettings, rs.run_settings),
    "retention": (rs.RetentionSettings, rs.retention_settings),
    "scraper": (rs.ScraperSettings, rs.scraper_settings),
    "attachments": (rs.AttachmentSettings, rs.attachment_settings),
    "ops_alerts": (rs.OpsAlertSettings, rs.ops_alert_settings),
    "disk": (rs.DiskSettings, rs.disk_settings),
}


@pytest.fixture(autouse=True)
def _fresh_warning_state():
    rs.reset_warnings()
    yield
    rs.reset_warnings()


@pytest.fixture
def no_settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_cache, "_SETTINGS_FILE", tmp_path / "absent.yaml")
    settings_cache._cache = None
    yield
    settings_cache._cache = None


@pytest.fixture
def warnings():
    """WARNING+ messages emitted by src.runtime_settings during the test."""
    seen: list[str] = []
    sink_id = logger.add(
        lambda m: seen.append(m.record["message"]),
        level="WARNING",
        filter=lambda r: r["name"] == "src.runtime_settings",
    )
    yield seen
    logger.remove(sink_id)


def _set(iso, section: str, **values):
    """Merge one section into the isolated settings.yaml (keeps the rest)."""
    data = iso.read()
    data.setdefault(section, {}).update(values)
    iso.write(data)


# ---------------------------------------------------------------------------
# Defaults == the literals they replaced
# ---------------------------------------------------------------------------

def test_defaults_are_the_original_literals():
    run = rs.RunSettings()
    assert run.serial_max_accounts == 10          # main.run_all: len(accounts) <= 10
    assert run.max_workers == 3                   # _run_concurrent(max_workers=3)
    assert run.account_delay_min_seconds == 2.0   # random.uniform(2.0, 5.0)
    assert run.account_delay_max_seconds == 5.0
    assert run.account_timeout_seconds == 600     # ACCOUNT_HARD_TIMEOUT_SECONDS

    ret = rs.RetentionSettings()
    assert ret.snapshots_keep_per_company == 30   # _SNAPSHOTS_KEEP_PER_COMPANY
    assert ret.reports_keep_days == 60            # _REPORTS_KEEP_DAYS
    assert ret.events_rotate_mb == 5              # _EVENTS_ROTATE_BYTES = 5 MiB
    assert ret.events_rotate_bytes == 5 * 1024 * 1024
    assert ret.events_rotated_keep == 6           # _EVENTS_ROTATED_KEEP
    assert ret.logs_keep_days == 30
    assert ret.web_logs_keep_days == 14

    assert rs.ScraperSettings().page_delay_seconds == 0.5

    att = rs.AttachmentSettings()
    assert att.request_timeout_seconds == 30      # getNotification / confirmation timeout=30
    assert att.download_timeout_seconds == 60     # _download_file timeout=60
    assert att.crawl_delay_seconds == 0.5
    assert att.max_file_mb == 50                  # _MAX_FILE_BYTES = 50 MiB
    assert att.max_file_bytes == 50 * 1024 * 1024
    assert att.max_total_mb == 1024
    assert att.max_total_bytes == 1024 * 1024 * 1024

    ops = rs.OpsAlertSettings()
    assert ops.stale_hours == 36                  # dashboard._STALE_AFTER_HOURS
    assert ops.min_failed_accounts == 3

    assert rs.DiskSettings().min_free_mb == 500


def test_getters_return_defaults_without_a_settings_file(no_settings_file, warnings):
    for cls, getter in SECTIONS.values():
        assert getter() == cls()
    assert warnings == []


def test_getters_return_defaults_when_sections_are_absent(isolated_settings, warnings):
    # The fixture's file has base_url/groups/login/... but none of the new sections.
    for cls, getter in SECTIONS.values():
        assert getter() == cls()
    assert warnings == []


# ---------------------------------------------------------------------------
# Valid values take effect (and re-take effect on edit, no restart)
# ---------------------------------------------------------------------------

def test_valid_values_take_effect(isolated_settings, warnings):
    _set(isolated_settings, "run", serial_max_accounts=4, max_workers=2,
         account_delay_min_seconds=1, account_delay_max_seconds=1.5,
         account_timeout_seconds=300)
    _set(isolated_settings, "retention", snapshots_keep_per_company=10, reports_keep_days=7,
         events_rotate_mb=2, events_rotated_keep=3, logs_keep_days=5, web_logs_keep_days=2)
    _set(isolated_settings, "scraper", page_size=50, page_delay_seconds=1.25)
    _set(isolated_settings, "attachments", enabled=True, request_timeout_seconds=10,
         download_timeout_seconds=20, crawl_delay_seconds=0, max_file_mb=5, max_total_mb=200)
    _set(isolated_settings, "ops_alerts", stale_hours=48, min_failed_accounts=1)
    _set(isolated_settings, "disk", min_free_mb=250)

    assert rs.run_settings() == rs.RunSettings(
        serial_max_accounts=4, max_workers=2, account_delay_min_seconds=1,
        account_delay_max_seconds=1.5, account_timeout_seconds=300,
    )
    assert rs.retention_settings() == rs.RetentionSettings(
        snapshots_keep_per_company=10, reports_keep_days=7, events_rotate_mb=2,
        events_rotated_keep=3, logs_keep_days=5, web_logs_keep_days=2,
    )
    assert rs.scraper_settings().page_delay_seconds == 1.25
    assert rs.attachment_settings() == rs.AttachmentSettings(
        request_timeout_seconds=10, download_timeout_seconds=20, crawl_delay_seconds=0,
        max_file_mb=5, max_total_mb=200,
    )
    assert rs.ops_alert_settings() == rs.OpsAlertSettings(stale_hours=48, min_failed_accounts=1)
    assert rs.disk_settings().min_free_mb == 250
    assert warnings == []


def test_edit_takes_effect_without_restart(isolated_settings):
    _set(isolated_settings, "run", max_workers=2)
    assert rs.run_settings().max_workers == 2
    _set(isolated_settings, "run", max_workers=1)
    assert rs.run_settings().max_workers == 1


def test_zero_serial_threshold_is_a_valid_always_concurrent_setting(isolated_settings, warnings):
    _set(isolated_settings, "run", serial_max_accounts=0)
    assert rs.run_settings().serial_max_accounts == 0
    assert warnings == []


# ---------------------------------------------------------------------------
# Invalid values: fall back to the default, warn exactly once
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["3", -1, 0, None, True, 2.5, [3]])
def test_invalid_integer_falls_back_and_warns_once(isolated_settings, warnings, bad):
    _set(isolated_settings, "run", max_workers=bad)
    for _ in range(3):
        assert rs.run_settings().max_workers == 3
    assert len(warnings) == 1
    assert "run.max_workers" in warnings[0]
    assert "3" in warnings[0]  # the default the operator ends up with


@pytest.mark.parametrize("bad", ["0.5", -0.1, None, False, {"a": 1}])
def test_invalid_number_falls_back_and_warns_once(isolated_settings, warnings, bad):
    _set(isolated_settings, "scraper", page_delay_seconds=bad)
    for _ in range(3):
        assert rs.scraper_settings().page_delay_seconds == 0.5
    assert len(warnings) == 1
    assert "scraper.page_delay_seconds" in warnings[0]


def test_number_keys_accept_integers(isolated_settings, warnings):
    _set(isolated_settings, "scraper", page_delay_seconds=1)
    assert rs.scraper_settings().page_delay_seconds == 1
    assert warnings == []


def test_zero_timeout_is_rejected(isolated_settings, warnings):
    # asyncio.wait_for(timeout=0) would time out every account instantly.
    _set(isolated_settings, "run", account_timeout_seconds=0)
    assert rs.run_settings().account_timeout_seconds == 600
    assert len(warnings) == 1


def test_non_finite_number_is_rejected(isolated_settings, warnings):
    isolated_settings.path.write_text("scraper:\n  page_delay_seconds: .inf\n", encoding="utf-8")
    settings_cache._cache = None
    assert rs.scraper_settings().page_delay_seconds == 0.5
    assert len(warnings) == 1


def test_one_bad_key_does_not_spoil_its_neighbours(isolated_settings, warnings):
    _set(isolated_settings, "run", max_workers="two", serial_max_accounts=5)
    got = rs.run_settings()
    assert got.max_workers == 3
    assert got.serial_max_accounts == 5
    assert len(warnings) == 1


def test_inverted_delay_window_falls_back_for_both_bounds(isolated_settings, warnings):
    _set(isolated_settings, "run", account_delay_min_seconds=8, account_delay_max_seconds=3)
    for _ in range(3):
        got = rs.run_settings()
        assert (got.account_delay_min_seconds, got.account_delay_max_seconds) == (2.0, 5.0)
    assert len(warnings) == 1
    assert "account_delay_max_seconds" in warnings[0]


def test_section_that_is_not_a_mapping_falls_back_and_warns_once(isolated_settings, warnings):
    isolated_settings.path.write_text("run: 5\n", encoding="utf-8")
    settings_cache._cache = None
    for _ in range(3):
        assert rs.run_settings() == rs.RunSettings()
    assert len(warnings) == 1
    assert "run" in warnings[0]


def test_empty_section_is_not_an_error(isolated_settings, warnings):
    isolated_settings.path.write_text("run:\nretention:\n", encoding="utf-8")
    settings_cache._cache = None
    assert rs.run_settings() == rs.RunSettings()
    assert rs.retention_settings() == rs.RetentionSettings()
    assert warnings == []


def test_warning_rearms_after_the_key_is_fixed(isolated_settings, warnings):
    _set(isolated_settings, "run", max_workers="x")
    rs.run_settings()
    rs.run_settings()
    assert len(warnings) == 1

    _set(isolated_settings, "run", max_workers=2)
    assert rs.run_settings().max_workers == 2

    _set(isolated_settings, "run", max_workers="y")
    rs.run_settings()
    assert len(warnings) == 2  # a fresh regression is worth a fresh line


def test_warned_keys_are_tracked_independently(isolated_settings, warnings):
    _set(isolated_settings, "run", max_workers="x", serial_max_accounts="y")
    rs.run_settings()
    rs.run_settings()
    assert len(warnings) == 2
    assert {("run.max_workers" in w, "run.serial_max_accounts" in w) for w in warnings} == {
        (True, False), (False, True)
    }


# ---------------------------------------------------------------------------
# MB → bytes
# ---------------------------------------------------------------------------

def test_events_rotate_mb_converts_to_bytes(isolated_settings):
    assert rs.RetentionSettings().events_rotate_bytes == 5 * 1024 * 1024
    _set(isolated_settings, "retention", events_rotate_mb=2)
    assert rs.retention_settings().events_rotate_bytes == 2 * 1024 * 1024
    _set(isolated_settings, "retention", events_rotate_mb=0.5)
    assert rs.retention_settings().events_rotate_bytes == 512 * 1024


def test_attachment_mb_converts_to_bytes(isolated_settings):
    _set(isolated_settings, "attachments", max_file_mb=1, max_total_mb=2)
    got = rs.attachment_settings()
    assert got.max_file_bytes == 1024 * 1024
    assert got.max_total_bytes == 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# config/settings.yaml.example must not drift from the code
# ---------------------------------------------------------------------------

def test_example_file_parses_and_reads_back_as_the_defaults(tmp_path, monkeypatch, warnings):
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)

    for section, (cls, _) in SECTIONS.items():
        assert section in data, f"settings.yaml.example is missing the `{section}` section"
        missing = {f for f in cls.__dataclass_fields__} - set(data[section])
        assert not missing, f"settings.yaml.example `{section}` is missing {sorted(missing)}"

    # The other keys the task fixed: groups and the certificate switch.
    assert data["groups"] == {"group_a": "来源一", "group_b": "来源二"}
    assert data["attachments"]["certificates_enabled"] is True
    assert data["attachments"]["enabled"] is True

    # Pre-existing keys keep their documented values.
    assert data["base_url"] == "https://www.zjip.org.cn"
    assert data["scraper"]["page_size"] == 50
    assert data["scraper"]["request_timeout_seconds"] == 30
    assert data["scraper"]["max_pages"] == 300
    assert data["login"]["method"] == "auto"
    assert data["login"]["max_consecutive_failures"] == 3
    assert data["schedule"]["crons"] == ["0 9 * * *", "0 18 * * *"]

    copy = tmp_path / "settings.yaml"
    shutil.copy(EXAMPLE, copy)
    monkeypatch.setattr(settings_cache, "_SETTINGS_FILE", copy)
    settings_cache._cache = None
    try:
        for cls, getter in SECTIONS.values():
            assert getter() == cls()
    finally:
        settings_cache._cache = None
    assert warnings == []


# ---------------------------------------------------------------------------
# Wiring: the consumers really read from the file
# ---------------------------------------------------------------------------

def _accounts(n):
    return [Account(company=f"公司{i}", username=f"u{i}", password="x", contact="") for i in range(n)]


def _ok(account):
    return {"account": account.username, "company": account.company,
            "records": [], "snapshot_path": None, "error": None}


def test_serial_delay_window_comes_from_settings(isolated_settings, monkeypatch):
    _set(isolated_settings, "run", account_delay_min_seconds=0.1, account_delay_max_seconds=0.2)
    windows = []
    monkeypatch.setattr(main.random, "uniform", lambda a, b: windows.append((a, b)) or 0.0)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "scan_account_and_notify", lambda account, cancel=None, **kw: _ok(account))

    results, cancelled = main._run_serial(_accounts(3))

    assert len(results) == 3 and not cancelled
    assert windows == [(0.1, 0.2), (0.1, 0.2)]  # between accounts only, not after the last


def test_run_all_threshold_and_workers_come_from_settings(
    isolated_settings, isolated_control_dir, monkeypatch
):
    _set(isolated_settings, "run", serial_max_accounts=2, max_workers=1, account_timeout_seconds=120)
    monkeypatch.setattr(main, "load_accounts", lambda: _accounts(3))  # 3 > 2 → concurrent
    monkeypatch.setattr(main, "generate_report", lambda *a, **k: None)
    monkeypatch.setattr(main, "notify_batch", lambda *a, **k: None)
    monkeypatch.setattr(main, "_prune_data", lambda: None)
    monkeypatch.setattr(main, "_run_serial", lambda *a, **k: pytest.fail("serial path taken"))

    seen = {}

    async def fake_concurrent(accounts, cancel=None, *, force_login=False,
                              max_workers=None, account_timeout=None):
        seen.update(max_workers=max_workers, account_timeout=account_timeout)
        return [_ok(a) for a in accounts]

    monkeypatch.setattr(main, "_run_concurrent", fake_concurrent)

    out = main.run_all(trigger="test")

    assert seen == {"max_workers": 1, "account_timeout": 120}
    assert len(out["results"]) == 3


def test_run_all_default_threshold_keeps_ten_accounts_serial(
    isolated_settings, isolated_control_dir, monkeypatch
):
    monkeypatch.setattr(main, "load_accounts", lambda: _accounts(10))
    monkeypatch.setattr(main, "generate_report", lambda *a, **k: None)
    monkeypatch.setattr(main, "notify_batch", lambda *a, **k: None)
    monkeypatch.setattr(main, "_prune_data", lambda: None)
    monkeypatch.setattr(main.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(main, "scan_account_and_notify", lambda account, cancel=None, **kw: _ok(account))

    async def never(*a, **k):
        pytest.fail("concurrent path taken for 10 accounts")

    monkeypatch.setattr(main, "_run_concurrent", never)
    assert len(main.run_all(trigger="test")["results"]) == 10


def test_prune_retention_comes_from_settings(isolated_settings, tmp_path, monkeypatch):
    _set(isolated_settings, "retention", snapshots_keep_per_company=5, events_rotated_keep=2)
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    monkeypatch.setattr(main, "_SNAPSHOTS_DIR", snaps)
    monkeypatch.setattr(main, "_REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(main, "_EVENTS_FILE", tmp_path / "events.jsonl")
    for day in range(1, 9):
        (snaps / f"公司A_2026-07-{day:02d}-0900.json").write_text("{}")
    for day in range(1, 6):
        (tmp_path / f"events-202601{day:02d}-000000.jsonl").write_text("x")

    main._prune_data()

    assert len(list(snaps.glob("公司A_*.json"))) == 5
    assert len(list(tmp_path.glob("events-*.jsonl"))) == 2


def _fake_pages(monkeypatch):
    pages = {1: [{"id": 1}], 2: [{"id": 2}]}  # page 3 → [] ends the crawl
    monkeypatch.setattr(scraper, "_build_session", lambda token, timeout=30: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        scraper, "_fetch_page",
        lambda session, base_url, page_num, page_size: {
            "code": 200, "data": {"elements": pages.get(page_num, []), "totalSize": 2},
        },
    )


def test_scraper_page_delay_comes_from_settings_sleep_path(isolated_settings, monkeypatch):
    _set(isolated_settings, "scraper", page_delay_seconds=0.01)
    _fake_pages(monkeypatch)
    slept = []
    monkeypatch.setattr(scraper.time, "sleep", lambda s: slept.append(s))

    assert len(scraper.fetch_all_records("tok").records) == 2
    assert slept == [0.01, 0.01]


def test_scraper_page_delay_comes_from_settings_cancel_path(isolated_settings, monkeypatch):
    _set(isolated_settings, "scraper", page_delay_seconds=0.02)
    _fake_pages(monkeypatch)
    monkeypatch.setattr(scraper.time, "sleep", lambda s: pytest.fail("sleep used with a cancel token"))
    waits = []
    cancel = SimpleNamespace(wait=lambda s: waits.append(s), checkpoint=lambda **kw: None)

    scraper.fetch_all_records("tok", cancel=cancel)
    assert waits == [0.02, 0.02]


def _api_resp(data):
    return SimpleNamespace(status_code=200, ok=True, text="", json=lambda: {"code": 200, "data": data})


def test_attachment_timeouts_delay_and_size_cap_come_from_settings(
    isolated_settings, tmp_path, monkeypatch
):
    _set(isolated_settings, "attachments", request_timeout_seconds=7, download_timeout_seconds=9,
         crawl_delay_seconds=0.02, max_file_mb=1)
    api_calls = []

    class Sess:
        def get(self, url, **kw):
            api_calls.append(("get", kw.get("timeout")))
            return _api_resp([{"dataMailType": "通知书", "dataMailFileUrl": "https://oss/n.pdf"}])

        def post(self, url, **kw):
            api_calls.append(("post", kw.get("timeout")))
            return _api_resp({"systemConfirmation": {"confirmationUrl": "https://oss/c.pdf"}})

        def close(self):
            pass

    monkeypatch.setattr(attachments, "_ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(attachments, "_build_session", lambda token: Sess())
    slept = []
    monkeypatch.setattr(attachments.time, "sleep", lambda s: slept.append(s))
    download_timeouts = []

    def fake_get(url, **kw):
        download_timeouts.append(kw.get("timeout"))
        # 2 MiB declared: over the configured 1 MiB cap, must be refused unread.
        return SimpleNamespace(ok=True, status_code=200,
                               headers={"Content-Length": str(2 * 1024 * 1024)},
                               iter_content=lambda chunk_size: pytest.fail("body was read"))

    monkeypatch.setattr(attachments.requests, "get", fake_get)

    downloaded = attachments.sync_attachments(
        "tok", "公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 7}]
    )

    assert api_calls == [("get", 7), ("post", 7)]
    assert download_timeouts == [9, 9]
    assert slept == [0.02]
    assert downloaded == [] and list(tmp_path.rglob("*.pdf")) == []


def test_download_file_explicit_arguments_still_win(tmp_path, monkeypatch, isolated_settings):
    _set(isolated_settings, "attachments", download_timeout_seconds=9, max_file_mb=1)
    seen = {}

    def fake_get(url, **kw):
        seen["timeout"] = kw.get("timeout")
        return SimpleNamespace(ok=True, status_code=200,
                               headers={"Content-Length": str(3 * 1024 * 1024)},
                               iter_content=lambda chunk_size: iter([b"x"]))

    monkeypatch.setattr(attachments.requests, "get", fake_get)
    dest = tmp_path / "f.pdf"
    assert attachments._download_file("https://oss/f.pdf", dest, timeout=4, max_bytes=4 * 1024 * 1024)
    assert seen["timeout"] == 4
    assert dest.read_bytes() == b"x"


def test_dashboard_stale_threshold_is_a_parameter():
    assert dashboard._is_stale("2026-01-01T00:00:00+00:00", 36) is True
    assert dashboard._is_stale("2026-01-01T00:00:00+00:00", 10 ** 9) is False
    assert dashboard._is_stale(None, 36) is False
    assert dashboard._is_stale("not a date", 36) is False


def test_dashboard_stale_hours_come_from_settings(
    isolated_settings, web_client, login_as, fake_accounts, isolated_snapshots
):
    # Fixture snapshot_time is 2026-01-01 — stale under any sane threshold.
    isolated_snapshots.write("甲 公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 7}])
    login_as(web_client, *ADMIN)

    body = web_client.get("/partials/account-grid").text
    assert "超过 36 小时未巡检成功" in body

    _set(isolated_settings, "ops_alerts", stale_hours=48)
    body = web_client.get("/partials/account-grid").text
    assert "超过 48 小时未巡检成功" in body
    assert "超过 36 小时" not in body

    _set(isolated_settings, "ops_alerts", stale_hours=10 ** 6)
    assert "数据可能过期" not in web_client.get("/partials/account-grid").text


def test_stale_hours_renders_without_a_trailing_zero(
    isolated_settings, web_client, login_as, fake_accounts, isolated_snapshots
):
    """The key accepts a float, and 36.0 must not reach the card as「36.0 小时」."""
    isolated_snapshots.write("甲 公司", [{"dataRegNo": "R1", "dataRegAuditStatus": 7}])
    login_as(web_client, *ADMIN)

    _set(isolated_settings, "ops_alerts", stale_hours=36.0)
    body = web_client.get("/partials/account-grid").text
    assert "超过 36 小时未巡检成功" in body
    assert "36.0" not in body

    _set(isolated_settings, "ops_alerts", stale_hours=36.5)
    assert "超过 36.5 小时未巡检成功" in web_client.get("/partials/account-grid").text


def test_an_absurdly_large_integer_falls_back_instead_of_raising(
    isolated_settings, warnings
):
    """A YAML int too big for a float used to raise OverflowError out of
    math.isfinite — inside run_all, which holds the global run lock."""
    _set(isolated_settings, "run", account_timeout_seconds=10 ** 400)
    assert rs.run_settings().account_timeout_seconds == 600
    assert any("run.account_timeout_seconds" in w for w in warnings)
