import pytest

from src import control


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
