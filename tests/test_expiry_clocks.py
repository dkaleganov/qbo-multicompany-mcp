"""Intuit has two refresh-token clocks, and each is tracked and reported apart.

Intuit's Authorization FAQ: a refresh token is valid for 100 days of inactivity;
the clock restarts when a refresh is issued a new value, at most once per 24
hours (observed 2026-09-26). Every refresh token also has a five-year maximum,
a hard limit only re-authorization resets. The token response's
``x_refresh_token_expires_in`` is the INACTIVITY clock. The hard clock is a
separate, opt-in response field (``x_refresh_token_hard_expires_in``, on
Intuit's "Set up OAuth 2.0" page), carried by every live token response since
the opt-in was sent (2026-09-24) -- read when present and otherwise preserved
as unknown, with the documented five-year maximum reported as an upper bound
where the consent date is the grant's own birth.

Each clock is named for what it is. The inactivity clock is never called hard.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from qbo_multicompany_mcp import auth_cli, keepalive, oauth
from qbo_multicompany_mcp.constants import (
    REFRESH_HARD_EXPIRY_WARN_DAYS,
    REFRESH_INACTIVITY_WARN_DAYS,
    REFRESH_TOKEN_HARD_LIMIT_SECONDS,
)
from qbo_multicompany_mcp.errors import StoreError
from qbo_multicompany_mcp.store import TokenStore, expiry_warnings

from .conftest import REALM_MAIN, make_token_response
from .test_store_lifecycle import call_tool, seed
from .test_token_store import journal_records, reopen


def _response(*, refresh_days: int = 100, hard_in: timedelta | None = None, **kwargs):
    now = datetime.now(UTC)
    return replace(
        make_token_response(refresh_days=refresh_days, **kwargs),
        refresh_token_hard_expires_at=(now + hard_in) if hard_in is not None else None,
    )


def _tamper(store: TokenStore, realm: str, **fields) -> None:
    with store.lock():
        document = store._load_locked()
        document.rows[realm] = replace(document.rows[realm], **fields)
        store._commit_locked(document)


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, credentials):
    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)
    return credentials


# --------------------------------------------------------------------------
# The response
# --------------------------------------------------------------------------


def test_the_token_response_reads_the_hard_expiry_when_present() -> None:
    body = {
        "access_token": "a", "refresh_token": "r", "expires_in": 3600,
        "x_refresh_token_expires_in": 8640000,
        # The example value on Intuit's "Set up OAuth 2.0" page: five years.
        "x_refresh_token_hard_expires_in": 157680000,
    }
    parsed = oauth._parse_token_response(httpx.Response(200, json=body))
    assert parsed.refresh_token_hard_expires_at is not None
    remaining = parsed.refresh_token_hard_expires_at - datetime.now(UTC)
    assert abs(remaining.total_seconds() - 157680000) < 60
    assert REFRESH_TOKEN_HARD_LIMIT_SECONDS == 157680000


def test_the_token_response_leaves_the_hard_expiry_unknown_when_absent() -> None:
    """Preserve unknown rather than inventing a date."""
    body = {"access_token": "a", "refresh_token": "r", "expires_in": 3600,
            "x_refresh_token_expires_in": 8640000}
    parsed = oauth._parse_token_response(httpx.Response(200, json=body))
    assert parsed.refresh_token_hard_expires_at is None


# --------------------------------------------------------------------------
# The row
# --------------------------------------------------------------------------


def test_a_row_names_its_two_clocks(seeded_store: TokenStore) -> None:
    row = seeded_store.get_row(REALM_MAIN)
    assert 99 < row.days_to_inactivity_expiry() <= 100
    hard = row.hard_expiry()
    assert hard.basis == "unknown"
    assert hard.at is None and hard.days() is None


def test_an_observed_hard_expiry_wins_and_survives_a_refresh_that_omits_it(
    seeded_store: TokenStore, actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: _response(hard_in=timedelta(days=400)))
    row = seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert row.hard_expiry().basis == "observed"
    assert 399 < row.hard_expiry().days() <= 400

    # A later response without the field does not erase what was observed:
    # the value is an absolute timestamp, and no refresh has been observed to
    # move the five-year clock, so the earlier reading stands.
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: _response())
    again = seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert again.hard_expiry().basis == "observed"
    assert 399 < again.hard_expiry().days() <= 400


def test_a_connect_consent_yields_an_upper_bound_and_an_import_does_not(
    store: TokenStore, actor
) -> None:
    """The five-year maximum runs from the grant. A connect commits seconds
    after Intuit issued the grant, so its consent date bounds the hard
    expiry from above. An imported token was granted at some earlier time
    nobody here can see, so no bound is derived: unknown stays unknown."""
    connected = store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor,
        origin="connect",
    )
    bound = connected.hard_expiry()
    assert bound.basis == "upper_bound_from_consent"
    assert bound.at == connected.consented_at + timedelta(seconds=REFRESH_TOKEN_HARD_LIMIT_SECONDS)

    imported = store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor,
        origin="import",
    )
    assert imported.hard_expiry().basis == "unknown"


def test_auth_state_is_expired_past_an_observed_hard_expiry(seeded_store: TokenStore) -> None:
    _tamper(seeded_store, REALM_MAIN,
            refresh_token_hard_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    row = seeded_store.get_row(REALM_MAIN)
    assert row.days_to_inactivity_expiry() > 99, "the inactivity clock is still fine"
    assert row.auth_state(registry_display_name="Acme Main") == "expired"


def test_auth_state_is_expired_past_the_bound_too(seeded_store: TokenStore) -> None:
    """A bound that has passed is a certainty: the grant is at least as old
    as this store's consent record."""
    _tamper(seeded_store, REALM_MAIN, grant_origin="connect",
            consented_at=datetime.now(UTC) - timedelta(seconds=REFRESH_TOKEN_HARD_LIMIT_SECONDS + 1))
    assert seeded_store.get_row(REALM_MAIN).auth_state(registry_display_name="Acme Main") == "expired"


def test_the_journal_carries_the_observed_hard_expiry(
    seeded_store: TokenStore, settings, credentials, actor, monkeypatch: pytest.MonkeyPatch,
    token_key: str,
) -> None:
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: _response(hard_in=timedelta(days=300)))
    original = seeded_store._commit_locked
    armed = {"now": True}

    def commit(document):  # noqa: ANN001, ANN202
        if armed["now"]:
            raise StoreError("simulated failure between the journal and the store")
        return original(document)

    monkeypatch.setattr(seeded_store, "_commit_locked", commit)
    with pytest.raises(StoreError):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert [r.stage for r in journal_records(seeded_store)] == ["result"]
    monkeypatch.undo()
    monkeypatch.setenv("QBO_TOKEN_KEY", token_key)

    adopted = reopen(settings, credentials).get_row(REALM_MAIN)
    assert adopted.auth_generation == 2
    assert adopted.hard_expiry().basis == "observed"
    assert 299 < adopted.hard_expiry().days() <= 300


def test_a_row_with_neither_clock_field_reads_back(seeded_store: TokenStore) -> None:
    """A row with neither field reads as unknown, not an error."""
    from qbo_multicompany_mcp.store import TokenRow

    row = TokenRow.from_json(
        {
            "entity": "acme_main", "environment": "sandbox", "client_id_fingerprint": "f",
            "refresh_token": "zq-bare-refresh-71", "access_token": "zq-bare-access-72",
            "access_token_expires_at": "2030-01-01T00:00:00Z",
            "refresh_token_expires_at": "2030-01-01T00:00:00Z",
            "consented_at": "2026-01-01T00:00:00Z", "auth_generation": 1,
        }
    )
    assert row.grant_origin == "unknown"
    assert row.hard_expiry().basis == "unknown"


# --------------------------------------------------------------------------
# The warnings: each clock under its own name
# --------------------------------------------------------------------------


def test_a_short_inactivity_clock_is_warned_about_as_inactivity_and_never_as_hard(
    store: TokenStore, settings, registry, cli_env, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    seed(store, actor, "acme_main", REALM_MAIN)
    monkeypatch.setattr(
        oauth, "refresh_tokens",
        lambda **kw: make_token_response(refresh_days=REFRESH_INACTIVITY_WARN_DAYS - 1),
    )
    code = keepalive.run(settings, registry, argparse.Namespace(force=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "WARNING: inactivity expiry" in out
    assert "WARNING: hard" not in out
    assert "approaching hard expiry" not in out


def test_an_observed_hard_expiry_is_warned_about_though_the_inactivity_clock_is_full(
    store: TokenStore, settings, registry, cli_env, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """What a single clock would miss: 100 days of inactivity lifetime, one
    day to the five-year limit, and no warning."""
    seed(store, actor, "acme_main", REALM_MAIN)
    monkeypatch.setattr(
        oauth, "refresh_tokens", lambda **kw: _response(refresh_days=100, hard_in=timedelta(days=1))
    )
    code = keepalive.run(settings, registry, argparse.Namespace(force=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "WARNING: hard expiry in 1.0 days" in out
    assert "WARNING: inactivity" not in out


def test_the_five_year_bound_warns_when_close_and_says_it_is_a_bound(
    store: TokenStore, settings, registry, cli_env, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    seed(store, actor, "acme_main", REALM_MAIN)
    _tamper(store, REALM_MAIN, grant_origin="connect",
            consented_at=datetime.now(UTC) - timedelta(seconds=REFRESH_TOKEN_HARD_LIMIT_SECONDS)
            + timedelta(days=REFRESH_HARD_EXPIRY_WARN_DAYS - 5))
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response())
    code = keepalive.run(settings, registry, argparse.Namespace(force=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "WARNING: hard expiry no later than" in out
    assert "five-year maximum" in out
    assert "exact hard expiry unknown" in out


def test_expiry_warnings_are_labelled_per_clock(seeded_store: TokenStore) -> None:
    now = datetime.now(UTC)
    row = seeded_store.get_row(REALM_MAIN)
    assert expiry_warnings(row, now=now) == []

    soon = replace(row, refresh_token_expires_at=now + timedelta(days=3))
    [inactivity] = expiry_warnings(soon, now=now)
    assert inactivity.startswith("inactivity expiry in 3.0 days")
    assert "hard" not in inactivity

    hard = replace(row, refresh_token_hard_expires_at=now + timedelta(days=2))
    [warning] = expiry_warnings(hard, now=now)
    assert warning.startswith("hard expiry in 2.0 days")
    assert "re-consent" in warning


# --------------------------------------------------------------------------
# Where the clocks are reported
# --------------------------------------------------------------------------


def test_list_entities_reports_both_clocks(
    settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    result = call_tool(settings, registry, "list_entities")
    entity = {e["entity"]: e for e in result.structured_content["entities"]}["acme_main"]
    assert entity["days_to_refresh_token_expiry"] > 99
    assert entity["refresh_token_hard_expiry"]["basis"] == "unknown"
    assert entity["refresh_token_hard_expiry"]["days"] is None

    _tamper(store, REALM_MAIN, refresh_token_hard_expires_at=datetime.now(UTC) + timedelta(days=10))
    result = call_tool(settings, registry, "list_entities")
    entity = {e["entity"]: e for e in result.structured_content["entities"]}["acme_main"]
    assert entity["refresh_token_hard_expiry"]["basis"] == "observed"
    assert 9 < entity["refresh_token_hard_expiry"]["days"] <= 10


def test_status_prints_both_clocks_by_name(
    settings, registry, credentials, cli_env, actor, capsys
) -> None:
    seed(settings.open_store(credentials=credentials), actor, "acme_main", REALM_MAIN)
    auth_cli.cmd_status(settings, registry, argparse.Namespace())
    out = capsys.readouterr().out
    assert "inactivity expiry in:" in out
    assert "hard expiry:" in out
    assert "consented at:" in out


def test_the_connect_and_import_commands_record_the_grant_origin(
    settings, registry, credentials, cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qbo_multicompany_mcp.callback_server import Attempt, CallbackParams
    from qbo_multicompany_mcp.constants import DEFAULT_REDIRECT_URI

    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response())
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main")
    store = settings.open_store(credentials=credentials)
    attempt = Attempt(entity_key="acme_main", environment="sandbox",
                      client_fingerprint="a" * 64, redirect_uri=DEFAULT_REDIRECT_URI)
    code = auth_cli._finish_connect(
        settings, store, registry["acme_main"], credentials, attempt,
        CallbackParams(code="c", state=attempt.state, realm_id=REALM_MAIN),
    )
    assert code == 0
    assert store.get_row(REALM_MAIN).grant_origin == "connect"

    monkeypatch.setenv("QBO_IMPORT_REFRESH_TOKEN", "zq-imported-refresh-token-77")
    monkeypatch.setattr(auth_cli.oauth, "refresh_tokens", lambda **kw: make_token_response())

    class _Info:
        payload = {"CompanyInfo": {"CompanyName": "Acme Main"}}

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def company_info(self, entity): return _Info()

    import qbo_multicompany_mcp.client as client_module

    monkeypatch.setattr(client_module, "AccountingClient", _Client)
    code = auth_cli.cmd_import(settings, registry, argparse.Namespace(entity="acme_main"))
    assert code == 0
    assert settings.open_store(credentials=credentials).get_row(REALM_MAIN).grant_origin == "import"
