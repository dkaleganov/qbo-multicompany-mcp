"""Proves the guard verifies the company name without ever routing on it, and
without echoing it to a model."""

from __future__ import annotations


import pytest

from qbo_multicompany_mcp.errors import GuardMismatch, guard_mismatch_text
from qbo_multicompany_mcp.guard import describe_mismatch, difference_class, names_match
from qbo_multicompany_mcp.session import QboSession

from .conftest import REALM_MAIN, REALM_OPS, MockIntuit, make_token_response


def test_comparison_is_exact() -> None:
    assert names_match("Acme Main", "Acme Main")
    assert not names_match("Acme Main", "acme main")
    assert not names_match("Acme Main", "Acme Main ")


@pytest.mark.parametrize(
    ("registry_name", "intuit_name", "expected"),
    [
        ("Acme Main", "Acme Main ", "whitespace"),
        ("Acme Main", "Acme Main", "whitespace"),
        ("Acme Main", "acme main", "case"),
        ("Acme, Inc.", "Acme Inc", "punctuation"),
        ("Acme Main", "Other Company", "other"),
        ("Acme Main", "Acme Main", "none"),
    ],
)
def test_difference_class_names_the_likeliest_fix(registry_name, intuit_name, expected) -> None:
    assert difference_class(registry_name, intuit_name) == expected


def test_cli_prints_both_values_with_repr() -> None:
    """An invisible difference -- a trailing space, a non-breaking space -- has
    to be visible on the page."""
    report = describe_mismatch("Acme Main", "Acme Main ")
    assert "'Acme Main'" in report
    assert "'Acme Main '" in report
    assert "differs by: whitespace" in report


def test_tool_error_echoes_neither_name() -> None:
    text = guard_mismatch_text("acme_main")
    assert "acme_main" in text
    assert "Acme" not in text
    assert "qbo-multicompany-mcp-auth" in text


# --------------------------------------------------------------------------
# Behaviour in a session
# --------------------------------------------------------------------------


def seed(store, actor, entity_key: str, realm: str) -> None:
    store.commit_consent(
        entity=entity_key,
        realm_id=realm,
        response=make_token_response(),
        actor=actor,
    )


def test_guard_verifies_once_per_entity_per_process(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """Re-verifying on every call would double this server's request count
    permanently."""
    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)

    session = QboSession(mock_settings, registry)
    entity = registry["acme_main"]

    session.ensure_ready(entity)
    first = len([r for r in mock_intuit.requests if "companyinfo" in r[1]])
    session.ensure_ready(entity)
    session.ensure_ready(entity)
    second = len([r for r in mock_intuit.requests if "companyinfo" in r[1]])

    assert first == 1
    assert second == 1


def test_guard_reverifies_after_a_reconsent(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """A refresh cannot change which company a realm points at. A re-consent
    can, so the consent epoch is the cache key."""
    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    session = QboSession(mock_settings, registry)
    entity = registry["acme_main"]

    session.ensure_ready(entity)
    assert len([r for r in mock_intuit.requests if "companyinfo" in r[1]]) == 1

    import time

    time.sleep(0.01)
    seed(store, actor, "acme_main", REALM_MAIN)  # re-consent
    session.ensure_ready(entity)
    assert len([r for r in mock_intuit.requests if "companyinfo" in r[1]]) == 2


def test_mismatch_raises_for_that_entity_and_leaves_others_working(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    seed(store, actor, "acme_ops", REALM_OPS)

    mock_intuit.company_name = "Renamed Elsewhere"
    session = QboSession(mock_settings, registry)

    with pytest.raises(GuardMismatch):
        session.ensure_ready(registry["acme_main"])

    mock_intuit.company_name = "Acme Ops"
    ready = session.ensure_ready(registry["acme_ops"])
    assert ready.state.company_name_matches_registry is True


def test_mismatch_is_persisted_so_list_entities_needs_no_request(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    mock_intuit.company_name = "Renamed Elsewhere"

    session = QboSession(mock_settings, registry)
    with pytest.raises(GuardMismatch):
        session.ensure_ready(registry["acme_main"])

    row = store.get_row(REALM_MAIN)
    assert row is not None
    assert row.name_mismatch is not None
    assert row.name_mismatch.mismatch is True
    assert row.name_mismatch.auth_generation == row.auth_generation
    assert row.auth_state(registry_display_name="Acme Main") == "name_mismatch"


def test_a_matching_name_persists_a_clean_verdict_and_capabilities(
    mock_settings, registry, credentials, actor
) -> None:
    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)

    QboSession(mock_settings, registry).ensure_ready(registry["acme_main"])

    row = store.get_row(REALM_MAIN)
    assert row is not None
    assert row.name_mismatch is not None and row.name_mismatch.mismatch is False
    assert row.capabilities is not None
    assert row.capabilities["accounting_basis"] == "Accrual"


def test_routing_is_by_realm_not_by_name(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """Two companies may carry byte-identical names, so the name cannot decide
    where a request goes."""
    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    seed(store, actor, "acme_ops", REALM_OPS)

    session = QboSession(mock_settings, registry)
    mock_intuit.company_name = "Acme Main"
    session.ensure_ready(registry["acme_main"])

    paths = [r[1] for r in mock_intuit.requests]
    assert all(REALM_MAIN in path for path in paths)
    assert not any(REALM_OPS in path for path in paths)
