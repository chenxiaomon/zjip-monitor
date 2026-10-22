"""web/services/users.py — the console user store (config/users.yaml)."""

import json
import stat

import pytest
import yaml
from loguru import logger

from web.services import users


@pytest.fixture
def log_lines():
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    yield lines
    logger.remove(sink)


def _doc(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _new(name="zhangsan", role="operator", groups=("group_a",), password="correct-horse"):
    return users.create_user(name, display_name="张三", role=role, groups=list(groups),
                             password=password, by="admin")


# --- hashing -----------------------------------------------------------------

def test_hash_round_trip():
    h = users.hash_password("correct-horse")
    assert h.startswith("pbkdf2_sha256$1000$")
    assert users.verify_password("correct-horse", h)
    assert not users.verify_password("wrong-horse", h)


def test_each_hash_keeps_its_own_iteration_count(monkeypatch):
    old = users.hash_password("correct-horse")
    monkeypatch.setattr(users, "PBKDF2_ITERATIONS", 2000)
    assert users.verify_password("correct-horse", old)
    assert users.hash_password("x" * 10).startswith("pbkdf2_sha256$2000$")


@pytest.mark.parametrize("stored", [
    "", "garbage", "md5$1$aa$bb", "pbkdf2_sha256$abc$aa$bb",
    "pbkdf2_sha256$999999999$AAAA$AAAA",  # a hand-edited count must not pin a worker
    "pbkdf2_sha256$1000$!!!$AAAA",
])
def test_malformed_hashes_never_verify(stored):
    assert users.verify_password("anything", stored) is False


# --- validation --------------------------------------------------------------

@pytest.mark.parametrize("name", ["a", "-abc", "has space", "名字", "a|b", "x" * 33, "view", "admin"])
def test_bad_usernames(name):
    with pytest.raises(users.UserError):
        users.validate_username(name)


def test_usernames_are_lowercased():
    assert users.validate_username("  ZhangSan ") == "zhangsan"


@pytest.mark.parametrize("password,username,msg", [
    ("short", "zhangsan", "至少"),
    ("x" * 257, "zhangsan", "最多"),
    (" " * 12, "zhangsan", "空格"),
    ("ZhangSan01", "zhangsan01", "用户名"),
])
def test_bad_passwords(password, username, msg):
    with pytest.raises(users.UserError, match=msg):
        users.validate_password(password, username)


# --- create / read -----------------------------------------------------------

def test_create_writes_a_private_file(_isolated_users):
    assert _new() == "zhangsan"
    assert stat.S_IMODE(_isolated_users.stat().st_mode) == 0o600
    rec = users.get("ZhangSan")
    assert rec["role"] == "operator" and rec["groups"] == ["group_a"]
    assert rec["display_name"] == "张三" and rec["created_by"] == "admin"
    assert len(rec["session_key"]) == 32
    assert "correct-horse" not in _isolated_users.read_text(encoding="utf-8")
    assert users.verify_password("correct-horse", rec["password_hash"])


def test_duplicate_and_groupless_users_are_refused():
    _new()
    with pytest.raises(users.UserError, match="已存在"):
        _new()
    with pytest.raises(users.UserError, match="分组"):
        _new("lisi", role="viewer", groups=())
    assert _new("boss", role="admin", groups=()) == "boss"  # admins need no group


def test_get_returns_a_copy():
    _new()
    users.get("zhangsan")["groups"].append("group_b")
    assert users.get("zhangsan")["groups"] == ["group_a"]


def test_unknown_role_reads_as_least_privileged(_isolated_users):
    doc = _doc(_isolated_users)
    doc["users"]["ga"]["role"] = "superuser"
    _isolated_users.write_text(yaml.safe_dump(doc), encoding="utf-8")
    users.invalidate()
    assert users.get("ga")["role"] == "viewer"


def test_a_corrupt_file_is_never_overwritten(_isolated_users, log_lines):
    _isolated_users.write_text("users: [unclosed", encoding="utf-8")
    before = _isolated_users.read_bytes()
    users.invalidate()

    assert users.load() == {}
    assert "格式错误" in users.load_error()
    users.load()  # cached: logged once per version of the file
    assert sum("无法读取" in ln for ln in log_lines) == 1

    with pytest.raises(users.UsersUnreadable):
        _new()
    with pytest.raises(users.UsersUnreadable):
        users.set_password("ga", "new-password-1")
    assert _isolated_users.read_bytes() == before


# --- changes -----------------------------------------------------------------

def test_role_change_signs_out_but_group_change_does_not():
    _new()
    key = users.get("zhangsan")["session_key"]

    diff = users.update_user("zhangsan", display_name="张三", role="operator",
                             groups=["group_a", "group_b"])
    assert diff == {"groups": [["group_a"], ["group_a", "group_b"]]}
    assert users.get("zhangsan")["session_key"] == key

    diff = users.update_user("zhangsan", display_name="张三", role="viewer",
                             groups=["group_a", "group_b"])
    assert diff == {"role": ["operator", "viewer"]}
    assert users.get("zhangsan")["session_key"] != key


def test_no_op_update_does_not_write(_isolated_users):
    _new()
    before = _isolated_users.read_bytes()
    assert users.update_user("zhangsan", display_name="张三", role="operator", groups=["group_a"]) == {}
    assert _isolated_users.read_bytes() == before


def test_password_change_rotates_the_session_key():
    _new()
    key = users.get("zhangsan")["session_key"]
    users.set_password("zhangsan", "another-horse", must_change=True)
    rec = users.get("zhangsan")
    assert rec["session_key"] != key and rec["must_change_password"] is True
    assert users.verify_password("another-horse", rec["password_hash"])
    with pytest.raises(users.UserError):
        users.set_password("zhangsan", "short")


def test_disable_signs_out_enable_does_not():
    _new()
    key = users.get("zhangsan")["session_key"]
    users.set_disabled("zhangsan", True)
    disabled_key = users.get("zhangsan")["session_key"]
    assert users.get("zhangsan")["disabled"] and disabled_key != key
    users.set_disabled("zhangsan", False)
    assert users.get("zhangsan")["session_key"] == disabled_key


def test_revoke_and_delete():
    _new()
    key = users.get("zhangsan")["session_key"]
    users.revoke_sessions("zhangsan")
    assert users.get("zhangsan")["session_key"] != key
    assert users.delete_user("zhangsan")["role"] == "operator"
    assert users.get("zhangsan") is None
    with pytest.raises(users.UserError, match="不存在"):
        users.delete_user("zhangsan")


# --- WEB_USERS import --------------------------------------------------------

def test_parse_keeps_colons_in_passwords():
    assert users.parse_web_users("alice:p:a:s:s:g1, bob:simple:g2") == [
        ("alice", "p:a:s:s", "g1"), ("bob", "simple", "g2"),
    ]
    assert users.parse_web_users("") == []


def test_parse_never_echoes_a_bad_entry():
    with pytest.raises(RuntimeError) as exc_info:
        users.parse_web_users("alice:secretpw")
    assert "secretpw" not in str(exc_info.value) and "第 1 项" in str(exc_info.value)


@pytest.mark.parametrize("raw", ["alice:pw1:g1, alice:pw2:g2", "admin:pw:g1"])
def test_parse_refuses_duplicates_including_the_env_admin(raw):
    with pytest.raises(RuntimeError, match="用户名重复"):
        users.parse_web_users(raw)


def test_migrate_imports_once_as_viewers(_isolated_users, _isolated_audit, monkeypatch):
    _isolated_users.unlink()
    users.invalidate()
    monkeypatch.setenv("WEB_USERS", "Ga:legacy:g1,ops:p:w:d:g2,bad name:pw:g1")

    assert users.migrate_from_env() == 2
    assert stat.S_IMODE(_isolated_users.stat().st_mode) == 0o600
    ga, ops = users.get("ga"), users.get("ops")
    assert ga["role"] == ops["role"] == "viewer"
    assert ga["groups"] == ["g1"] and ops["groups"] == ["g2"]
    # Same signature as the cookie they already hold — the deploy logs no one out.
    assert ga["session_key"] == "" and ga["must_change_password"] is True
    assert users.verify_password("legacy", ga["password_hash"])  # short legacy passwords are kept
    assert users.verify_password("p:w:d", ops["password_hash"])

    (entry,) = [json.loads(ln) for f in _isolated_audit.glob("*.jsonl")
                for ln in f.read_text(encoding="utf-8").splitlines()]
    assert entry["action"] == "user.import" and entry["detail"] == {"users": ["ga", "ops"]}

    # A second start finds the file and imports nothing.
    before = _isolated_users.read_bytes()
    assert users.migrate_from_env() == 0
    assert _isolated_users.read_bytes() == before


def test_migrate_with_an_existing_file_only_warns(_isolated_users, monkeypatch, log_lines):
    monkeypatch.setenv("WEB_USERS", "zz:pw:g1")
    assert users.migrate_from_env() == 0
    assert users.get("zz") is None
    assert any("WEB_USERS 已停用" in ln for ln in log_lines)


def test_malformed_web_users_imports_nothing_and_never_logs_passwords(
        _isolated_users, monkeypatch, log_lines):
    _isolated_users.unlink()
    users.invalidate()
    monkeypatch.setenv("WEB_USERS", "alice:SENTINEL-PW:g1,bob:SENTINEL-PW2")
    assert users.migrate_from_env() == 0
    assert not _isolated_users.exists()
    assert not any("SENTINEL" in ln for ln in log_lines)
    assert any("未导入" in ln for ln in log_lines)


def test_migrate_without_web_users_is_a_no_op(_isolated_users, monkeypatch):
    _isolated_users.unlink()
    monkeypatch.setenv("WEB_USERS", "")
    assert users.migrate_from_env() == 0
    assert not _isolated_users.exists()
