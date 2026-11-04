"""Proves get_company_info answers through a guard mismatch while every other
tool fails closed, list tools share one envelope, and the suite stays off the
real callback port.

Named after the failure each test prevents, not after the code it touches.
"""

from __future__ import annotations

import pytest

from qbo_multicompany_mcp.constants import CALLBACK_PORT
from qbo_multicompany_mcp.errors import GuardMismatch
from qbo_multicompany_mcp.session import QboSession

from .conftest import REALM_MAIN, MockIntuit, make_token_response


def seed(store, actor, entity_key: str, realm: str) -> None:
    store.commit_consent(
        entity=entity_key, realm_id=realm, response=make_token_response(), actor=actor
    )


# --------------------------------------------------------------------------
# get_company_info answers through a guard mismatch
# --------------------------------------------------------------------------


def test_get_company_info_answers_when_the_guard_trips(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """It is the one tool that should answer.

    A model can then tell the operator exactly what to put in the registry,
    instead of sending them to a terminal blind.
    """
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    mock_intuit.company_name = "Acme Main LLC"

    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)

    import anyio

    result = anyio.run(lambda: server.call_tool("get_company_info", {"entity": "acme_main"}))
    assert result.is_error is False
    payload = result.structured_content

    assert payload["company_name_matches_registry"] is False
    assert payload["company_info"]["company_name"] == "Acme Main LLC"
    assert payload["display_name_from_registry"] == "Acme Main"
    assert payload["company_name_mismatch_difference_class"] == "other"
    assert "entities.yaml" in payload["guard_note"]


def test_a_mismatched_entity_withholds_settings_and_capabilities(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """Preferences are not fetched for a mismatched entity, so there is nothing
    to report -- and every capability reported as false would be a lie dressed
    as an answer."""
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    mock_intuit.company_name = "Acme Main LLC"

    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)

    import anyio

    result = anyio.run(lambda: server.call_tool("get_company_info", {"entity": "acme_main"}))
    payload = result.structured_content
    assert payload["settings"] is None
    assert payload["capabilities"] is None
    assert not any("preferences" in r[1] for r in mock_intuit.requests)


def test_a_whitespace_only_mismatch_is_still_named(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """The case where the two names look identical on screen."""
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    mock_intuit.company_name = "Acme Main "

    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)

    import anyio

    result = anyio.run(lambda: server.call_tool("get_company_info", {"entity": "acme_main"}))
    payload = result.structured_content
    assert payload["company_name_mismatch_difference_class"] == "whitespace"
    assert payload["company_info"]["company_name"] == "Acme Main "


def test_every_other_tool_still_fails_closed_on_a_mismatch(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    mock_intuit.company_name = "Acme Main LLC"

    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)

    import anyio
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as caught:
        anyio.run(lambda: server.call_tool("list_accounts", {"entity": "acme_main"}))
    # Neither name is echoed to a model.
    assert "Acme" not in str(caught.value)


def test_the_session_still_raises_for_callers_that_do_not_opt_in(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    mock_intuit.company_name = "Elsewhere"

    session = QboSession(mock_settings, registry)
    with pytest.raises(GuardMismatch):
        session.ensure_ready(registry["acme_main"])


def test_a_matching_entity_is_unaffected(
    mock_settings, registry, credentials, actor
) -> None:
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)

    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)

    import anyio

    result = anyio.run(lambda: server.call_tool("get_company_info", {"entity": "acme_main"}))
    payload = result.structured_content
    assert payload["company_name_matches_registry"] is True
    assert payload["settings"]["home_currency"] == "USD"
    assert payload["capabilities"]["accounting_basis"] == "Accrual"
    assert payload["guard_note"] is None


# --------------------------------------------------------------------------
# The standard list envelope
# --------------------------------------------------------------------------


def test_list_accounts_uses_the_shared_list_vocabulary() -> None:
    """Eight list tools with eight shapes costs tokens on every call, so they
    all use these names."""
    from qbo_multicompany_mcp.envelopes import ListAccountsEnvelope, ListEnvelope

    assert issubclass(ListAccountsEnvelope, ListEnvelope)
    fields = set(ListAccountsEnvelope.model_fields)
    assert {"count", "rows", "truncated", "duplicates_dropped", "pages_walked"} <= fields
    assert "account_count" not in fields
    assert "accounts" not in fields
    assert "complete" not in fields


def test_truncated_says_rows_were_withheld() -> None:
    from qbo_multicompany_mcp.envelopes import ListEnvelope

    field = ListEnvelope.model_fields["truncated"]
    assert "withheld" in (field.description or "")


# --------------------------------------------------------------------------
# The suite never binds the real callback port
# --------------------------------------------------------------------------


def test_the_settings_fixture_never_uses_the_real_callback_port(settings) -> None:
    """Binding 8765 would stop this suite running while any consent is in
    flight on the machine."""
    assert settings.callback_port != CALLBACK_PORT
    assert settings.callback_port > 0


def test_no_test_binds_the_fixed_callback_port() -> None:
    """A source-level guard against the fixed port reappearing in tests.

    The needle is assembled at run time. Spelled as a literal, this scan finds
    itself and fails, the same trap as the private-key scan in
    test_repo_hygiene.py. A scan that can match its own source is not a scan.
    """
    import pathlib

    needle = "port=" + "CALLBACK_PORT"
    offenders = [
        path.name
        for path in pathlib.Path("tests").glob("test_*.py")
        if needle in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders


def test_that_port_guard_actually_fires(tmp_path) -> None:
    """Proves the scan above matches something, rather than passing forever."""
    import pathlib

    needle = "port=" + "CALLBACK_PORT"
    sample = tmp_path / "test_sample.py"
    sample.write_text(f"LoopbackReceiver(a, {needle})\n", encoding="utf-8")
    hits = [p.name for p in pathlib.Path(tmp_path).glob("test_*.py")
            if needle in p.read_text(encoding="utf-8")]
    assert hits == ["test_sample.py"]
