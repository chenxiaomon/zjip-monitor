"""scripts/encrypt_accounts.py + scripts/decrypt_accounts.py.

Everything is redirected into tmp_path: the plain accounts.yaml, the .env
holding a freshly generated Fernet key, and the accounts.enc that both the
scripts and src.config_loader read/write. No test touches the real config/.
"""

from __future__ import annotations

import io
import os
import stat
from types import SimpleNamespace

import pytest
import yaml
from cryptography.fernet import Fernet

from scripts import decrypt_accounts, encrypt_accounts
from src import config_loader
from src.config_loader import Account, ConfigError, load_accounts

# Plain-text passwords used across the tests. They exist so assertions can
# prove NONE of them ever reaches stdout/stderr of the encrypt script.
PW_A_OLD = "pw-alpha-OLD-9x"
PW_A_NEW = "pw-alpha-NEW-7q"
PW_B = "pw-bravo-secret"
PW_C = "pw-charlie-secret"
PW_D = "pw-delta-secret"
ALL_PASSWORDS = (PW_A_OLD, PW_A_NEW, PW_B, PW_C, PW_D)

INITIAL = [
    {"company": "甲公司", "username": "ua", "password": PW_A_OLD, "contact": "张三", "group": "group_a"},
    {"company": "乙公司", "username": "ub", "password": PW_B, "contact": "李四", "group": "group_a"},
    {"company": "丙公司", "username": "uc", "password": PW_C, "contact": "王五", "group": "group_b"},
]


class _TtyStdin(io.StringIO):
    """StringIO that claims to be a terminal so the confirmation prompt runs."""

    def isatty(self) -> bool:
        return True


class _ReactiveTtyStdin(_TtyStdin):
    """A tty that runs `callback` the moment the script blocks on input().

    Stands in for "the web console saved an account while the operator was
    still reading the diff" — input() falls back to sys.stdin.readline() when
    stdin is not a real interactive file object.
    """

    def __init__(self, text: str, callback) -> None:
        super().__init__(text)
        self._callback = callback

    def readline(self, *args, **kwargs):  # type: ignore[override]
        self._callback()
        return super().readline(*args, **kwargs)


@pytest.fixture
def acct_env(tmp_path, monkeypatch):
    """Isolated yaml/.env/.enc trio wired into both scripts and config_loader."""
    key = Fernet.generate_key().decode()
    env_path = tmp_path / ".env"
    env_path.write_text(f"FERNET_KEY={key}\n", encoding="utf-8")
    cfg = tmp_path / "config"
    cfg.mkdir()
    yaml_path = cfg / "accounts.yaml"
    enc_path = cfg / "accounts.enc"

    monkeypatch.setattr(config_loader, "_ENC_PATH", enc_path)
    monkeypatch.setattr(config_loader, "_ENV_PATH", env_path)
    monkeypatch.setattr(encrypt_accounts, "_YAML_PATH", yaml_path)
    monkeypatch.setattr(encrypt_accounts, "_ENC_PATH", enc_path)
    monkeypatch.setattr(encrypt_accounts, "_ENV_PATH", env_path)
    # setenv first so monkeypatch records the original state even for tests
    # that later delenv() and let the script re-create the variable.
    monkeypatch.setenv("FERNET_KEY", key)
    # Non-tty stdin by default: the script must then write without prompting.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    invalidate = getattr(config_loader, "invalidate_cache", None)
    if callable(invalidate):
        invalidate()

    def write_yaml(accounts: list[dict]) -> None:
        yaml_path.write_text(
            yaml.dump({"accounts": accounts}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    return SimpleNamespace(
        key=key,
        env_path=env_path,
        yaml_path=yaml_path,
        enc_path=enc_path,
        bak_path=enc_path.with_suffix(".enc.bak"),
        write_yaml=write_yaml,
    )


def _decrypt_raw(path, key: str) -> list[dict]:
    data = yaml.safe_load(Fernet(key.encode()).decrypt(path.read_bytes()).decode("utf-8"))
    return data["accounts"]


def _encrypt_raw(path, key: str, data: object) -> None:
    """Write an accounts.enc straight from `data`, bypassing every validation
    the current scripts do — this is how an older version of the script (or a
    hand-rolled yaml) could leave non-string fields inside the file."""
    plaintext = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    path.write_bytes(Fernet(key.encode()).encrypt(plaintext.encode("utf-8")))
    config_loader.invalidate_cache()


def _assert_no_passwords(text: str) -> None:
    for pw in ALL_PASSWORDS:
        assert pw not in text, f"plain-text password {pw!r} leaked into script output"


# ---------------------------------------------------------------------------
# encrypt_accounts.py — pure helpers
# ---------------------------------------------------------------------------

def test_build_accounts_happy_path():
    accounts = encrypt_accounts.build_accounts({"accounts": INITIAL})
    assert [a.username for a in accounts] == ["ua", "ub", "uc"]
    assert accounts[0] == Account("甲公司", "ua", PW_A_OLD, "张三", "group_a")


def test_build_accounts_optional_fields_default_to_empty():
    accounts = encrypt_accounts.build_accounts(
        {"accounts": [{"company": "X", "username": "u", "password": "p", "contact": None}]}
    )
    assert accounts[0].contact == ""
    assert accounts[0].group == ""


@pytest.mark.parametrize(
    "data, fragment",
    [
        ({}, "no accounts"),
        ({"accounts": []}, "no accounts"),
        ([], "accounts:"),
        ({"accounts": [{"company": "X", "username": "u"}]}, "accounts[0] missing required field 'password'"),
        ({"accounts": [INITIAL[0], {"company": "", "username": "u", "password": "p"}]},
         "accounts[1] missing required field 'company'"),
        ({"accounts": ["not-a-mapping"]}, "accounts[0]"),
        ({"accounts": [{"company": "X", "username": "u", "password": 12345678}]}, "引号"),
        ({"accounts": [INITIAL[0], {**INITIAL[1], "username": "ua"}]}, "重复"),
    ],
)
def test_build_accounts_rejects_bad_input(data, fragment):
    with pytest.raises(ConfigError) as exc_info:
        encrypt_accounts.build_accounts(data)
    assert fragment in str(exc_info.value)


def test_summarize_changes_lists_added_removed_changed_without_passwords():
    old = [Account(**INITIAL[0]), Account(**INITIAL[1]), Account(**INITIAL[2])]
    new = [
        Account("甲公司", "ua", PW_A_NEW, "张三", "group_a"),          # password only
        Account("丙公司改名", "uc", PW_C, "赵六", "group_a"),          # company+contact+group
        Account("丁公司", "ud", PW_D, "", ""),                          # added; ub removed
    ]
    text = encrypt_accounts.summarize_changes(old, new)
    assert "+ 新增 ud（丁公司）" in text
    assert "- 移除 ub（乙公司）" in text
    assert "~ 变化 ua（甲公司）：密码有变化" in text
    assert "~ 变化 uc" in text and "公司 丙公司 → 丙公司改名" in text
    assert "联系人 王五 → 赵六" in text and "分组 group_b → group_a" in text
    assert "无变化" not in text
    _assert_no_passwords(text)


def test_summarize_changes_no_change():
    old = [Account(**d) for d in INITIAL]
    new = [Account(**d) for d in INITIAL]
    text = encrypt_accounts.summarize_changes(old, new)
    assert "无变化" in text
    _assert_no_passwords(text)


# ---------------------------------------------------------------------------
# encrypt_accounts.py — main()
# ---------------------------------------------------------------------------

def test_encrypt_missing_yaml_returns_1(acct_env, capsys):
    assert encrypt_accounts.main([]) == 1
    out = capsys.readouterr().out
    assert "not found" in out
    assert not acct_env.enc_path.exists()


def test_encrypt_invalid_yaml_returns_1_without_writing(acct_env, capsys):
    acct_env.write_yaml([{"company": "X", "username": "u"}])
    assert encrypt_accounts.main([]) == 1
    assert "missing required field 'password'" in capsys.readouterr().out
    assert not acct_env.enc_path.exists()


def test_first_run_creates_enc_and_round_trips(acct_env, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0

    out = capsys.readouterr().out
    assert "Found 3 account(s)" in out
    assert "甲公司 (ua)" in out
    assert "Round-trip verification passed" in out
    assert "Next steps" in out
    _assert_no_passwords(out)

    assert acct_env.enc_path.exists()
    assert not acct_env.bak_path.exists(), "no previous file, so no backup expected"
    loaded = load_accounts()
    assert [(a.username, a.password, a.group) for a in loaded] == [
        ("ua", PW_A_OLD, "group_a"), ("ub", PW_B, "group_a"), ("uc", PW_C, "group_b"),
    ]


def test_first_run_generates_key_when_env_has_none(acct_env, monkeypatch, capsys):
    monkeypatch.delenv("FERNET_KEY")
    acct_env.env_path.write_text("", encoding="utf-8")
    acct_env.write_yaml(INITIAL[:1])

    assert encrypt_accounts.main([]) == 0

    out = capsys.readouterr().out
    assert "generating a new one" in out
    env_text = acct_env.env_path.read_text(encoding="utf-8")
    assert "FERNET_KEY=" in env_text
    saved_key = os.environ["FERNET_KEY"]
    assert saved_key and saved_key in env_text
    # save_accounts() must have used the freshly generated key
    assert _decrypt_raw(acct_env.enc_path, saved_key)[0]["username"] == "ua"


def test_second_run_prints_summary_and_writes_without_prompt_when_not_tty(acct_env, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()
    old_bytes = acct_env.enc_path.read_bytes()

    acct_env.write_yaml([
        {**INITIAL[0], "password": PW_A_NEW},                                   # changed
        INITIAL[2],                                                              # unchanged
        {"company": "丁公司", "username": "ud", "password": PW_D, "contact": "赵六", "group": ""},  # added
    ])                                                                           # ub removed
    assert encrypt_accounts.main([]) == 0

    captured = capsys.readouterr()
    out = captured.out
    assert "+ 新增 ud（丁公司）" in out
    assert "- 移除 ub（乙公司）" in out
    assert "~ 变化 ua（甲公司）：密码有变化" in out
    assert "uc" not in out.split("对比")[1].split("Encrypted")[0], "unchanged account must not appear in the diff"
    assert "输入 yes" not in out, "non-tty stdin must not prompt"
    _assert_no_passwords(out + captured.err)

    # backup holds the previous generation, .enc holds the new one
    assert acct_env.bak_path.read_bytes() == old_bytes
    assert [a["username"] for a in _decrypt_raw(acct_env.bak_path, acct_env.key)] == ["ua", "ub", "uc"]
    new = {a.username: a for a in load_accounts()}
    assert set(new) == {"ua", "uc", "ud"}
    assert new["ua"].password == PW_A_NEW
    assert new["ud"].contact == "赵六"


def test_second_run_yes_flag_skips_prompt_even_on_tty(acct_env, monkeypatch, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()

    # A tty that would answer "no" — must never be consulted when --yes is given
    monkeypatch.setattr("sys.stdin", _TtyStdin("no\n"))
    acct_env.write_yaml(INITIAL[:2])
    assert encrypt_accounts.main(["--yes"]) == 0

    out = capsys.readouterr().out
    assert "- 移除 uc（丙公司）" in out
    assert "输入 yes" not in out
    assert acct_env.bak_path.exists()
    assert [a.username for a in load_accounts()] == ["ua", "ub"]


def test_second_run_tty_prompt_yes_writes(acct_env, monkeypatch, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", _TtyStdin("yes\n"))
    acct_env.write_yaml(INITIAL[:2])
    assert encrypt_accounts.main([]) == 0

    out = capsys.readouterr().out
    assert "输入 yes" in out
    assert acct_env.bak_path.exists()
    assert [a.username for a in load_accounts()] == ["ua", "ub"]


def test_second_run_tty_prompt_declined_leaves_enc_untouched(acct_env, monkeypatch, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()
    before = acct_env.enc_path.read_bytes()

    monkeypatch.setattr("sys.stdin", _TtyStdin("n\n"))
    acct_env.write_yaml(INITIAL[:1])
    assert encrypt_accounts.main([]) == 1

    out = capsys.readouterr().out
    assert "已取消" in out
    assert acct_env.enc_path.read_bytes() == before
    assert not acct_env.bak_path.exists()


def test_second_run_no_change_still_writes_and_says_so(acct_env, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()
    before = acct_env.enc_path.read_bytes()

    assert encrypt_accounts.main([]) == 0
    out = capsys.readouterr().out
    assert "无变化" in out
    assert "Round-trip verification passed" in out
    assert acct_env.bak_path.read_bytes() == before
    assert len(load_accounts()) == 3


def test_existing_enc_undecryptable_requires_explicit_yes(acct_env, capsys):
    acct_env.enc_path.write_bytes(b"not-a-fernet-token")
    acct_env.write_yaml(INITIAL)

    # Non-tty and no --yes: refuse, because the operator cannot see a diff
    assert encrypt_accounts.main([]) == 1
    out = capsys.readouterr().out
    assert "无法" in out and "--yes" in out
    assert acct_env.enc_path.read_bytes() == b"not-a-fernet-token"

    # Explicit --yes: overwrite, keeping the unreadable file as .enc.bak
    assert encrypt_accounts.main(["--yes"]) == 0
    assert acct_env.bak_path.read_bytes() == b"not-a-fernet-token"
    assert len(load_accounts()) == 3


# ---------------------------------------------------------------------------
# decrypt_accounts.py
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pw, masked",
    [("pw-alpha-OLD-9x", "****9x"), ("abcd", "****"), ("a", "****"), ("", "****")],
)
def test_mask_password(pw, masked):
    assert decrypt_accounts.mask_password(pw) == masked


def test_decrypt_default_output_masks_passwords(acct_env, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()

    assert decrypt_accounts.main([]) == 0
    captured = capsys.readouterr()
    _assert_no_passwords(captured.out)
    data = yaml.safe_load(captured.out)
    assert [a["username"] for a in data["accounts"]] == ["ua", "ub", "uc"]
    assert data["accounts"][0] == {
        "company": "甲公司", "username": "ua", "password": "****9x", "contact": "张三", "group": "group_a",
    }
    assert "--show-passwords" in captured.err


def test_decrypt_show_passwords_prints_plaintext(acct_env, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()

    assert decrypt_accounts.main(["--show-passwords"]) == 0
    data = yaml.safe_load(capsys.readouterr().out)
    assert [a["password"] for a in data["accounts"]] == [PW_A_OLD, PW_B, PW_C]


def test_decrypt_out_writes_0600_plaintext_file(acct_env, tmp_path, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()

    out_file = tmp_path / "dump" / "accounts.plain.yaml"
    assert decrypt_accounts.main(["--out", str(out_file)]) == 0

    captured = capsys.readouterr()
    assert "已写入 3 个账号" in captured.out
    assert "明文密码" in captured.err and "删除" in captured.err
    _assert_no_passwords(captured.out)

    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    data = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    assert data == {"accounts": INITIAL}


def test_decrypt_out_tightens_permissions_of_existing_file(acct_env, tmp_path, capsys):
    acct_env.write_yaml(INITIAL[:1])
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()

    out_file = tmp_path / "existing.yaml"
    out_file.write_text("stale content that is longer than the new one\n" * 5, encoding="utf-8")
    os.chmod(out_file, 0o644)

    assert decrypt_accounts.main(["--out", str(out_file)]) == 0
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    assert yaml.safe_load(out_file.read_text(encoding="utf-8")) == {"accounts": INITIAL[:1]}


def test_decrypt_config_error_is_chinese_and_no_traceback(acct_env, capsys):
    assert not acct_env.enc_path.exists()
    assert decrypt_accounts.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "错误" in captured.err
    assert "Traceback" not in captured.err


def test_decrypt_wrong_key_reports_error(acct_env, monkeypatch, capsys):
    acct_env.write_yaml(INITIAL[:1])
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()

    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    assert decrypt_accounts.main([]) == 1
    captured = capsys.readouterr()
    assert "错误" in captured.err and "Traceback" not in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------------------
# encrypt_accounts.py — FERNET_KEY resolution (.env is the only authority)
# ---------------------------------------------------------------------------

def test_empty_shell_key_does_not_shadow_the_key_in_env(acct_env, monkeypatch, capsys):
    """`export FERNET_KEY=` used to make the script believe .env had no key.

    It then replaced the real key in .env with a fresh one — after which the
    existing accounts.enc (and every backup of it) was unreadable forever.
    """
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()
    env_before = acct_env.env_path.read_text(encoding="utf-8")

    monkeypatch.setenv("FERNET_KEY", "")  # the destructive shell state
    acct_env.write_yaml(INITIAL[:2])
    assert encrypt_accounts.main(["--yes"]) == 0

    out = capsys.readouterr().out
    assert "No FERNET_KEY found" not in out
    assert acct_env.env_path.read_text(encoding="utf-8") == env_before
    assert os.environ["FERNET_KEY"] == acct_env.key
    assert [a["username"] for a in _decrypt_raw(acct_env.enc_path, acct_env.key)] == ["ua", "ub"]


def test_shell_key_differing_from_env_key_aborts_without_writing(acct_env, monkeypatch, capsys):
    """The production services read .env through systemd's EnvironmentFile=,
    so encrypting with a different key exported in the shell would hand them
    a file they cannot decrypt."""
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()
    enc_before = acct_env.enc_path.read_bytes()
    env_before = acct_env.env_path.read_text(encoding="utf-8")

    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    acct_env.write_yaml(INITIAL[:1])
    assert encrypt_accounts.main(["--yes"]) == 1

    out = capsys.readouterr().out
    assert "unset FERNET_KEY" in out
    assert acct_env.enc_path.read_bytes() == enc_before
    assert acct_env.env_path.read_text(encoding="utf-8") == env_before
    assert not acct_env.bak_path.exists()


def test_no_key_in_env_but_enc_exists_refuses_to_generate(acct_env, monkeypatch, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()
    enc_before = acct_env.enc_path.read_bytes()

    # The key vanished from .env (restored backup, botched edit…). Minting a
    # replacement makes the accounts.enc next to it unreadable forever.
    monkeypatch.delenv("FERNET_KEY")
    acct_env.env_path.write_text("", encoding="utf-8")

    assert encrypt_accounts.main([]) == 1
    out = capsys.readouterr().out
    assert "永久" in out and "--yes" in out
    assert acct_env.enc_path.read_bytes() == enc_before
    assert "FERNET_KEY" not in acct_env.env_path.read_text(encoding="utf-8")
    assert "FERNET_KEY" not in os.environ


def test_no_key_in_env_tty_decline_leaves_env_and_enc_untouched(acct_env, monkeypatch, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()
    enc_before = acct_env.enc_path.read_bytes()

    monkeypatch.delenv("FERNET_KEY")
    acct_env.env_path.write_text("", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", _TtyStdin("no\n"))

    assert encrypt_accounts.main([]) == 1
    assert "已取消" in capsys.readouterr().out
    assert acct_env.enc_path.read_bytes() == enc_before
    assert "FERNET_KEY" not in acct_env.env_path.read_text(encoding="utf-8")
    assert "FERNET_KEY" not in os.environ


def test_no_key_in_env_with_yes_generates_and_overwrites(acct_env, monkeypatch, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()

    monkeypatch.delenv("FERNET_KEY")
    acct_env.env_path.write_text("", encoding="utf-8")

    assert encrypt_accounts.main(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "generating a new one" in out

    new_key = os.environ["FERNET_KEY"]
    assert new_key != acct_env.key
    assert new_key in acct_env.env_path.read_text(encoding="utf-8")
    assert [a["username"] for a in _decrypt_raw(acct_env.enc_path, new_key)] == ["ua", "ub", "uc"]


def test_generated_env_file_is_mode_0600(acct_env, monkeypatch, capsys):
    monkeypatch.delenv("FERNET_KEY")
    acct_env.env_path.unlink()  # brand-new box: .env does not exist yet
    acct_env.write_yaml(INITIAL[:1])

    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()
    assert stat.S_IMODE(acct_env.env_path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# encrypt_accounts.py — the masked dump must never be re-encrypted (B)
# ---------------------------------------------------------------------------

def test_build_accounts_rejects_masked_password_sentinel():
    with pytest.raises(ConfigError) as exc_info:
        encrypt_accounts.build_accounts(
            {"accounts": [{"company": "甲公司", "username": "ua", "password": "****9x"}]}
        )
    message = str(exc_info.value)
    assert "打码" in message and "--show-passwords" in message


def test_encrypt_refuses_masked_dump_and_leaves_enc_untouched(acct_env, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()
    before = acct_env.enc_path.read_bytes()

    # The operator saved the DEFAULT (masked) decrypt output as accounts.yaml.
    assert decrypt_accounts.main([]) == 0
    acct_env.yaml_path.write_text(capsys.readouterr().out, encoding="utf-8")

    assert encrypt_accounts.main(["--yes"]) == 1
    assert "打码" in capsys.readouterr().out
    assert acct_env.enc_path.read_bytes() == before
    assert {a.password for a in load_accounts()} == {PW_A_OLD, PW_B, PW_C}


# ---------------------------------------------------------------------------
# encrypt_accounts.py — accounts.enc re-checked after the confirmation (C)
# ---------------------------------------------------------------------------

def test_enc_rewritten_during_confirmation_aborts_the_write(acct_env, monkeypatch, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()

    def console_saves_an_account() -> None:
        config_loader.save_accounts([Account("丁公司", "ud", PW_D, "赵六", "")])

    monkeypatch.setattr("sys.stdin", _ReactiveTtyStdin("yes\n", console_saves_an_account))
    acct_env.write_yaml(INITIAL[:1])
    assert encrypt_accounts.main([]) == 1

    captured = capsys.readouterr()
    assert "确认期间被修改" in captured.out
    _assert_no_passwords(captured.out + captured.err)
    # The console's save survived; nothing was silently overwritten.
    assert [a.username for a in load_accounts()] == ["ud"]


# ---------------------------------------------------------------------------
# decrypt_accounts.py --out must never follow a symlink (D)
# ---------------------------------------------------------------------------

def test_decrypt_out_refuses_symlink_and_leaves_target_untouched(acct_env, tmp_path, capsys):
    acct_env.write_yaml(INITIAL)
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()

    target = tmp_path / "settings.yaml"
    target.write_text("base_url: https://example.invalid\n", encoding="utf-8")
    os.chmod(target, 0o644)
    link = tmp_path / "dump.yaml"
    link.symlink_to(target)

    assert decrypt_accounts.main(["--out", str(link)]) == 1

    captured = capsys.readouterr()
    assert "错误" in captured.err and "Traceback" not in captured.err
    assert target.read_text(encoding="utf-8") == "base_url: https://example.invalid\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert link.is_symlink()
    _assert_no_passwords(captured.out + captured.err)


def test_decrypt_out_refuses_dangling_symlink(acct_env, tmp_path, capsys):
    acct_env.write_yaml(INITIAL[:1])
    assert encrypt_accounts.main([]) == 0
    capsys.readouterr()

    victim = tmp_path / "not-created-by-us.yaml"
    link = tmp_path / "dangling.yaml"
    link.symlink_to(victim)

    assert decrypt_accounts.main(["--out", str(link)]) == 1
    assert "错误" in capsys.readouterr().err
    assert not victim.exists()
    assert link.is_symlink()


# ---------------------------------------------------------------------------
# Legacy accounts.enc with non-string fields (E)
# ---------------------------------------------------------------------------

def test_legacy_non_string_fields_decrypt_and_round_trip(acct_env, tmp_path, capsys):
    """An older encrypt script fed `password: 123456` (unquoted in the yaml,
    therefore an int) straight into the file. Masking used to crash on it and
    the --out dump was then rejected by build_accounts()."""
    _encrypt_raw(acct_env.enc_path, acct_env.key, {"accounts": [
        {"company": "甲公司", "username": "ua", "password": 123456,
         "contact": 13800000000, "group": None},
    ]})

    assert decrypt_accounts.main([]) == 0
    masked = yaml.safe_load(capsys.readouterr().out)
    assert masked["accounts"][0]["password"] == "****56"

    dump = tmp_path / "plain.yaml"
    assert decrypt_accounts.main(["--out", str(dump)]) == 0
    capsys.readouterr()
    assert "password: '123456'" in dump.read_text(encoding="utf-8")

    acct_env.yaml_path.write_text(dump.read_text(encoding="utf-8"), encoding="utf-8")
    assert encrypt_accounts.main(["--yes"]) == 0
    capsys.readouterr()

    reloaded = load_accounts()[0]
    assert (reloaded.password, reloaded.contact, reloaded.group) == ("123456", "13800000000", "")
