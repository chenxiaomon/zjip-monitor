"""Shared test fixtures.

The environment is pinned BEFORE anything imports `web.app` / `web.auth`:
both read os.environ at import time (admin credentials, session secret, API
key, group users) and their `load_dotenv()` call does not override variables
that are already set — so these setdefaults win over the developer's real
`.env` and the suite behaves the same on every machine.
"""

import json
import os

os.environ.setdefault("WEB_USER", "admin")
os.environ.setdefault("WEB_PASS", "test-admin-pass")
os.environ.setdefault("WEB_USERS", "ga:test-ga-pass:group_a,gb:test-gb-pass:group_b")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("API_KEY", "test-api-key-0123456789")
os.environ.setdefault("ZJIP_LOG_TO_FILE", "0")

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
import yaml  # noqa: E402

from src import control, settings_cache, status_store  # noqa: E402
from src.config_loader import Account  # noqa: E402

# Credentials the web fixtures log in with — keep in sync with the setdefaults.
ADMIN = ("admin", "test-admin-pass")
GROUP_A_USER = ("ga", "test-ga-pass")
GROUP_B_USER = ("gb", "test-gb-pass")
API_KEY = "test-api-key-0123456789"


@pytest.fixture
def _own_patcher():
    """A MonkeyPatch instance the test under way cannot revert.

    The `monkeypatch` fixture is one object shared by the test function and
    every fixture that asks for it, so a test calling `monkeypatch.undo()`
    (to simulate "the disk is fine again", say) also undid the path
    redirections that keep the suite off the real data/ directory — and the
    rest of that test then wrote data/control/state.json and run.lock for
    real. Isolation fixtures patch through their own instance instead.
    """
    patcher = pytest.MonkeyPatch()
    yield patcher
    patcher.undo()


@pytest.fixture(autouse=True)
def _isolated_status_store(tmp_path_factory, _own_patcher):
    """Redirect the durable account-status file for EVERY test: deep code
    paths (scan timeouts, login failures) write it as a side effect, and no
    test may leak into the real data/control/accounts_status.json."""
    path = tmp_path_factory.mktemp("status") / "accounts_status.json"
    _own_patcher.setattr(status_store, "_STATUS_FILE", path)
    status_store._read_cache = None
    yield
    status_store._read_cache = None


@pytest.fixture(autouse=True)
def _isolated_diskstat(tmp_path_factory, _own_patcher):
    """Point the disk guard at an empty temp tree for EVERY test: every way
    out of run_all()/run_single() now prunes, which sweeps .part files under
    data/attachments and writes data/control/disk_stats.json as a side
    effect — none of that may reach the real data/."""
    from src import diskstat
    from src import main as src_main

    root = tmp_path_factory.mktemp("diskstat")
    data = root / "data"
    _own_patcher.setattr(diskstat, "_ROOT", root)
    _own_patcher.setattr(diskstat, "_DATA_DIR", data)
    _own_patcher.setattr(diskstat, "_ATTACHMENTS_DIR", data / "attachments")
    _own_patcher.setattr(diskstat, "_SNAPSHOTS_DIR", data / "snapshots")
    _own_patcher.setattr(diskstat, "_REPORTS_DIR", data / "reports")
    _own_patcher.setattr(diskstat, "_EVENTS_FILE", data / "events.jsonl")
    _own_patcher.setattr(diskstat, "_LOGS_DIR", root / "logs")
    _own_patcher.setattr(diskstat, "_STATS_FILE", data / "control" / "disk_stats.json")
    _own_patcher.setattr(src_main, "_ATTACHMENTS_DIR", data / "attachments")
    diskstat._read_cache = None
    yield
    diskstat._read_cache = None


@pytest.fixture(autouse=True)
def _isolated_ops_alerts(tmp_path_factory, _own_patcher):
    """Redirect the ops-banner bookkeeping file for EVERY test: any call to
    ops_alerts.refresh_and_log() (the daemon job, the alert tests) writes it,
    and a leftover activation would silence the very next run's log lines."""
    from src import ops_alerts

    path = tmp_path_factory.mktemp("ops_alerts") / "ops_alerts.json"
    _own_patcher.setattr(ops_alerts, "_STATE_FILE", path)
    yield path


@pytest.fixture(autouse=True)
def _isolated_settings_file(tmp_path_factory, _own_patcher):
    """No test may read — or write — the developer's real config/settings.yaml.

    Everything tunable now flows through settings_cache (runtime_settings,
    logging_setup, the dashboard), and those readers have no monkeypatch
    seam of their own, so a machine with an unusual settings.yaml would get
    different results from CI. The file simply does not exist here, which
    means "all defaults"; `isolated_settings` overrides this with a real
    file for the tests that need one.
    """
    from web.services import settings_svc

    path = tmp_path_factory.mktemp("settings") / "settings.yaml"  # never created
    _own_patcher.setattr(settings_cache, "_SETTINGS_FILE", path)
    _own_patcher.setattr(settings_svc, "_SETTINGS", path)
    settings_cache._cache = None
    yield
    settings_cache._cache = None


@pytest.fixture(autouse=True)
def _reset_login_guard():
    """The console login guard keeps process-global counters; a failed-auth
    assertion in one test must not eat another test's lockout budget."""
    from web import login_guard
    from web.routes import login as login_routes

    login_guard.reset()
    login_routes.FAILED_LOGIN_DELAY_SECONDS = 0.0
    yield
    login_guard.reset()
    login_routes.FAILED_LOGIN_DELAY_SECONDS = 0.5


@pytest.fixture
def isolated_control_dir(tmp_path, _own_patcher):
    """Point all control files at a temp dir so tests never touch data/."""
    _own_patcher.setattr(control, "_CONTROL_DIR", tmp_path)
    _own_patcher.setattr(control, "_LOCK_FILE", tmp_path / "run.lock")
    _own_patcher.setattr(control, "_STATE_FILE", tmp_path / "state.json")
    _own_patcher.setattr(control, "_STOP_FLAG", tmp_path / "stop.flag")
    _own_patcher.setattr(control, "_PAUSE_FLAG", tmp_path / "pause.flag")
    control._local_stop.clear()
    yield tmp_path
    control._local_stop.clear()


# ---------------------------------------------------------------------------
# Web (route-level) fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def web_client():
    """A TestClient for the real FastAPI app.

    Deliberately NOT entered as a context manager. Running the lifespan would
    execute web.app's shutdown handler, which calls
    `deps.executor.shutdown(cancel_futures=True)` on a module-global pool —
    every later test in the same process would then fail with "cannot schedule
    new futures after shutdown". Without the lifespan the SSE watcher task is
    simply never started, which no route-level test needs.
    """
    from fastapi.testclient import TestClient

    from web.app import app

    return TestClient(app)


@pytest.fixture
def login_as():
    """login_as(client, *ADMIN) -> client with a valid session cookie."""

    def _login(client, username: str, password: str):
        resp = client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"login failed: {resp.status_code} {resp.text[:200]}"
        return client

    return _login


@pytest.fixture
def fake_accounts(monkeypatch):
    """Replace the encrypted account store with three in-memory accounts.

    Company names deliberately include a space and a middle dot so sanitizer
    behaviour (snapshot/attachment filenames) stays exercised.
    """
    accounts = [
        Account(company="甲 公司", username="ua1", password="p1", contact="A1", group="group_a"),
        Account(company="乙·公司", username="ua2", password="p2", contact="A2", group="group_a"),
        Account(company="丙公司", username="ub1", password="p3", contact="B1", group="group_b"),
    ]

    from web import auth as web_auth
    from web.routes import accounts as accounts_routes

    # Both modules imported the symbol into their own namespace.
    monkeypatch.setattr(web_auth, "load_accounts", lambda: list(accounts))
    monkeypatch.setattr(accounts_routes, "load_accounts", lambda: list(accounts))
    return accounts


@pytest.fixture
def isolated_settings(tmp_path, _own_patcher):
    """A writable temp settings.yaml wired into both the reader and the writer.

    Returns a namespace with `.path`, `.write(dict)` and `.read()`.
    """
    from web.services import settings_svc

    path = tmp_path / "settings.yaml"
    _own_patcher.setattr(settings_cache, "_SETTINGS_FILE", path)
    _own_patcher.setattr(settings_svc, "_SETTINGS", path)

    def write(data: dict):
        path.write_text(
            yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        settings_cache._cache = None  # mtime resolution is coarser than a test
        return path

    def read() -> dict:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    write(
        {
            "base_url": "https://example.invalid",
            "groups": {"group_a": "来源一", "group_b": "来源二"},
            "schedule": {"crons": ["0 9 * * *", "0 18 * * *"]},
            "login": {"method": "auto", "max_consecutive_failures": 3},
            "notify": {
                "dingtalk": {"enabled": False, "webhook": "", "secret": ""},
                "wechat": {"enabled": False, "webhook": ""},
                "email": {"enabled": False},
            },
        }
    )
    settings_cache._cache = None
    yield SimpleNamespace(path=path, write=write, read=read)
    settings_cache._cache = None


@pytest.fixture
def isolated_snapshots(tmp_path, _own_patcher):
    """Temp snapshot dir shared by the web reader and src.main's writer."""
    from src import main as src_main
    from web.services import snapshot as snap_svc

    d = tmp_path / "snapshots"
    d.mkdir()
    _own_patcher.setattr(snap_svc, "_SNAPSHOTS_DIR", d)
    _own_patcher.setattr(src_main, "_SNAPSHOTS_DIR", d)
    snap_svc._cache.clear()

    def write(company: str, records: list[dict], *, ts: str = "2026-01-01-0900", fetch=None):
        payload = {
            "company": company,
            "snapshot_time": "2026-01-01T01:00:00+00:00",
            "record_count": len(records),
            "records": records,
        }
        if fetch:
            payload["fetch"] = fetch
        path = d / f"{snap_svc._safe_name(company)}_{ts}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        snap_svc._cache.clear()
        return path

    yield SimpleNamespace(dir=d, write=write)
    snap_svc._cache.clear()
