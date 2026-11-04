"""Proves the keepalive shares the store's lock and refresh, reports per realm,
and distinguishes transient from configuration failure.

The scenario throughout: one fresh realm, one stale realm, one never-authorized
realm.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from qbo_multicompany_mcp import keepalive, oauth
from qbo_multicompany_mcp.constants import REFRESH_INACTIVITY_WARN_DAYS, STALE_REFRESH_SECONDS
from qbo_multicompany_mcp.errors import ConfigurationError, StoreLockTimeout
from qbo_multicompany_mcp.store import TokenStore

from .conftest import REALM_MAIN, REALM_OPS, make_token_response


def args(force: bool = False) -> argparse.Namespace:
    return argparse.Namespace(force=force)


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, credentials):
    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)
    return credentials


def age_row(store: TokenStore, realm: str, *, seconds: float) -> None:
    with store.lock():
        document = store._load_locked()
        document.rows[realm] = replace(
            document.rows[realm],
            last_refresh_at=datetime.now(UTC) - timedelta(seconds=seconds),
        )
        store._commit_locked(document)


@pytest.fixture
def three_realms(store: TokenStore, actor, registry):
    """acme_main is fresh, acme_ops is stale, and one realm is never authorized."""
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )
    store.commit_consent(
        entity="acme_ops", realm_id=REALM_OPS, response=make_token_response(), actor=actor
    )
    age_row(store, REALM_OPS, seconds=STALE_REFRESH_SECONDS + 600)
    return store


def test_stale_is_computed_per_realm(three_realms: TokenStore) -> None:
    """A healthy realm must never mask another's age."""
    rows = three_realms.rows()
    assert rows[REALM_MAIN].is_stale() is False
    assert rows[REALM_OPS].is_stale() is True


def test_a_company_inside_the_hour_is_reported_unchanged(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The line says what happened: a company left alone isn't called refreshed."""
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="a2"))

    code = keepalive.run(settings, registry, args())
    out = capsys.readouterr().out

    assert code == 0
    assert "acme_main: unchanged (still within the hour);" in out
    assert "acme_ops: refreshed;" in out


def test_a_forced_run_reports_a_refresh_inside_the_hour(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="a2"))

    code = keepalive.run(settings, registry, args(force=True))
    out = capsys.readouterr().out

    assert code == 0
    assert "acme_main: refreshed;" in out


def test_only_the_stale_realm_is_refreshed(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    submitted: list[str] = []
    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: (submitted.append(kw["refresh_token"]), make_token_response(access_token="a2"))[1],
    )

    code = keepalive.run(settings, registry, args())
    out = capsys.readouterr().out

    assert code == 0
    assert len(submitted) == 1  # the fresh realm was left alone
    assert "acme_main" in out and "acme_ops" in out


def test_never_authorized_realms_are_skipped_without_a_request(
    store: TokenStore, settings, registry, cli_env, actor,
    monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )
    calls: list[int] = []
    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: (calls.append(1), make_token_response())[1],
    )

    keepalive.run(settings, registry, args(force=True))
    out = capsys.readouterr().out

    assert "acme_ops: never authorized; skipped" in out
    assert len(calls) == 1


# --------------------------------------------------------------------------
# --require-all
# --------------------------------------------------------------------------
#
# By default a configured company with no credential prints "never authorized;
# skipped" and the run exits 0, so a wrapper reading the exit code sees a
# healthy run while that company can answer nothing. --require-all makes such
# a company need attention.


def _one_connected(store: TokenStore, actor, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """acme_main connected, acme_ops configured and never authorized."""
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )
    calls: list[int] = []
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: (calls.append(1), make_token_response())[1])
    return calls


def test_by_default_a_never_authorized_realm_is_skipped_and_the_run_exits_0(
    store: TokenStore, settings, registry, cli_env, actor, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """The default: skipped, and not by itself a reason to exit nonzero."""
    calls = _one_connected(store, actor, monkeypatch)
    assert keepalive.run(settings, registry, args(force=True)) == 0
    assert "acme_ops: never authorized; skipped" in capsys.readouterr().out
    assert len(calls) == 1


def test_require_all_counts_a_never_authorized_realm_as_needing_attention(
    store: TokenStore, settings, registry, cli_env, actor, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    calls = _one_connected(store, actor, monkeypatch)
    code = keepalive.run(settings, registry, argparse.Namespace(force=True, require_all=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "acme_ops: never authorized; needs attention" in out
    assert "acme_main: refreshed" in out, "the connected realm is still refreshed"
    assert len(calls) == 1


def test_the_cli_takes_require_all_and_defaults_it_off() -> None:
    assert keepalive.build_parser().parse_args(["--require-all"]).require_all is True
    assert keepalive.build_parser().parse_args([]).require_all is False


def test_force_refreshes_every_realm(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[str] = []
    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: (submitted.append(kw["refresh_token"]), make_token_response())[1],
    )
    keepalive.run(settings, registry, args(force=True))
    assert len(submitted) == 2


def test_the_hour_criterion_is_rechecked_inside_the_lock() -> None:
    """Deciding outside the lock lets two runs both decide yes, queue, and
    refresh twice -- the second submitting a token the first already spent."""
    import inspect

    from qbo_multicompany_mcp.store import TokenStore as Store

    source = inspect.getsource(Store.refresh)
    body = source.split("with self.lock():", 1)[1]
    assert "min_age_seconds" in body


def test_a_line_is_written_only_after_the_pair_is_on_disk(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The store commits before refresh() returns, so a printed line can never
    claim a refresh that was not persisted."""
    from qbo_multicompany_mcp.errors import StoreError

    def fail_after_response(**kwargs):
        return make_token_response(access_token="never-persisted")

    monkeypatch.setattr(oauth, "refresh_tokens", fail_after_response)

    original = TokenStore._commit_locked

    def failing_commit(self, document):
        raise StoreError("no space left on device")

    monkeypatch.setattr(TokenStore, "_commit_locked", failing_commit)
    keepalive.run(settings, registry, args(force=True))
    monkeypatch.setattr(TokenStore, "_commit_locked", original)

    out = capsys.readouterr().out
    assert "refreshed" not in out
    assert "refresh failed" in out


def test_a_lock_timeout_waits_once_more_then_exits_1(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Exit 2 means "your configuration is wrong and will stay wrong". A busy
    lock is the system working."""
    attempts: list[int] = []

    def always_busy(self, realm_id, **kwargs):
        attempts.append(1)
        raise StoreLockTimeout("busy")

    monkeypatch.setattr(TokenStore, "refresh", always_busy)

    code = keepalive.run(settings, registry, args(force=True))
    out = capsys.readouterr().out

    assert code == 1
    assert "transient" in out
    # Two attempts per realm, for two authorized realms.
    assert len(attempts) == 4


def test_a_configuration_error_exits_2(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def misconfigured(self, realm_id, **kwargs):
        raise ConfigurationError("client id")

    monkeypatch.setattr(TokenStore, "refresh", misconfigured)
    assert keepalive.run(settings, registry, args(force=True)) == 2


def test_one_companys_configuration_error_does_not_stop_the_others(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A row written under another client id is one company's problem. The
    others still need their refresh before their own 100 days run out, and
    each gets its line."""
    real = TokenStore.refresh

    def first_misconfigured(self, realm_id, **kwargs):
        if realm_id == REALM_MAIN:
            raise ConfigurationError("client id")
        return real(self, realm_id, **kwargs)

    monkeypatch.setattr(TokenStore, "refresh", first_misconfigured)
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="a2"))

    code = keepalive.run(settings, registry, args(force=True))
    out = capsys.readouterr().out

    assert code == 2
    assert "acme_main: client id" in out
    assert "acme_ops: refreshed;" in out


@pytest.mark.parametrize(
    ("later", "line"),
    [
        ("connected", "acme_ops: not attempted (Intuit rejected the app's credentials)"),
        ("needs consent", "acme_ops: needs consent; skipped (no request made)"),
    ],
)
def test_rejected_app_credentials_stop_the_run(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys, later, line
) -> None:
    """invalid_client is the app's own credentials, so every company would get
    the same answer: no further refresh token goes out with them. A company
    after it still gets its line, and the one its own state calls for."""
    if later == "needs consent":
        with three_realms.lock():
            document = three_realms._load_locked()
            document.rows[REALM_OPS] = replace(document.rows[REALM_OPS], needs_consent=True)
            three_realms._commit_locked(document)
    attempted: list[str] = []

    def rejected(**kwargs: object):  # noqa: ANN202
        attempted.append(str(kwargs["refresh_token"]))
        raise oauth.InvalidClient("Intuit rejected the client id or secret")

    monkeypatch.setattr(oauth, "refresh_tokens", rejected)
    code = keepalive.run(settings, registry, args(force=True))
    assert code == 2
    assert len(attempted) == 1, "a second company's token went out with rejected credentials"
    lines = capsys.readouterr().out.splitlines()
    assert [each.split(":")[0] for each in lines] == ["acme_main", "acme_ops"]
    assert lines[1] == line


def test_a_busy_store_on_the_first_read_waits_once_more(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The same single extra wait a refresh gets, before the first line."""
    real = TokenStore.read
    reads: list[int] = []

    def busy_once(self):
        reads.append(1)
        if len(reads) == 1:
            raise StoreLockTimeout("busy")
        return real(self)

    monkeypatch.setattr(TokenStore, "read", busy_once)
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="a2"))

    code = keepalive.run(settings, registry, args())
    out = capsys.readouterr().out

    assert code == 0, out
    assert len(reads) >= 2
    assert "acme_ops: refreshed;" in out


def test_an_uncertain_realm_is_never_retried_automatically(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    with three_realms.lock():
        document = three_realms._load_locked()
        document.rows[REALM_OPS] = replace(document.rows[REALM_OPS], refresh_uncertain=True)
        three_realms._commit_locked(document)

    calls: list[int] = []
    monkeypatch.setattr(
        oauth, "refresh_tokens", lambda **kw: (calls.append(1), make_token_response())[1]
    )

    code = keepalive.run(settings, registry, args(force=True))
    out = capsys.readouterr().out

    assert code == 1
    assert "never retried automatically" in out
    assert len(calls) == 1  # only the healthy realm


def test_approaching_inactivity_expiry_is_warned_about_by_its_own_name(
    store: TokenStore, settings, registry, cli_env, actor,
    monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """``x_refresh_token_expires_in`` is the 100-day INACTIVITY clock, which a
    refresh restarts when it is issued a new value. "Hard expiry" is the
    five-year limit, a different clock; tests/test_expiry_clocks.py covers
    both."""
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )
    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: make_token_response(refresh_days=REFRESH_INACTIVITY_WARN_DAYS - 1),
    )

    code = keepalive.run(settings, registry, args(force=True))
    out = capsys.readouterr().out

    assert "WARNING: inactivity expiry" in out
    assert "hard expiry" not in out.split("WARNING", 1)[1]
    assert code == 1


def test_stale_refresh_is_documented_as_passive() -> None:
    """A process that never runs announces nothing, so this cannot be an alert
    about the schedule -- and the docs have to say so, or an operator will
    reasonably assume silence means healthy."""
    from pathlib import Path

    def flat(text: str) -> str:
        return " ".join(text.split())

    assert "passive observation, not an alert" in flat(keepalive.__doc__ or "")
    assert "Monitor the schedule itself." in flat(keepalive.__doc__ or "")
    assert "passive observation, not an alert" in flat(
        Path("docs/design.md").read_text(encoding="utf-8")
    )


def test_keepalive_shares_the_canonical_lock(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qbo_multicompany_mcp import store as store_module

    taken: list[str] = []
    real_lock = store_module.canonical_lock

    def counting_lock(path, timeout=None):
        taken.append(path.name)
        return real_lock(path, timeout) if timeout is not None else real_lock(path)

    monkeypatch.setattr(store_module, "canonical_lock", counting_lock)
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response())

    keepalive.run(settings, registry, args(force=True))
    assert taken and set(taken) == {"tokens.lock"}


def test_a_row_bound_to_another_entity_is_not_refreshed(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A refresh is a credential use, so it checks the whole tuple too.

    The store alone checks environment and app, because it cannot know which
    entity key a realm is supposed to belong to. The keepalive can: it is
    walking the registry. A row whose stored entity disagrees with the registry
    entry for its realm is a configuration error, and the refresh token in it
    is not submitted.
    """
    with three_realms.lock():
        document = three_realms._load_locked()
        document.rows[REALM_OPS] = replace(document.rows[REALM_OPS], entity="acme_main")
        three_realms._commit_locked(document)

    submitted: list[str] = []
    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: (submitted.append(kw["refresh_token"]), make_token_response())[1],
    )

    code = keepalive.run(settings, registry, args(force=True))
    out = capsys.readouterr().out

    assert code == 2
    assert "configuration problem" in out
    assert len(submitted) == 1, "only the correctly bound realm was refreshed"


# --------------------------------------------------------------------------
# Configuration exits 2 wherever it is met
# --------------------------------------------------------------------------


def test_a_configuration_error_on_the_first_store_read_exits_2(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Configuration exits 2 on the first store read -- a store holding
    another environment, a malformed key setting -- as it does at refresh
    time, because 1 is the code a scheduler reads as transient. A permanent
    setup failure is not transient wherever it is met."""

    def misconfigured(self):
        raise ConfigurationError("token store holds environment 'production'")

    monkeypatch.setattr(TokenStore, "read", misconfigured)
    assert keepalive.run(settings, registry, args(force=True)) == 2
    assert "token store unavailable" in capsys.readouterr().out


def test_an_unreadable_store_on_the_first_read_still_exits_1(
    three_realms, settings, registry, cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a store that cannot be read for a reason that is not
    configuration -- a lock that never came free, a torn file -- stays 1."""
    from qbo_multicompany_mcp.errors import StoreError

    def unreadable(self):
        raise StoreError("could not read the token store")

    monkeypatch.setattr(TokenStore, "read", unreadable)
    assert keepalive.run(settings, registry, args(force=True)) == 1
