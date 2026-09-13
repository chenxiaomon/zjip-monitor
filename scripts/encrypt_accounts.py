#!/usr/bin/env python3
"""Encrypt accounts.yaml → accounts.enc.

Usage (first time — generates a new key):
    python scripts/encrypt_accounts.py

Usage (reuse existing key from .env, skip the overwrite confirmation):
    python scripts/encrypt_accounts.py --yes

Workflow:
  1. Reads config/accounts.yaml (plain text, NOT committed to git)
  2. Reads FERNET_KEY out of the .env FILE (never out of the environment —
     see _env_file_key). A key already in .env is reused as-is and never
     replaced; one is generated only when .env has none, and only after the
     operator confirms that no unreadable accounts.enc would be orphaned
  3. If config/accounts.enc already exists, decrypts it and prints what
     would change (added / removed / modified accounts — never passwords),
     then asks for a "yes" on an interactive terminal
  4. Writes encrypted config/accounts.enc atomically, keeping the previous
     file as config/accounts.enc.bak
  5. Verifies the round-trip by decrypting and re-parsing

After running this script, delete config/accounts.yaml — the .enc file
is the only copy needed at runtime. To look at what is inside .enc later,
use scripts/decrypt_accounts.py instead of re-creating the yaml by hand.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from cryptography.fernet import Fernet
from dotenv import dotenv_values, set_key

from src.config_loader import Account, ConfigError, load_accounts, save_accounts

_YAML_PATH = ROOT / "config" / "accounts.yaml"
_ENC_PATH = ROOT / "config" / "accounts.enc"
_ENV_PATH = ROOT / ".env"

_REQUIRED_FIELDS = ("company", "username", "password")

# What decrypt_accounts.mask_password() produces: "****" plus the last two
# characters. A genuine password could in principle start with four stars, but
# refusing it (the operator can still change that one password from the
# console) is far cheaper than silently masking every password in the file.
_MASKED_PASSWORD_RE = re.compile(r"^\*{4,}")


def _env_file_key() -> str:
    """FERNET_KEY exactly as the .env FILE spells it (empty when absent).

    Deliberately NOT os.getenv(): load_dotenv() never overrides a variable
    already present in os.environ, and an `export FERNET_KEY=` left over in
    the shell counts as present. Deciding "does .env have a key?" from the
    environment therefore used to answer "no" while .env held the real key —
    and the answer was acted on by overwriting that key. systemd hands the
    daemon and the web console this same file through EnvironmentFile=, so
    the file is the only thing that decides what production can decrypt.
    """
    return (dotenv_values(_ENV_PATH).get("FERNET_KEY") or "").strip()


def _generate_key() -> bytes:
    """Mint a FERNET_KEY, persist it to .env (0600) and export it.

    Destructive whenever an accounts.enc encrypted with another key exists —
    every caller must have confirmed that before getting here.
    """
    key = Fernet.generate_key()
    _ENV_PATH.touch(exist_ok=True)
    set_key(str(_ENV_PATH), "FERNET_KEY", key.decode())
    # .env holds the only copy of the key; created under the default umask it
    # would be 0644, i.e. readable by every account on the box.
    os.chmod(_ENV_PATH, 0o600)
    # save_accounts()/load_accounts() read .env through load_dotenv(), which
    # never overrides a variable already present in os.environ — an empty or
    # stale FERNET_KEY inherited from the shell would therefore shadow the
    # key we just wrote. Export it explicitly so the very next call uses it.
    os.environ["FERNET_KEY"] = key.decode()
    print(f"  ✓ Key saved to {_ENV_PATH}")
    print("  ⚠  Back up this key — without it you cannot decrypt accounts.enc!")
    return key


def _enc_stamp() -> tuple[int, int] | None:
    """Identity of accounts.enc right now, or None when it does not exist.

    Same (mtime_ns, size) pair config_loader caches on: enough to notice that
    the web console saved an account while we were waiting for a "yes".
    """
    try:
        st = _ENC_PATH.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_yaml(path: Path) -> object:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _as_text(value: object, where: str) -> str:
    # YAML turns an unquoted digit-only password into an int (and a
    # leading-zero one into octal!). Refuse instead of str()-ing it so the
    # operator fixes the source file rather than encrypting a mangled value.
    if not isinstance(value, str):
        kind = type(value).__name__
        raise ConfigError(
            f"{where} 不是字符串，而是 {kind}（{value!r}）——YAML 会把未加引号的 "
            "12345 / true / 2026-01-01 解析成数字、布尔或日期，"
            "请给该值加上引号后重跑"
        )
    return value


def build_accounts(data: object) -> list[Account]:
    """Validate the parsed accounts.yaml structure and build Account objects.

    Raises ConfigError with an operator-facing message on the first problem.
    """
    if not isinstance(data, dict):
        raise ConfigError("accounts.yaml 顶层必须是映射，形如 `accounts: [...]`")

    raw_accounts = data.get("accounts") or []
    if not raw_accounts:
        raise ConfigError("accounts.yaml has no accounts listed.")

    accounts: list[Account] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(raw_accounts):
        if not isinstance(raw, dict):
            raise ConfigError(f"accounts[{i}] 必须是包含 company/username/password 的映射")
        for field in _REQUIRED_FIELDS:
            if not raw.get(field):
                raise ConfigError(f"accounts[{i}] missing required field '{field}'")

        username = _as_text(raw["username"], f"accounts[{i}].username")
        # The console addresses accounts by username (edit / delete / status),
        # so two entries with the same username would be indistinguishable.
        if username in seen:
            raise ConfigError(
                f"accounts[{i}] 用户名 {username!r} 与 accounts[{seen[username]}] 重复"
            )
        seen[username] = i

        password = _as_text(raw["password"], f"accounts[{i}].password")
        # decrypt_accounts.py masks passwords by default; saving that listing
        # as accounts.yaml and re-encrypting it would silently replace every
        # real password with "****xx" and lock every account out of the site.
        if _MASKED_PASSWORD_RE.match(password):
            raise ConfigError(
                f"accounts[{i}].password 以 4 个以上 * 开头——这看起来是 decrypt_accounts.py "
                "的打码输出，请用 --show-passwords 或 --out 导出真实密码后再加密"
            )

        contact = raw.get("contact")
        group = raw.get("group")
        accounts.append(Account(
            company=_as_text(raw["company"], f"accounts[{i}].company"),
            username=username,
            password=password,
            contact="" if contact is None else str(contact),
            group="" if group is None else str(group),
        ))
    return accounts


def _show(value: str) -> str:
    return value if value else "（空）"


def summarize_changes(old: list[Account], new: list[Account]) -> str:
    """Describe how `new` differs from `old`, keyed by username.

    Passwords are only ever reported as "changed" — the text is meant for a
    terminal that may be shared or logged.
    """
    old_by = {a.username: a for a in old}
    new_by = {a.username: a for a in new}

    added = [a for u, a in new_by.items() if u not in old_by]
    removed = [a for u, a in old_by.items() if u not in new_by]
    changed: list[tuple[Account, list[str]]] = []
    for username, n in new_by.items():
        o = old_by.get(username)
        if o is None:
            continue
        fields: list[str] = []
        if o.password != n.password:
            fields.append("密码有变化")
        if o.company != n.company:
            fields.append(f"公司 {_show(o.company)} → {_show(n.company)}")
        if o.contact != n.contact:
            fields.append(f"联系人 {_show(o.contact)} → {_show(n.contact)}")
        if o.group != n.group:
            fields.append(f"分组 {_show(o.group)} → {_show(n.group)}")
        if fields:
            changed.append((n, fields))

    lines = [f"与现有 accounts.enc 对比（现有 {len(old)} 个账号 → 新 {len(new)} 个账号）："]
    if not (added or removed or changed):
        lines.append("  无变化（内容相同，仍会重新加密写入）")
        return "\n".join(lines)
    for a in added:
        lines.append(f"  + 新增 {a.username}（{a.company}）")
    for a in removed:
        lines.append(f"  - 移除 {a.username}（{a.company}）")
    for a, fields in changed:
        lines.append(f"  ~ 变化 {a.username}（{a.company}）：{'；'.join(fields)}")
    return "\n".join(lines)


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() == "yes"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 config/accounts.yaml 加密成 config/accounts.enc（已有文件会先对比再覆盖）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="不询问直接覆盖已有的 accounts.enc（stdin 不是终端时对可解密的旧文件也会自动跳过询问）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not _YAML_PATH.exists():
        print(f"ERROR: {_YAML_PATH} not found.")
        print(f"  Copy {ROOT / 'config' / 'accounts.yaml.example'} to {_YAML_PATH}")
        print("  and fill in your account details, then re-run this script.")
        return 1

    try:
        accounts = build_accounts(_read_yaml(_YAML_PATH))
    except yaml.YAMLError as exc:
        print(f"ERROR: accounts.yaml 不是合法的 YAML：{exc}")
        return 1
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Found {len(accounts)} account(s):")
    for acc in accounts:
        print(f"  - {acc.company} ({acc.username})")

    interactive = sys.stdin is not None and sys.stdin.isatty()
    had_existing = _ENC_PATH.exists()
    # What we are about to compare against. Re-checked just before the write,
    # because the operator may sit on the confirmation prompt for a while.
    enc_stamp = _enc_stamp()

    env_key = _env_file_key()
    if env_key:
        shell_key = os.environ.get("FERNET_KEY", "").strip()
        if shell_key and shell_key != env_key:
            print(f"\nERROR: 当前 shell 的 FERNET_KEY 与 {_ENV_PATH} 里的不一致。")
            print("  生产的 daemon / web 通过 systemd 的 EnvironmentFile 读取 .env，")
            print("  用 shell 里的密钥加密会让线上进程解不开 accounts.enc。")
            print("  请先执行 `unset FERNET_KEY`，再重跑本脚本。")
            return 1
        # .env wins: export it so load_accounts()/save_accounts() below use
        # the same key production will, instead of whatever the shell holds
        # (load_dotenv() inside them would not override an existing value).
        os.environ["FERNET_KEY"] = env_key

        if had_existing:
            try:
                existing = load_accounts()
            except ConfigError as exc:
                print(f"\n⚠  现有 {_ENC_PATH} 无法用当前密钥读取，无法对比：{exc}")
                print("   继续将整体覆盖它（旧文件会保留为 accounts.enc.bak）。")
                if not args.yes:
                    if not interactive:
                        # Nobody is watching and there is no diff to show — the
                        # only safe default is to stop and demand an explicit flag.
                        print("   非交互环境下需要显式传 --yes 才会覆盖无法读取的旧文件。")
                        return 1
                    if not _confirm("确认覆盖？输入 yes 继续："):
                        print("已取消，accounts.enc 未改动。")
                        return 1
            else:
                print()
                print(summarize_changes(existing, accounts))
                if not args.yes and interactive:
                    if not _confirm("确认用 accounts.yaml 的内容覆盖 accounts.enc？输入 yes 继续："):
                        print("已取消，accounts.enc 未改动。")
                        return 1
    else:
        # No key in .env. Minting one is irreversible for any data encrypted
        # with the previous key, so confirm FIRST and write .env last: an
        # aborted run must leave the old key recoverable from a .env backup.
        if had_existing:
            print(f"\n⚠  {_ENV_PATH} 里没有 FERNET_KEY，但 {_ENC_PATH} 已存在。")
            print("   现有文件是用另一把密钥加密的，生成新密钥后它将永久无法解密，")
            print("   里面的账号会全部丢失。")
            print("   如果密钥只是没写进 .env，请先把它写回 .env（FERNET_KEY=…）再重跑本脚本。")
            if not args.yes:
                if not interactive:
                    print("   非交互环境下需要显式传 --yes 才会生成新密钥并覆盖它。")
                    return 1
                if not _confirm("确认生成新密钥并覆盖现有 accounts.enc？输入 yes 继续："):
                    print("已取消，.env 与 accounts.enc 均未改动。")
                    return 1
        print("No FERNET_KEY found in .env — generating a new one…")
        _generate_key()

    # A console save that landed while the operator was reading the diff would
    # be silently thrown away by the write below; re-running is cheap.
    if _enc_stamp() != enc_stamp:
        print(f"\nERROR: {_ENC_PATH} 在确认期间被修改（可能是控制台保存了账号），")
        print("  已放弃写入，请重新运行本脚本。")
        return 1

    # save_accounts() owns the one-generation .enc.bak and the atomic write.
    try:
        save_accounts(accounts)
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"\n✓ Encrypted → {_ENC_PATH}")
    bak = _ENC_PATH.with_suffix(".enc.bak")
    if had_existing and bak.exists():
        print(f"  旧文件已备份为 {bak}")

    # Verify round-trip through the same loader the daemon/web use
    try:
        reloaded = load_accounts()
    except ConfigError as exc:
        print(f"ERROR: Round-trip verification failed — {exc}")
        return 1
    if len(reloaded) != len(accounts):
        print(
            f"ERROR: Round-trip verification failed — wrote {len(accounts)} account(s) "
            f"but read back {len(reloaded)}"
        )
        return 1
    print("✓ Round-trip verification passed")

    print("\nNext steps:")
    print(f"  1. Delete {_YAML_PATH} (it contains plain-text passwords)")
    print("  2. Keep .env (with FERNET_KEY) secure — do NOT commit it")
    print("  3. accounts.enc is safe to commit to git")
    return 0


if __name__ == "__main__":
    sys.exit(main())
