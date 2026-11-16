"""The suite itself stays off the developer's real account store.

Dashboard pages used to decrypt the real config/accounts.enc in about 50
tests: the ops banner calls load_accounts() through a module `fake_accounts`
does not patch, and the real FERNET_KEY came in with the real .env. conftest
now points the loader at an empty temp directory and pins a dummy key.
"""

import base64
import os
from pathlib import Path

import pytest

from src import config_loader
from src.config_loader import ConfigError, load_accounts
from tests.conftest import ADMIN

REAL_ROOT = Path(config_loader.__file__).resolve().parent.parent


def test_the_loader_points_away_from_the_real_files():
    assert config_loader._ENC_PATH.resolve() != REAL_ROOT / "config" / "accounts.enc"
    assert config_loader._ENV_PATH.resolve() != REAL_ROOT / ".env"
    with pytest.raises(ConfigError, match="not found"):
        load_accounts()


def test_the_real_key_is_not_in_the_environment():
    assert base64.urlsafe_b64decode(os.environ["FERNET_KEY"]) == b"test-only-fernet-key-not-secret!"


def test_a_page_whose_callers_are_not_faked_still_renders(web_client, login_as):
    """No fake_accounts here: every load_accounts() caller on the dashboard,
    the ops banner included, meets an absent store and copes."""
    login_as(web_client, *ADMIN)
    resp = web_client.get("/")
    assert resp.status_code == 200 and "暂无账号数据" in resp.text
