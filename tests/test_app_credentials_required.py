"""No app credentials, no credential use.

``_assert_configuration`` compares a row's client fingerprint with the running
app only when the app is configured. With no ``QBO_CLIENT_ID`` it compares
nothing, so on its own it would let a still-valid cached token from a row
written by *any* app be sent. The refusal goes where the risk is -- the path
that sends -- and not on every row read: ``tools/list``, ``list_entities``,
``server_info`` and the inspection reads must keep working without secrets,
because a registry can start a server with none to list its tools.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from qbo_multicompany_mcp.client import AccountingClient
from qbo_multicompany_mcp.errors import ConfigurationError
from qbo_multicompany_mcp.tools import register_tools

from .conftest import REALM_MAIN, make_token_response, stdio_env


def _seed(settings, credentials, actor) -> None:
    settings.open_store(credentials=credentials).commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )


def _no_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QBO_CLIENT_ID", raising=False)
    monkeypatch.delenv("QBO_CLIENT_SECRET", raising=False)


def test_a_stored_credential_is_not_released_for_use_without_an_app(
    settings, credentials, actor, registry
) -> None:
    _seed(settings, credentials, actor)
    appless = settings.open_store(credentials=None)
    with pytest.raises(ConfigurationError, match="QBO_CLIENT_ID"):
        appless.usable_row(REALM_MAIN, entity_key="acme_main")


def test_nothing_reaches_the_transport_without_an_app(
    settings, credentials, actor, registry
) -> None:
    """The row holds a still-valid access token, so no refresh is due and
    nothing else would have stopped it being sent."""
    _seed(settings, credentials, actor)
    reached: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        return httpx.Response(200, json={}, request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with AccountingClient(settings, settings.open_store(credentials=None), http=http) as client:
        with pytest.raises(ConfigurationError, match="QBO_CLIENT_ID"):
            client.get(registry["acme_main"], "preferences")
    assert reached == []


def test_a_data_tool_says_what_is_missing_without_an_app(
    mock_settings, mock_intuit, credentials, actor, registry, monkeypatch
) -> None:
    _seed(mock_settings, credentials, actor)
    _no_app(monkeypatch)
    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)

    with pytest.raises(ToolError) as caught:
        anyio.run(lambda: server.call_tool("list_accounts", {"entity": "acme_main"}))
    text = str(caught.value)
    assert "QBO_CLIENT_ID" in text and "QBO_CLIENT_SECRET" in text
    assert mock_intuit.requests == [], "a credential was sent with no app configured"


def test_inspection_still_reads_rows_without_an_app(settings, credentials, actor) -> None:
    """get_row serves status, keepalive reporting and diagnostics. It sends
    nothing, so it is not where the refusal belongs."""
    _seed(settings, credentials, actor)
    row = settings.open_store(credentials=None).get_row(REALM_MAIN)
    assert row is not None and row.entity == "acme_main"


@pytest.mark.parametrize("tool", ["list_entities", "server_info"])
def test_request_free_diagnostics_still_work_without_an_app(
    mock_settings, mock_intuit, credentials, actor, registry, monkeypatch, tool: str
) -> None:
    _seed(mock_settings, credentials, actor)
    _no_app(monkeypatch)
    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)

    result = anyio.run(lambda: server.call_tool(tool, {}))
    assert result.is_error is False
    assert result.structured_content["token_store_available"] is True
    assert mock_intuit.requests == []


def test_server_info_says_whether_credential_use_is_enabled(
    mock_settings, mock_intuit, credentials, actor, registry, monkeypatch
) -> None:
    """The refusal shows on stderr and in each refused call, but a model
    calling server_info first should learn of it before its first data call,
    not from its first failure."""
    _seed(mock_settings, credentials, actor)
    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    with_app = anyio.run(lambda: server.call_tool("server_info", {})).structured_content
    assert with_app["credential_use_enabled"] is True
    assert with_app["credential_use_note"] is None

    _no_app(monkeypatch)
    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    without = anyio.run(lambda: server.call_tool("server_info", {})).structured_content
    assert without["credential_use_enabled"] is False
    assert "QBO_CLIENT_ID" in without["credential_use_note"]
    assert mock_intuit.requests == []


def test_startup_without_an_app_warns_that_credential_use_is_disabled(
    entities_file: Path, store_dir: Path
) -> None:
    """A real process, stdin closed: it starts, warns once on stderr, and exits
    when the (empty) session ends. stdout stays the protocol's alone."""
    env = stdio_env(entities_file=entities_file, store_dir=store_dir)
    completed = subprocess.run(
        [sys.executable, "-m", "qbo_multicompany_mcp.server"],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    assert "credential use is disabled" in completed.stderr
    assert "QBO_CLIENT_ID" in completed.stderr
    assert completed.stdout == ""
    assert not store_dir.exists(), "a warning is not a reason to create a store"


def test_startup_with_an_app_does_not_warn(
    entities_file: Path, store_dir: Path, credentials
) -> None:
    env = stdio_env(
        entities_file=entities_file, store_dir=store_dir,
        extra={"QBO_CLIENT_ID": credentials.client_id,
               "QBO_CLIENT_SECRET": credentials.client_secret},
    )
    completed = subprocess.run(
        [sys.executable, "-m", "qbo_multicompany_mcp.server"],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    assert "credential use is disabled" not in completed.stderr
