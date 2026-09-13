"""Load and decrypt account configuration.

Runtime usage:
    from src.config_loader import load_accounts
    accounts = load_accounts()
    # [{"company": "...", "username": "...", "password": "...", "contact": "..."}, ...]

Expects:
    config/accounts.enc  — Fernet-encrypted YAML
    .env                 — contains FERNET_KEY

load_accounts() is called on every web request (visible_accounts) and on
every mid-run re-login (main._current_password), so its result is cached on
(path, key fingerprint, st_mtime_ns, st_size). Callers get copies and may
mutate them freely; save_accounts() drops the cache so a same-process write
is visible at once, while a write from the other process (web ↔ daemon) is
picked up through the file key.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from loguru import logger

from .fsutil import atomic_write_bytes

_ROOT = Path(__file__).parent.parent
_ENC_PATH = _ROOT / "config" / "accounts.enc"
_ENV_PATH = _ROOT / ".env"

# How many times to re-read when the file changes under us (see load_accounts).
_MAX_READ_ATTEMPTS = 3

_cache_lock = threading.Lock()
# ((str(path), key fingerprint, st_mtime_ns, st_size), accounts) — `None`
# means "nothing cached". Tests reset it directly; keep the name.
_cache: tuple[tuple[str, str, int, int], list["Account"]] | None = None


@dataclass
class Account:
    company: str
    username: str
    password: str
    contact: str = ""
    group: str = ""  # 分组 key（对应 settings.yaml groups；空 = 未分组）

    def __repr__(self) -> str:
        return f"Account(company={self.company!r}, username={self.username!r})"


class ConfigError(Exception):
    """Raised when account config cannot be loaded or decrypted."""


def invalidate_cache() -> None:
    """Forget the decrypted accounts; the next load_accounts() re-reads the file."""
    global _cache
    with _cache_lock:
        _cache = None


def _file_key(path: Path) -> tuple[int, int]:
    """Identity of the file's current content (see settings_cache._file_key
    for why mtime alone is not enough). Fernet tokens for equal-length
    plaintexts have equal length, so a password edit usually changes only
    st_mtime_ns — that component is what makes cross-process edits visible."""
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def _key_fingerprint(key_str: str) -> str:
    # Never keep the key itself in the cache key: a rotated FERNET_KEY must
    # force a fresh decrypt, and a hash prefix is enough to tell keys apart.
    return hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16]


def _copies(accounts: list[Account]) -> list[Account]:
    # Callers (account edit/group routes) mutate what they get before saving;
    # the cache must hold what is on disk, not what a request is about to write.
    return [dataclasses.replace(a) for a in accounts]


def load_accounts() -> list[Account]:
    """Load and decrypt all accounts from config/accounts.enc.

    Returns:
        List of Account dataclasses (fresh copies — safe to mutate).

    Raises:
        ConfigError: File missing, key missing, or decryption failed.
    """
    global _cache
    load_dotenv(_ENV_PATH)

    enc_path = _ENC_PATH  # one consistent value per call (tests monkeypatch it)
    if not enc_path.exists():
        raise ConfigError(
            f"{enc_path} not found. "
            "Run 'python scripts/encrypt_accounts.py' to create it."
        )

    key_str = os.getenv("FERNET_KEY", "").strip()
    if not key_str:
        raise ConfigError(
            "FERNET_KEY not set in .env. "
            "Run 'python scripts/encrypt_accounts.py' to generate one."
        )

    identity = (str(enc_path), _key_fingerprint(key_str))
    try:
        file_key = _file_key(enc_path)
    except OSError as exc:
        raise ConfigError(f"Unexpected error reading accounts.enc: {exc}") from exc

    with _cache_lock:
        if _cache is not None and _cache[0] == identity + file_key:
            return _copies(_cache[1])

    # stat → read → stat, same reasoning as settings_cache.get_settings():
    # a console save landing between our stat and read must not get filed
    # under the pre-save key, or the daemon would keep the stale password.
    ciphertext = b""
    stable = False
    for _ in range(_MAX_READ_ATTEMPTS):
        try:
            ciphertext = enc_path.read_bytes()
            after = _file_key(enc_path)
        except OSError as exc:
            raise ConfigError(f"Unexpected error reading accounts.enc: {exc}") from exc
        if after == file_key:
            stable = True
            break
        file_key = after

    # Decrypt outside the lock: it is the slow part, and a miss under
    # contention is rare (startup, or right after an edit).
    try:
        plaintext = Fernet(key_str.encode()).decrypt(ciphertext)
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

    raw_accounts = (data or {}).get("accounts", [])
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
            group=raw.get("group", ""),
        ))

    # Snapshots/attachments/events are keyed by the SANITIZED company name —
    # compare on that key, since names that differ only in punctuation still
    # collide on disk and overwrite each other's history.
    by_company: dict[str, list[str]] = {}
    for a in accounts:
        key = "".join(c if c.isalnum() or c in "-_" else "_" for c in a.company)
        by_company.setdefault(key, []).append(a.username)
    for key, users in by_company.items():
        if len(users) > 1:
            logger.warning(
                f"公司名称重复（脱敏后同名 {key!r}）：被账号 {users} 共用 — 它们的快照/附件会互相覆盖，请改用不同公司名称"
            )

    logger.info(f"Loaded {len(accounts)} account(s) from accounts.enc")

    if stable:
        with _cache_lock:
            _cache = (identity + file_key, accounts)
    return _copies(accounts)


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
                "group": a.group,
            }
            for a in accounts
        ]
    }
    plaintext = yaml.dump(data, allow_unicode=True, default_flow_style=False).encode("utf-8")
    try:
        encrypted = Fernet(key_str.encode()).encrypt(plaintext)
    except Exception as exc:
        raise ConfigError(f"Encryption failed: {exc}") from exc

    # Keep a one-generation backup: a bad write must never be the only copy
    if _ENC_PATH.exists():
        try:
            _ENC_PATH.with_suffix(".enc.bak").write_bytes(_ENC_PATH.read_bytes())
        except OSError as exc:
            logger.warning(f"Could not write accounts.enc.bak: {exc}")
    atomic_write_bytes(_ENC_PATH, encrypted)
    # The file key would change too, but not necessarily within this same
    # process's next call on a coarse-mtime filesystem — drop it explicitly.
    invalidate_cache()
    logger.info(f"accounts.enc updated: {len(accounts)} account(s)")
