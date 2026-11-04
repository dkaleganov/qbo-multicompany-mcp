"""The hard-expiry opt-in is sent, and the answer is read.

Intuit's "Set up OAuth 2.0" page shows the opt-in two ways: a body parameter in
its parameter tables and a request header,
``x-include-refresh-token-hard-expires-in: true``, in its request examples. The
response field it unlocks is ``x_refresh_token_hard_expires_in``. This package
sends the HEADER form, on exchange and on refresh: the refresh body policy in
``netpolicy`` admits exactly two form keys, so the body form would be refused
by this package's own policy before it left.

Intuit honoured the header when it was observed in sandbox on 2026-09-24. These
tests pin what is sent, that it passes the policy at the real send boundary,
and what is read back: a response carrying the field sets the hard clock; a
response without it leaves the clock unknown, never a derived date.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qsl

import httpx
import pytest

from qbo_multicompany_mcp import oauth
from qbo_multicompany_mcp.constants import (
    HARD_EXPIRY_OPT_IN_HEADER,
    HARD_EXPIRY_OPT_IN_VALUE,
    REFRESH_TOKEN_HARD_LIMIT_SECONDS,
    REVOKE_ENDPOINT,
    TOKEN_ENDPOINT,
)
from qbo_multicompany_mcp.store import TokenStore

from .conftest import REALM_MAIN

# Distinct from every other fixture value in the suite: a pair received from a
# mock token endpoint is registered as live, and must not collide with a
# synthetic pair another test sends to a loopback mock.
_ACCESS = "zq-hard-expiry-access-0000000001"
_REFRESH = "zq-hard-expiry-refresh-0000000001"
_APP = dict(client_id="test-client-id", client_secret="test-client-secret", environment="sandbox")


def _token_body(*, with_hard_field: bool) -> dict[str, object]:
    body: dict[str, object] = {
        "access_token": _ACCESS,
        "refresh_token": _REFRESH,
        "expires_in": 3600,
        "x_refresh_token_expires_in": 8640000,
        "token_type": "bearer",
    }
    if with_hard_field:
        # The example value on Intuit's "Set up OAuth 2.0" page: five years.
        body["x_refresh_token_hard_expires_in"] = REFRESH_TOKEN_HARD_LIMIT_SECONDS
    return body


def _recording(
    respond: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.Client, list[httpx.Request]]:
    """A real httpx client whose transport records what reached it."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return respond(request)

    return httpx.Client(transport=httpx.MockTransport(handler)), sent


def _answer(*, with_hard_field: bool) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(
        200, json=_token_body(with_hard_field=with_hard_field), request=request
    )


def _form_keys(request: httpx.Request) -> set[str]:
    return {key for key, _value in parse_qsl(request.content.decode(), keep_blank_values=True)}


# --------------------------------------------------------------------------
# What is sent
# --------------------------------------------------------------------------


def test_the_opt_in_header_is_the_documented_name_and_value() -> None:
    """Pinned by value: a header of a different spelling is a request Intuit
    would read as carrying no opt-in at all."""
    assert HARD_EXPIRY_OPT_IN_HEADER == "x-include-refresh-token-hard-expires-in"
    assert HARD_EXPIRY_OPT_IN_VALUE == "true"


def test_a_refresh_sends_the_hard_expiry_opt_in_header() -> None:
    client, sent = _recording(_answer(with_hard_field=False))
    with client:
        oauth.refresh_tokens(refresh_token="zq-submitted-refresh-0001", client=client, **_APP)

    # Through the real send boundary: the policy saw this request and let it
    # pass with the header on it.
    assert len(sent) == 1
    request = sent[0]
    assert str(request.url) == TOKEN_ENDPOINT
    assert request.headers[HARD_EXPIRY_OPT_IN_HEADER] == HARD_EXPIRY_OPT_IN_VALUE
    # The header form, not the body form: the body carries exactly the two
    # keys the refresh policy admits.
    assert _form_keys(request) == {"grant_type", "refresh_token"}


def test_an_exchange_sends_the_hard_expiry_opt_in_header() -> None:
    client, sent = _recording(_answer(with_hard_field=False))
    with client:
        oauth.exchange_code(
            code="zq-code-0001", redirect_uri="http://localhost:8765/callback", client=client, **_APP
        )

    assert len(sent) == 1
    request = sent[0]
    assert str(request.url) == TOKEN_ENDPOINT
    assert request.headers[HARD_EXPIRY_OPT_IN_HEADER] == HARD_EXPIRY_OPT_IN_VALUE
    assert _form_keys(request) == {"grant_type", "code", "redirect_uri"}


def test_a_revocation_does_not_carry_the_opt_in() -> None:
    """The opt-in belongs to the two calls that issue a token. A revocation
    issues nothing, and Intuit's page shows no such header on it."""
    client, sent = _recording(lambda request: httpx.Response(200, request=request))
    with client:
        oauth.revoke_token(token="zq-revoked-refresh-0001", client=client, **_APP)

    assert len(sent) == 1
    assert str(sent[0].url) == REVOKE_ENDPOINT
    assert HARD_EXPIRY_OPT_IN_HEADER not in sent[0].headers


# --------------------------------------------------------------------------
# What is read, through the real call rather than the parser alone
# --------------------------------------------------------------------------


def test_a_refresh_answer_carrying_the_field_sets_the_hard_clock() -> None:
    client, _sent = _recording(_answer(with_hard_field=True))
    with client:
        response = oauth.refresh_tokens(
            refresh_token="zq-submitted-refresh-0002", client=client, **_APP
        )

    assert response.refresh_token_hard_expires_at is not None
    remaining = (response.refresh_token_hard_expires_at - datetime.now(UTC)).total_seconds()
    assert abs(remaining - REFRESH_TOKEN_HARD_LIMIT_SECONDS) < 60


def test_a_refresh_answer_without_the_field_leaves_the_hard_clock_unknown() -> None:
    """Never a derived date: the opt-in having been SENT proves nothing about
    what came back, and an absent field is unknown."""
    client, sent = _recording(_answer(with_hard_field=False))
    with client:
        response = oauth.refresh_tokens(
            refresh_token="zq-submitted-refresh-0003", client=client, **_APP
        )

    assert sent[0].headers[HARD_EXPIRY_OPT_IN_HEADER] == HARD_EXPIRY_OPT_IN_VALUE
    assert response.refresh_token_hard_expires_at is None


# --------------------------------------------------------------------------
# End to end: the store records what the endpoint returned
# --------------------------------------------------------------------------


@pytest.fixture
def token_endpoint(monkeypatch: pytest.MonkeyPatch):
    """Route the client ``oauth._post`` builds for itself to a mock transport,
    so ``TokenStore.refresh`` runs the real ``refresh_tokens`` end to end."""
    real_client = httpx.Client

    def install(respond: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        sent: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return respond(request)

        def client(**kwargs):  # noqa: ANN003, ANN202
            # The network policy's builder passes transport=None for httpx to
            # build one; the mock takes that place.
            kwargs.pop("transport", None)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(oauth.httpx, "Client", client)
        return sent

    return install


def test_the_store_records_an_observed_hard_expiry_from_a_real_refresh(
    seeded_store: TokenStore, actor, token_endpoint
) -> None:
    sent = token_endpoint(_answer(with_hard_field=True))

    row = seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    assert [r.headers.get(HARD_EXPIRY_OPT_IN_HEADER) for r in sent] == [HARD_EXPIRY_OPT_IN_VALUE]
    assert row.hard_expiry().basis == "observed"
    assert 1824 < row.hard_expiry().days() <= 1825
    # And it is what was persisted, not only what was returned.
    assert seeded_store.get_row(REALM_MAIN).hard_expiry().basis == "observed"


def test_the_store_keeps_the_hard_clock_unknown_when_a_real_refresh_omits_it(
    seeded_store: TokenStore, actor, token_endpoint
) -> None:
    sent = token_endpoint(_answer(with_hard_field=False))

    row = seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    assert [r.headers.get(HARD_EXPIRY_OPT_IN_HEADER) for r in sent] == [HARD_EXPIRY_OPT_IN_VALUE]
    assert row.auth_generation == 2, "the refresh itself went through"
    assert row.hard_expiry().basis == "unknown"
    assert row.hard_expiry().at is None
