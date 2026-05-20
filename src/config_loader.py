"""Load and decrypt account configuration.

Runtime usage:
    from src.config_loader import load_accounts
    accounts = load_accounts()
    # [{"company": "...", "username": "...", "password": "...", "contact": "..."}, ...]

Expects:
    config/accounts.enc  — Fernet-encrypted YAML
    .env                 — contains FERNET_KEY
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from loguru import logger

_ROOT = Path(__file__).parent.parent
_ENC_PATH = _ROOT / "config" / "accounts.enc"
_ENV_PATH = _ROOT / ".env"


@dataclass
class Account:
    company: str
    username: str
    password: str
    contact: str = ""

    def __repr__(self) -> str:
        return f"Account(company={self.company!r}, username={self.username!r})"


class ConfigError(Exception):
    """Raised when account config cannot be loaded or decrypted."""


def load_accounts() -> list[Account]:
    """Load and decrypt all accounts from config/accounts.enc.

    Returns:
        List of Account dataclasses.

    Raises:
        ConfigError: File missing, key missing, or decryption failed.
    """
    load_dotenv(_ENV_PATH)

    if not _ENC_PATH.exists():
        raise ConfigError(
            f"{_ENC_PATH} not found. "
            "Run 'python scripts/encrypt_accounts.py' to create it."
        )

    key_str = os.getenv("FERNET_KEY", "").strip()
    if not key_str:
        raise ConfigError(
            "FERNET_KEY not set in .env. "
            "Run 'python scripts/encrypt_accounts.py' to generate one."
        )

    try:
        f = Fernet(key_str.encode())
        plaintext = f.decrypt(_ENC_PATH.read_bytes())
    except InvalidToken as exc:
        raise ConfigError(
            "Failed to decrypt accounts.enc — the FERNET_KEY may be wrong "
            "or the file may be corrupted."
        ) from exc
    except Exception as exc:
        raise ConfigError(f"Unexpected error decrypting accounts.enc: {exc}") from exc

    try:
        data = yaml.safe_load(plaintext.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Decrypted data is not valid YAML: {exc}") from exc

    raw_accounts = data.get("accounts", [])
    if not raw_accounts:
        raise ConfigError("accounts.enc decrypted successfully but contains no accounts.")

    accounts: list[Account] = []
    for i, raw in enumerate(raw_accounts):
        missing = [f for f in ("company", "username", "password") if not raw.get(f)]
        if missing:
            logger.warning(f"accounts[{i}] missing fields {missing} — skipping")
            continue
        accounts.append(Account(
            company=raw["company"],
            username=raw["username"],
            password=raw["password"],
            contact=raw.get("contact", ""),
        ))

    logger.info(f"Loaded {len(accounts)} account(s) from accounts.enc")
    return accounts


def save_accounts(accounts: list[Account]) -> None:
    """Re-encrypt and overwrite config/accounts.enc with the given list.

    Raises:
        ConfigError: FERNET_KEY missing or encryption fails.
    """
    load_dotenv(_ENV_PATH)
    key_str = os.getenv("FERNET_KEY", "").strip()
    if not key_str:
        raise ConfigError("FERNET_KEY not set in .env")

    data = {
        "accounts": [
            {
                "company": a.company,
                "username": a.username,
                "password": a.password,
                "contact": a.contact,
            }
            for a in accounts
        ]
    }
    plaintext = yaml.dump(data, allow_unicode=True, default_flow_style=False).encode("utf-8")
    try:
        encrypted = Fernet(key_str.encode()).encrypt(plaintext)
    except Exception as exc:
        raise ConfigError(f"Encryption failed: {exc}") from exc

    _ENC_PATH.write_bytes(encrypted)
    logger.info(f"accounts.enc updated: {len(accounts)} account(s)")
