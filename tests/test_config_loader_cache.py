"""Caching behaviour of src.config_loader.load_accounts().

Every web request (visible_accounts) and every mid-run re-login
(main._current_password) calls load_accounts(); without a cache each call
re-reads .env, decrypts accounts.enc, parses YAML and logs a line.
"""

import os
import threading
from types import SimpleNamespace

import pytest
import yaml
from cryptography.fernet import Fernet
from loguru import logger

from src import config_loader
from src.config_loader import Account, ConfigError, load_accounts, save_accounts

T1 = 1_000_000_000_000_000_000  # fixed st_mtime_ns values (year 2001 / 2033)
T2 = 2_000_000_000_000_000_000


def _acct(username: str, password: str, company: str = "甲公司", **kw) -> dict:
    return {"company": company, "username": username, "password": password,
            "contact": kw.get("contact", "c"), "group": kw.get("group", "")}


def _encrypt(key: bytes, accounts: list[dict]) -> bytes:
    plaintext = yaml.dump({"accounts": accounts}, allow_unicode=True, default_flow_style=False)
    return Fernet(key).encrypt(plaintext.encode("utf-8"))


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real Fernet key + accounts.enc under tmp_path, wired into the loader."""
    key = Fernet.generate_key()
    enc = tmp_path / "accounts.enc"
    monkeypatch.setattr(config_loader, "_ENC_PATH", enc)
    monkeypatch.setattr(config_loader, "_ENV_PATH", tmp_path / ".env")  # absent: env only
    monkeypatch.setenv("FERNET_KEY", key.decode())
    config_loader.invalidate_cache()

    def write(accounts: list[dict], *, key_: "bytes | None" = None, mtime_ns: "int | None" = None):
        # Direct write, NOT save_accounts(): this is "another process wrote it".
        enc.write_bytes(_encrypt(key_ or key, accounts))
        if mtime_ns is not None:
            os.utime(enc, ns=(mtime_ns, mtime_ns))
        return enc

    write([_acct("u1", "p1")])
    yield SimpleNamespace(path=enc, key=key, write=write)
    config_loader.invalidate_cache()


@pytest.fixture
def decrypt_calls(monkeypatch):
    calls: list[bytes] = []
    real = Fernet.decrypt

    def spy(self, token, ttl=None):
        calls.append(token)
        return real(self, token, ttl)

    monkeypatch.setattr(Fernet, "decrypt", spy)
    return calls


@pytest.fixture
def log_records():
    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda m: records.append((m.record["level"].name, m.record["message"])), level="DEBUG"
    )
    yield records
    logger.remove(sink_id)


# ---------------------------------------------------------------------------
# Hit / copy semantics
# ---------------------------------------------------------------------------

def test_second_call_is_served_without_decrypting(store, decrypt_calls):
    first = load_accounts()
    second = load_accounts()
    assert first == second == [Account("甲公司", "u1", "p1", "c", "")]
    assert len(decrypt_calls) == 1


def test_returned_accounts_are_copies(store):
    a = load_accounts()
    a[0].password = "tampered"
    a[0].group = "x"
    a.append(Account("乙", "u9", "p9"))

    b = load_accounts()
    assert b == [Account("甲公司", "u1", "p1", "c", "")]
    assert b[0] is not a[0]


def test_loaded_log_line_only_when_actually_decrypting(store, log_records):
    for _ in range(3):
        load_accounts()
    loaded = [m for lvl, m in log_records if lvl == "INFO" and m.startswith("Loaded ")]
    assert loaded == ["Loaded 1 account(s) from accounts.enc"]

    config_loader.invalidate_cache()
    load_accounts()
    loaded = [m for lvl, m in log_records if lvl == "INFO" and m.startswith("Loaded ")]
    assert len(loaded) == 2


def test_duplicate_company_warning_only_on_real_reload(store, log_records):
    # "甲 公司" and "甲_公司" collide after sanitizing → one warning per decrypt.
    store.write([_acct("u1", "p1", company="甲 公司"), _acct("u2", "p2", company="甲_公司")])
    for _ in range(3):
        assert len(load_accounts()) == 2
    dupes = [m for lvl, m in log_records if lvl == "WARNING" and "公司名称重复" in m]
    assert len(dupes) == 1


def test_invalidate_cache_forces_decrypt(store, decrypt_calls):
    load_accounts()
    config_loader.invalidate_cache()
    assert config_loader._cache is None
    load_accounts()
    assert len(decrypt_calls) == 2


# ---------------------------------------------------------------------------
# Invalidation: in-process save, cross-process rewrite, key, path
# ---------------------------------------------------------------------------

def test_save_accounts_invalidates_immediately(store, decrypt_calls):
    accounts = load_accounts()
    accounts[0].password = "new-pass"
    save_accounts(accounts)
    assert config_loader._cache is None

    assert load_accounts()[0].password == "new-pass"
    assert len(decrypt_calls) == 2


def test_external_rewrite_same_size_is_seen_via_mtime_ns(store, decrypt_calls):
    """Another process (web console) rewrote the file; the daemon's next
    load must see it. Same plaintext length → same ciphertext size, so only
    st_mtime_ns distinguishes the two versions."""
    store.write([_acct("u1", "p1")], mtime_ns=T1)
    assert load_accounts()[0].password == "p1"
    size_before = store.path.stat().st_size

    store.write([_acct("u1", "p2")], mtime_ns=T2)
    assert store.path.stat().st_size == size_before

    assert load_accounts()[0].password == "p2"
    assert len(decrypt_calls) == 2


def test_external_rewrite_same_mtime_is_seen_via_size(store, decrypt_calls):
    store.write([_acct("u1", "p1")], mtime_ns=T1)
    assert load_accounts()[0].password == "p1"

    store.write([_acct("u1", "p1-but-much-longer")], mtime_ns=T1)
    assert load_accounts()[0].password == "p1-but-much-longer"
    assert len(decrypt_calls) == 2


def test_changing_fernet_key_invalidates(store, monkeypatch, decrypt_calls):
    store.write([_acct("u1", "p1")], mtime_ns=T1)
    assert load_accounts()[0].password == "p1"

    # Rotate the key. Same size and same mtime → only the key fingerprint changes.
    key_b = Fernet.generate_key()
    store.write([_acct("u1", "p2")], key_=key_b, mtime_ns=T1)
    monkeypatch.setenv("FERNET_KEY", key_b.decode())
    assert load_accounts()[0].password == "p2"
    assert len(decrypt_calls) == 2

    # A wrong key must fail loudly, never be papered over by the cache.
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    with pytest.raises(ConfigError):
        load_accounts()


def test_pointing_at_another_path_invalidates(store, tmp_path, monkeypatch, decrypt_calls):
    store.write([_acct("u1", "pA")], mtime_ns=T1)
    assert load_accounts()[0].password == "pA"

    other = tmp_path / "other" / "accounts.enc"
    other.parent.mkdir()
    other.write_bytes(_encrypt(store.key, [_acct("u1", "pB")]))
    os.utime(other, ns=(T1, T1))
    assert other.stat().st_size == store.path.stat().st_size  # only the path differs

    monkeypatch.setattr(config_loader, "_ENC_PATH", other)
    assert load_accounts()[0].password == "pB"
    assert len(decrypt_calls) == 2


# ---------------------------------------------------------------------------
# Error paths are never cached and never masked by the cache
# ---------------------------------------------------------------------------

def test_config_errors_are_not_cached(store, monkeypatch):
    store.path.unlink()
    with pytest.raises(ConfigError, match="not found"):
        load_accounts()
    assert config_loader._cache is None

    store.write([])
    with pytest.raises(ConfigError, match="no accounts"):
        load_accounts()
    assert config_loader._cache is None

    store.path.write_bytes(b"garbage")
    with pytest.raises(ConfigError, match="decrypt"):
        load_accounts()
    assert config_loader._cache is None

    monkeypatch.setenv("FERNET_KEY", "")
    with pytest.raises(ConfigError, match="FERNET_KEY"):
        load_accounts()
    assert config_loader._cache is None


def test_corrupted_file_after_a_good_load_raises_not_stale(store):
    assert load_accounts()[0].password == "p1"
    store.path.write_bytes(b"garbage")
    os.utime(store.path, ns=(T2, T2))
    with pytest.raises(ConfigError):
        load_accounts()


# ---------------------------------------------------------------------------
# stat → read → stat
# ---------------------------------------------------------------------------

def test_writer_landing_between_stat_and_read_is_reread(store, monkeypatch, decrypt_calls):
    store.write([_acct("u1", "old")], mtime_ns=T1)
    real_key = config_loader._file_key
    calls: list[int] = []

    def racy_key(path):
        calls.append(1)
        key = real_key(path)
        if len(calls) == 1:
            # The console saves right after our first stat.
            store.write([_acct("u1", "new")], mtime_ns=T2)
        return key

    monkeypatch.setattr(config_loader, "_file_key", racy_key)

    assert load_accounts()[0].password == "new"
    assert len(calls) == 3  # stat, post-read stat (mismatch), post-re-read stat (match)
    assert len(decrypt_calls) == 1  # the torn read is discarded before decrypting
    assert config_loader._cache is not None
    assert config_loader._cache[0][-2:] == real_key(store.path)

    assert load_accounts()[0].password == "new"
    assert len(decrypt_calls) == 1  # cached under the right key


def test_file_that_never_settles_is_returned_but_not_cached(store, monkeypatch, decrypt_calls):
    counter = iter(range(1, 1000))
    monkeypatch.setattr(config_loader, "_file_key", lambda p: (next(counter), 0))

    assert load_accounts()[0].password == "p1"
    assert config_loader._cache is None
    load_accounts()
    assert len(decrypt_calls) == 2


# ---------------------------------------------------------------------------
# Concurrency (web worker threads)
# ---------------------------------------------------------------------------

def test_concurrent_loads_are_consistent(store):
    results: list[list[Account]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        try:
            for _ in range(20):
                r = load_accounts()
                with lock:
                    results.append(r)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert errors == []
    assert len(results) == 160
    assert all(r == [Account("甲公司", "u1", "p1", "c", "")] for r in results)
    assert config_loader._cache is not None
