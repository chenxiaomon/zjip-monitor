"""WEB_USERS parsing: colon-containing passwords, no password echo in errors."""

import pytest

from web import auth


def test_password_with_colons(monkeypatch):
    monkeypatch.setenv("WEB_USERS", "alice:p:a:s:s:g1, bob:simple:g2")
    users = auth._parse_users()
    assert users["alice"]["password"] == "p:a:s:s"
    assert users["alice"]["group"] == "g1"
    assert users["bob"]["password"] == "simple"
    assert users["bob"]["group"] == "g2"


def test_bad_entry_does_not_echo_password(monkeypatch):
    monkeypatch.setenv("WEB_USERS", "alice:secretpw")  # missing group
    with pytest.raises(RuntimeError) as exc_info:
        auth._parse_users()
    assert "secretpw" not in str(exc_info.value)
    assert "第 1 项" in str(exc_info.value)


def test_duplicate_username_rejected(monkeypatch):
    monkeypatch.setenv("WEB_USERS", "alice:pw1:g1, alice:pw2:g2")
    with pytest.raises(RuntimeError, match="用户名重复"):
        auth._parse_users()


def test_empty_env_gives_admin_only(monkeypatch):
    monkeypatch.delenv("WEB_USERS", raising=False)
    users = auth._parse_users()
    assert set(users) == {auth._ADMIN_USER}
