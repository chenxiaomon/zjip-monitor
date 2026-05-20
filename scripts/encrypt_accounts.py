#!/usr/bin/env python3
"""Encrypt accounts.yaml → accounts.enc.

Usage (first time — generates a new key):
    python scripts/encrypt_accounts.py

Usage (reuse existing key from .env):
    python scripts/encrypt_accounts.py

Workflow:
  1. Reads config/accounts.yaml (plain text, NOT committed to git)
  2. Reads FERNET_KEY from .env; generates and saves one if absent
  3. Writes encrypted config/accounts.enc (safe to commit)
  4. Verifies the round-trip by decrypting and re-parsing

After running this script, delete config/accounts.yaml — the .enc file
is the only copy needed at runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet
from dotenv import load_dotenv, set_key
import os
import yaml


_YAML_PATH = ROOT / "config" / "accounts.yaml"
_ENC_PATH = ROOT / "config" / "accounts.enc"
_ENV_PATH = ROOT / ".env"


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
    print(f"  ✓ Key saved to {_ENV_PATH}")
    print("  ⚠  Back up this key — without it you cannot decrypt accounts.enc!")
    return key


def main() -> int:
    if not _YAML_PATH.exists():
        print(f"ERROR: {_YAML_PATH} not found.")
        print(f"  Copy {ROOT / 'config' / 'accounts.yaml.example'} to {_YAML_PATH}")
        print("  and fill in your account details, then re-run this script.")
        return 1

    # Load and validate YAML
    with open(_YAML_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    accounts = data.get("accounts", [])
    if not accounts:
        print("ERROR: accounts.yaml has no accounts listed.")
        return 1

    # Validate required fields
    for i, acc in enumerate(accounts):
        for field in ("company", "username", "password"):
            if not acc.get(field):
                print(f"ERROR: accounts[{i}] missing required field '{field}'")
                return 1

    print(f"Found {len(accounts)} account(s):")
    for acc in accounts:
        print(f"  - {acc['company']} ({acc['username']})")

    # Encrypt
    key = _get_or_create_key()
    f = Fernet(key)
    plaintext = yaml.dump(data, allow_unicode=True).encode("utf-8")
    ciphertext = f.encrypt(plaintext)

    _ENC_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ENC_PATH.write_bytes(ciphertext)
    print(f"\n✓ Encrypted → {_ENC_PATH}")

    # Verify round-trip
    decrypted = f.decrypt(_ENC_PATH.read_bytes())
    parsed = yaml.safe_load(decrypted.decode("utf-8"))
    assert len(parsed["accounts"]) == len(accounts), "Round-trip verification failed!"
    print("✓ Round-trip verification passed")

    print(f"\nNext steps:")
    print(f"  1. Delete {_YAML_PATH} (it contains plain-text passwords)")
    print(f"  2. Keep .env (with FERNET_KEY) secure — do NOT commit it")
    print(f"  3. accounts.enc is safe to commit to git")
    return 0


if __name__ == "__main__":
    sys.exit(main())
