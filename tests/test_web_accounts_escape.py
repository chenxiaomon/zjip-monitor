"""Reflected-XSS and error-disclosure guards on the accounts routes.

`/accounts/{username}/token-status` and `/scan-status` answer 200 text/html for
ANY username — the unknown-account branch is exactly the reflection sink. The
session cookie is SameSite=Lax, so a top-level navigation from a link in chat
or email carries it, and there is no Content-Security-Policy to fall back on.
"""

import pytest

from src.config_loader import ConfigError
from tests.conftest import ADMIN
from web.routes import accounts as accounts_routes

PAYLOAD = '"><img src=x onerror=alert(1)>'


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/accounts/{u}/token-status"),
        ("get", "/accounts/{u}/scan-status"),
        ("post", "/accounts/{u}/relogin"),
        ("post", "/accounts/{u}/scan"),
        ("post", "/accounts/{u}/group"),
    ],
)
def test_unknown_username_is_escaped_not_reflected(
    web_client, login_as, fake_accounts, isolated_snapshots, method, path
):
    login_as(web_client, *ADMIN)
    url = path.format(u=PAYLOAD)

    kwargs = {"data": {"group": ""}} if method == "post" else {}
    resp = getattr(web_client, method)(url, **kwargs)

    assert resp.status_code == 200
    # The payload must stay *inside* the quoted attribute: no new element, and
    # no quote that could close it. (The characters may survive as entities —
    # that is the point of escaping, not a leak.)
    assert "<img" not in resp.text
    assert PAYLOAD not in resp.text
    assert '"><img' not in resp.text
    assert "&lt;img" in resp.text and "&quot;" in resp.text


def test_row_helpers_escape_quotes_and_markup():
    body = accounts_routes._row_error('a"b', "<y>").body.decode()
    assert '"' not in body.split('id="row-')[1].split('"')[0]
    assert "<y>" not in body

    empty = accounts_routes._row_empty('a"><b').body.decode()
    assert "<b" not in empty


def test_public_error_hides_config_internals(caplog):
    exc = ConfigError(
        "/opt/zjip-monitor/config/accounts.enc not found. "
        "Run 'python scripts/encrypt_accounts.py' to create it."
    )
    msg = accounts_routes._public_error(exc, "load failed", username="u1")

    assert "accounts.enc" not in msg
    assert "encrypt_accounts.py" not in msg
    assert "日志" in msg


def test_public_error_generic_exception(caplog):
    msg = accounts_routes._public_error(OSError(28, "No space left on device"), "save failed")
    assert "No space left" not in msg
    assert "日志" in msg
