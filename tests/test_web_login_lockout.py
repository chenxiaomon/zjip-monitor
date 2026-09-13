"""Console login rate limiting.

The only brake used to be `await asyncio.sleep(0.5)`, which does not block the
event loop — a hundred concurrent attempts still ran at full speed. The console
sits on a public IP over plain HTTP, so a weak WEB_PASS was brute-forceable at
network speed.
"""

import pytest

from tests.conftest import ADMIN
from web import login_guard


@pytest.fixture(autouse=True)
def _clean_guard():
    login_guard.reset()
    yield
    login_guard.reset()


# ---------------------------------------------------------------------------
# unit
# ---------------------------------------------------------------------------

def test_lockout_after_the_configured_number_of_failures():
    for _ in range(login_guard.MAX_FAILURES - 1):
        assert login_guard.record_failure("1.2.3.4", "admin") is False
        assert login_guard.seconds_locked("1.2.3.4", "admin") == 0

    assert login_guard.record_failure("1.2.3.4", "admin") is True
    assert 0 < login_guard.seconds_locked("1.2.3.4", "admin") <= login_guard.LOCK_SECONDS


def test_lockout_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(login_guard, "_now", lambda: clock[0])

    for _ in range(login_guard.MAX_FAILURES):
        login_guard.record_failure("1.2.3.4", "admin")
    assert login_guard.seconds_locked("1.2.3.4", "admin") > 0

    clock[0] += login_guard.LOCK_SECONDS + 1
    assert login_guard.seconds_locked("1.2.3.4", "admin") == 0


def test_one_users_typos_do_not_lock_another():
    for _ in range(login_guard.MAX_FAILURES):
        login_guard.record_failure("1.2.3.4", "ga")
    assert login_guard.seconds_locked("1.2.3.4", "ga") > 0
    assert login_guard.seconds_locked("1.2.3.4", "admin") == 0


def test_username_rotation_still_trips_the_ip_counter():
    """An attacker cycling usernames never fills one user bucket."""
    for i in range(login_guard.IP_MAX_FAILURES):
        login_guard.record_failure("9.9.9.9", f"user{i}")
    assert login_guard.seconds_locked("9.9.9.9", "brand-new-name") > 0


def test_success_clears_the_counters():
    for _ in range(login_guard.MAX_FAILURES - 1):
        login_guard.record_failure("1.2.3.4", "admin")
    login_guard.record_success("1.2.3.4", "admin")
    assert login_guard.record_failure("1.2.3.4", "admin") is False


def test_client_ip_prefers_proxy_headers():
    from types import SimpleNamespace

    def req(headers, host="127.0.0.1"):
        return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))

    assert login_guard.client_ip(req({"x-forwarded-for": "203.0.113.9, 10.0.0.1"})) == "203.0.113.9"
    assert login_guard.client_ip(req({"x-real-ip": "203.0.113.9"})) == "203.0.113.9"
    assert login_guard.client_ip(req({})) == "127.0.0.1"
    assert login_guard.client_ip(SimpleNamespace(headers={}, client=None)) == "unknown"


def test_masking_never_prints_the_whole_username(caplog):
    assert login_guard.mask("hwtxqc123") == "hw******3"
    assert "hwtxqc123" not in login_guard.mask("hwtxqc123")
    assert login_guard.mask("") == "***"


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

def test_form_login_locks_out_and_then_refuses_the_right_password(web_client):
    for _ in range(login_guard.MAX_FAILURES):
        resp = web_client.post(
            "/login", data={"username": ADMIN[0], "password": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 401

    resp = web_client.post(
        "/login", data={"username": ADMIN[0], "password": ADMIN[1]},
        follow_redirects=False,
    )
    assert resp.status_code == 429
    assert resp.headers["Retry-After"].isdigit()
    assert "失败次数过多" in resp.text


def test_successful_login_resets_the_counter(web_client):
    for _ in range(login_guard.MAX_FAILURES - 1):
        web_client.post("/login", data={"username": ADMIN[0], "password": "wrong"},
                        follow_redirects=False)

    ok = web_client.post("/login", data={"username": ADMIN[0], "password": ADMIN[1]},
                         follow_redirects=False)
    assert ok.status_code == 303

    again = web_client.post("/login", data={"username": ADMIN[0], "password": "wrong"},
                            follow_redirects=False)
    assert again.status_code == 401  # not 429 — the counter was cleared


def test_basic_auth_shares_the_same_lockout(web_client, isolated_snapshots):
    """Otherwise the form's rate limit is bypassed by brute-forcing the header."""
    for _ in range(login_guard.MAX_FAILURES):
        assert web_client.get("/api/status", auth=(ADMIN[0], "wrong")).status_code == 401

    assert web_client.get("/api/status", auth=ADMIN).status_code == 401  # locked out
    login_guard.reset()
    assert web_client.get("/api/status", auth=ADMIN).status_code == 200
