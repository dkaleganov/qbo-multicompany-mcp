"""Proves the SDK contract, against the pinned SDK, over a real stdio client.

Annotating a tool ``Annotated[CallToolResult, Envelope]`` makes the SDK derive
and publish an output schema, and validate the handler's ``structured_content``
against it. ``structured_output`` is a boolean, not a schema argument -- if it
were taking a schema, the annotation route would be unnecessary and these
assertions would be checking the wrong mechanism.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from qbo_multicompany_mcp.envelopes import (
    CompanyInfoEnvelope,
    ListAccountsEnvelope,
    ListEntitiesEnvelope,
    ServerInfoEnvelope,
)

from .conftest import REALM_MAIN, make_token_response, result_text, run_stdio, stdio_env


def test_structured_output_is_a_boolean_not_a_schema() -> None:
    """The pinned SDK is the authority. Assert it directly, so a future SDK
    that changes this fails here rather than silently."""
    from mcp.server import MCPServer

    parameter = inspect.signature(MCPServer.tool).parameters["structured_output"]
    assert parameter.annotation in ("bool | None", bool | None)


def test_every_tool_publishes_an_output_schema(entities_file: Path, store_dir: Path) -> None:
    async def body(client):
        return {t.name: t for t in (await client.list_tools()).tools}

    tools = run_stdio(stdio_env(entities_file=entities_file, store_dir=store_dir), [], body)

    expected = {
        "list_entities": ListEntitiesEnvelope,
        "server_info": ServerInfoEnvelope,
        "get_company_info": CompanyInfoEnvelope,
        "list_accounts": ListAccountsEnvelope,
    }
    for name, model in expected.items():
        schema = tools[name].output_schema
        assert schema is not None, name
        assert schema["title"] == model.__name__


def test_intuit_backed_tools_require_an_explicit_entity(
    entities_file: Path, store_dir: Path
) -> None:
    """No default company, ever -- enforced by the published input schema."""

    async def body(client):
        return {t.name: t.input_schema for t in (await client.list_tools()).tools}

    schemas = run_stdio(stdio_env(entities_file=entities_file, store_dir=store_dir), [], body)
    assert schemas["get_company_info"]["required"] == ["entity"]
    assert schemas["list_accounts"]["required"] == ["entity"]


def test_text_block_is_compact_json_and_matches_structured_content(
    entities_file: Path, store_dir: Path
) -> None:
    async def body(client):
        return await client.call_tool("list_entities", {})

    result = run_stdio(stdio_env(entities_file=entities_file, store_dir=store_dir), [], body)

    assert result.is_error is False
    text = result_text(result)
    # Compact: no spaces after the separators.
    assert ", " not in text
    assert '": ' not in text
    assert json.loads(text) == result.structured_content


def test_structured_content_validates_against_the_published_schema(
    entities_file: Path, store_dir: Path
) -> None:
    async def body(client):
        tool = {t.name: t for t in (await client.list_tools()).tools}["server_info"]
        result = await client.call_tool("server_info", {})
        return tool.output_schema, result

    schema, result = run_stdio(
        stdio_env(entities_file=entities_file, store_dir=store_dir), [], body
    )
    assert result.structured_content is not None
    # The envelope is the schema's source, so re-validating through it is the
    # same check the SDK performs server-side.
    ServerInfoEnvelope(**result.structured_content)
    assert set(schema["required"]) <= set(result.structured_content)


def test_a_successful_intuit_backed_result_carries_the_provenance_fields(
    entities_file: Path, store_dir: Path, token_key: str, settings, credentials, actor,
    mock_intuit, mock_settings,
) -> None:
    store = mock_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )

    env = stdio_env(
        entities_file=entities_file,
        store_dir=store_dir,
        token_key=token_key,
        extra={"QBO_CLIENT_ID": credentials.client_id, "QBO_CLIENT_SECRET": credentials.client_secret},
    )

    async def body(client):
        return await client.call_tool("get_company_info", {"entity": "acme_main"})

    result = run_stdio(env, ["--api-base", mock_intuit.base_url], body)
    assert result.is_error is False, result_text(result)

    payload = result.structured_content
    assert payload["entity"] == "acme_main"
    assert payload["environment"] == "sandbox"
    assert payload["display_name_from_registry"] == "Acme Main"
    assert payload["realm_id_last4"] == "0001"
    assert payload["request_started_at"]
    assert payload["fetched_at"]
    assert payload["company_name_matches_registry"] is True
    assert payload["guard_checked_at"]
    assert payload["capabilities"]["accounting_basis"] == "Accrual"
    assert payload["settings"]["home_currency"] == "USD"
    # The full realm id never appears in a result.
    assert REALM_MAIN not in json.dumps(payload)


def test_list_accounts_returns_the_walked_list_envelope(
    entities_file: Path, store_dir: Path, token_key: str, credentials, actor,
    mock_intuit, mock_settings,
) -> None:
    """The envelope, over a real stdio server.

    Not a paging test: five rows go to a client asking for a thousand, so they
    are one page. The paging behaviour is covered where it can actually be
    driven, in ``tests/test_pagination_and_limits.py``; what this one is good
    for is the shape of the result at the far end of a subprocess.
    """
    store = mock_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )
    mock_intuit.accounts = [
        {"Id": str(i), "Name": f"Account {i}", "AccountType": "Expense", "CurrentBalance": float(i)}
        for i in range(1, 6)
    ]

    env = stdio_env(
        entities_file=entities_file,
        store_dir=store_dir,
        token_key=token_key,
        extra={"QBO_CLIENT_ID": credentials.client_id, "QBO_CLIENT_SECRET": credentials.client_secret},
    )

    async def body(client):
        return await client.call_tool("list_accounts", {"entity": "acme_main"})

    result = run_stdio(env, ["--api-base", mock_intuit.base_url], body)

    assert result.is_error is False, result_text(result)
    payload = result.structured_content
    # The standard list envelope: count / rows / truncated / duplicates_dropped.
    assert payload["count"] == 5
    assert len(payload["rows"]) == 5
    assert payload["duplicates_dropped"] == 0
    assert "account_count" not in payload and "accounts" not in payload
    assert payload["truncated"] is False
    # One page of five rows really was one page, and it says so.
    assert payload["pages_walked"] == 1
    assert payload["consistency"] == "non_snapshot"
    assert payload["walk_started_at"] <= payload["walk_finished_at"]
    assert ListAccountsEnvelope(**payload).count == 5


def test_an_adapter_raised_error_still_reaches_the_client_as_readable_text(
    entities_file: Path, store_dir: Path
) -> None:
    """A tool whose entity does not exist must produce text a model can act on,
    not the SDK's opaque 'Error executing tool <name>'."""

    async def body(client):
        return await client.call_tool("get_company_info", {"entity": "no_such_entity"})

    result = run_stdio(stdio_env(entities_file=entities_file, store_dir=store_dir), [], body)
    assert result.is_error is True
    text = result_text(result)
    assert "no default company" in text
    assert "acme_main" in text
    assert text != "Error executing tool get_company_info"


def test_invalid_structured_content_is_caught_by_the_sdk(tmp_path: Path) -> None:
    """Documents the failure mode extra='forbid' buys: a projection that emits
    an undeclared field fails loudly instead of shipping it."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ListEntitiesEnvelope(
            environment="sandbox",
            entity_count=0,
            entities=[],
            token_store_available=True,
            fetched_at="2026-01-01T00:00:00Z",
            surprise="x",
        )
