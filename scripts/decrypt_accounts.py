#!/usr/bin/env python3
"""Decrypt accounts.enc for inspection.

Usage:
    python scripts/decrypt_accounts.py                    # YAML on stdout, passwords masked (****ab)
    python scripts/decrypt_accounts.py --show-passwords   # YAML on stdout, passwords in clear
    python scripts/decrypt_accounts.py --out PATH         # plain YAML written to PATH with mode 0600

Reads config/accounts.enc with the FERNET_KEY from .env, exactly like the
daemon and the web console do. The output has the same shape as
config/accounts.yaml.example, so a file produced with --out can be fed back
into scripts/encrypt_accounts.py after editing.

Exit codes:
    0 — success
    1 — accounts.enc missing / key missing / decryption failed
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from src.config_loader import Account, ConfigError, load_accounts


def mask_password(password: str) -> str:
    """Hide a password, keeping the last two characters when that reveals
    little: enough to tell "did I change it?" apart, useless for logging in."""
    if len(password) > 4:
        return "****" + password[-2:]
    return "****"


def accounts_to_yaml(accounts: list[Account], *, show_passwords: bool) -> str:
    """Serialize in the accounts.yaml.example layout (field order preserved)."""
    data = {
        "accounts": [
            {
                "company": a.company,
                "username": a.username,
                "password": a.password if show_passwords else mask_password(a.password),
                "contact": a.contact,
                "group": a.group,
            }
            for a in accounts
        ]
    }
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def write_private_file(path: Path, text: str) -> None:
    """Write `text` so the file is 0600 from its first byte.

    os.open applies the mode at creation; a file that already exists (maybe
    0644 from an earlier manual copy) is tightened BEFORE we put plain-text
    passwords into it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        os.chmod(path, 0o600)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="解密 config/accounts.enc 并以 accounts.yaml 的格式输出（默认密码打码）",
    )
    parser.add_argument(
        "--show-passwords",
        action="store_true",
        help="在 stdout 明文打印密码（默认打码为 ****，保留末 2 位）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        metavar="PATH",
        help="把含明文密码的 YAML 写到 PATH（权限 0600），stdout 只报告数量",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        accounts = load_accounts()
    except ConfigError as exc:
        print(f"错误：无法读取账号配置 — {exc}", file=sys.stderr)
        return 1

    if args.out is not None:
        try:
            write_private_file(args.out, accounts_to_yaml(accounts, show_passwords=True))
        except OSError as exc:
            print(f"错误：写入 {args.out} 失败 — {exc}", file=sys.stderr)
            return 1
        print(f"已写入 {len(accounts)} 个账号到 {args.out}")
        print(
            f"警告：{args.out} 含明文密码（权限已设为 0600），用完请立即删除：rm {args.out}",
            file=sys.stderr,
        )
        return 0

    sys.stdout.write(accounts_to_yaml(accounts, show_passwords=args.show_passwords))
    if not args.show_passwords:
        print(
            "提示：密码已打码；--show-passwords 明文显示，--out PATH 写入 0600 文件",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
