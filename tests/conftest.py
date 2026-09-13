import pytest

from src import control, status_store


@pytest.fixture(autouse=True)
def _isolated_status_store(tmp_path_factory, monkeypatch):
    """Redirect the durable account-status file for EVERY test: deep code
    paths (scan timeouts, login failures) write it as a side effect, and no
    test may leak into the real data/control/accounts_status.json."""
    path = tmp_path_factory.mktemp("status") / "accounts_status.json"
    monkeypatch.setattr(status_store, "_STATUS_FILE", path)
    status_store._read_cache = None
    yield
    status_store._read_cache = None


@pytest.fixture
def isolated_control_dir(tmp_path, monkeypatch):
    """Point all control files at a temp dir so tests never touch data/."""
    monkeypatch.setattr(control, "_CONTROL_DIR", tmp_path)
    monkeypatch.setattr(control, "_LOCK_FILE", tmp_path / "run.lock")
    monkeypatch.setattr(control, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(control, "_STOP_FLAG", tmp_path / "stop.flag")
    monkeypatch.setattr(control, "_PAUSE_FLAG", tmp_path / "pause.flag")
    control._local_stop.clear()
    yield tmp_path
    control._local_stop.clear()
