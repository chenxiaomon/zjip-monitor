#!/usr/bin/env python3
"""Encrypt accounts.yaml → accounts.enc.

Usage (first time — generates a new key):
    python scripts/encrypt_accounts.py

Usage (reuse existing key from .env, skip the overwrite confirmation):
    python scripts/encrypt_accounts.py --yes

Workflow:
  1. Reads config/accounts.yaml (plain text, NOT committed to git)
  2. Reads FERNET_KEY from .env; generates and saves one if absent
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
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from cryptography.fernet import Fernet
from dotenv import load_dotenv, set_key

from src.config_loader import Account, ConfigError, load_accounts, save_accounts

_YAML_PATH = ROOT / "config" / "accounts.yaml"
_ENC_PATH = ROOT / "config" / "accounts.enc"
_ENV_PATH = ROOT / ".env"

_REQUIRED_FIELDS = ("company", "username", "password")


def _get_or_create_key() -> bytes:
    """Load FERNET_KEY from .env, or generate and save a new one."""
    load_dotenv(_ENV_PATH)
    key_str = os.getenv("FERNET_KEY", "").strip()
    if key_str:
        return key_str.encode()

    print("No FERNET_KEY found in .env — generating a new one…")
    key = Fernet.generate_key()
    # Persist to .env
    _ENV_PATH.touch(exist_ok=True)
    set_key(str(_ENV_PATH), "FERNET_KEY", key.decode())
    # save_accounts()/load_accounts() re-read .env through load_dotenv(),
    # which never overrides a variable already present in os.environ — an
    # empty FERNET_KEY inherited from the shell would therefore shadow the
    # key we just wrote. Export it explicitly so the very next call uses it.
    os.environ["FERNET_KEY"] = key.decode()
    print(f"  ✓ Key saved to {_ENV_PATH}")
    print("  ⚠  Back up this key — without it you cannot decrypt accounts.enc!")
    return key


def _read_yaml(path: Path) -> object:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _as_text(value: object, where: str) -> str:
    # YAML turns an unquoted digit-only password into an int (and a
    # leading-zero one into octal!). Refuse instead of str()-ing it so the
    # operator fixes the source file rather than encrypting a mangled value.
    if not isinstance(value, str):
        raise ConfigError(
            f"{where} 不是字符串（YAML 把未加引号的纯数字解析成了数字）——请给该值加上引号后重跑"
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

        contact = raw.get("contact")
        group = raw.get("group")
        accounts.append(Account(
            company=_as_text(raw["company"], f"accounts[{i}].company"),
            username=username,
            password=_as_text(raw["password"], f"accounts[{i}].password"),
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

    # Key must be in place before load_accounts() looks at the existing file.
    _get_or_create_key()

    had_existing = _ENC_PATH.exists()
    if had_existing:
        interactive = sys.stdin is not None and sys.stdin.isatty()
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
