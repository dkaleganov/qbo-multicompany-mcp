"""Proves three promises that are easy to state and easy to break silently.

* Enumerating tools touches no credential and leaves no trace on disk.
* The canonical lock is never held across human interaction.
* Diagnostics go to stderr only, because stdout carries the stdio transport.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

from qbo_multicompany_mcp import auth_cli
from qbo_multicompany_mcp.errors import StoreLockTimeout
from qbo_multicompany_mcp.store import canonical_lock

from .conftest import REALM_MAIN, make_token_response, run_stdio, stdio_env


# --------------------------------------------------------------------------
# Lazy store
# --------------------------------------------------------------------------


def test_tools_list_creates_no_store_and_no_directory(
    entities_file: Path, tmp_path: Path
) -> None:
    """A client merely enumerating tools has not asked this server to touch a
    credential, and enumeration should leave no trace."""
    store_dir = tmp_path / "never-created"

    async def body(client):
        return [tool.name for tool in (await client.list_tools()).tools]

    names = run_stdio(stdio_env(entities_file=entities_file, store_dir=store_dir), [], body)

    # The eighteen default tools. get_attachment is the one opt-in and is
    # absent without --allow-documents.
    assert sorted(names) == [
        "get_company_info", "get_transaction", "list_accounts", "list_attachments",
        "list_changes", "list_customers", "list_entities", "list_reference_data",
        "list_transactions", "list_vendors", "report_aging", "report_balance_sheet",
        "report_general_ledger", "report_profit_and_loss", "run_query", "run_report",
        "server_info", "summarize_vendor_payments",
    ]
    # The invariant that matters: enumerating tools touches no credential and
    # leaves nothing on disk -- including the rate limiter's state file, which
    # is constructed with the client but only written inside a request.
    assert not store_dir.exists()


def test_tools_list_answers_with_only_the_entities_variable_set(
    entities_file: Path, tmp_path: Path
) -> None:
    """No client id, no client secret, no token key, no store."""
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "QBO_ENTITIES_FILE": str(entities_file),
        "QBO_TOKEN_STORE": str(tmp_path / "state"),
    }

    async def body(client):
        return (await client.list_tools()).tools

    tools = run_stdio(env, [], body)
    assert len(tools) == 18, "the eighteen default tools; the opt-in is absent"
    assert not (tmp_path / "state").exists()


def test_constructing_the_store_touches_no_disk(settings, credentials) -> None:
    store = settings.open_store(credentials=credentials)
    assert not store.directory.exists()
    assert not store.lock_path.exists()
    assert not store.journal_path.exists()


# --------------------------------------------------------------------------
# The lock and human interaction
# --------------------------------------------------------------------------


def test_the_lock_is_not_held_while_waiting_for_a_human(
    settings, registry, credentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consent screen can sit open for minutes. A lock held that long looks
    exactly like a deadlock to every other process."""
    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)

    lock_free_during_consent: dict[str, bool] = {}
    captured: dict[str, str] = {}

    real_authorize_url = auth_cli.authorize_url

    def spy_url(*, client_id: str, state: str, redirect_uri: str) -> str:
        captured["state"] = state
        return real_authorize_url(
            client_id=client_id, state=state, redirect_uri=redirect_uri
        )

    def fake_open(url: str) -> bool:
        def probe_then_deliver() -> None:
            # This is the moment a human is at the consent screen.
            store = settings.open_store(credentials=credentials)
            try:
                with canonical_lock(store.lock_path, timeout=1):
                    lock_free_during_consent["free"] = True
            except StoreLockTimeout:
                lock_free_during_consent["free"] = False
            httpx.get(
                f"http://127.0.0.1:{settings.callback_port}/callback",
                params={
                    "code": "thecode",
                    "state": captured["state"],
                    "realmId": REALM_MAIN,
                },
                timeout=5,
            )

        threading.Thread(target=probe_then_deliver, daemon=True).start()
        return True

    monkeypatch.setattr(auth_cli, "authorize_url", spy_url)
    monkeypatch.setattr(auth_cli.webbrowser, "open", fake_open)
    monkeypatch.setattr(
        auth_cli.oauth,
        "exchange_code",
        lambda **kw: make_token_response(realm_id=REALM_MAIN),
    )
    # The consent guard reads CompanyName before storing; this test is about
    # lock discipline, so give it a name that matches.
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main")

    import argparse

    code = auth_cli.cmd_connect(
        settings, registry, argparse.Namespace(entity="acme_main", manual=False)
    )

    assert code == 0
    assert lock_free_during_consent.get("free") is True


def test_the_consent_commit_does_take_the_lock(
    settings, credentials, actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qbo_multicompany_mcp import store as store_module

    taken: list[str] = []
    real_lock = store_module.canonical_lock

    def counting_lock(path, timeout=None):
        taken.append(path.name)
        return real_lock(path, timeout) if timeout is not None else real_lock(path)

    monkeypatch.setattr(store_module, "canonical_lock", counting_lock)
    settings.open_store(credentials=credentials).commit_consent(
        entity="acme_main",
        realm_id=REALM_MAIN,
        response=make_token_response(),
        actor=actor,
    )
    assert taken == ["tokens.lock"]


# --------------------------------------------------------------------------
# stdout discipline
# --------------------------------------------------------------------------


def test_verbose_diagnostics_go_to_stderr_and_never_to_stdout(
    entities_file: Path, tmp_path: Path
) -> None:
    """A stray byte on stdout corrupts the stdio session."""
    import json
    import os

    from .conftest import with_source_root

    env = with_source_root(
        {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "QBO_ENTITIES_FILE": str(entities_file),
            "QBO_TOKEN_STORE": str(tmp_path / "state"),
        }
    )
    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            }
        )
        + "\n"
    )

    result = subprocess.run(
        [sys.executable, "-m", "qbo_multicompany_mcp.server", "--verbose"],
        input=request,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    # Every stdout line is a JSON-RPC message and nothing else, and the one
    # there is the answer: an empty stdout passes the first check on its own.
    assert _answers_initialize(_stdout_messages(result.stdout)), result.stdout
    # The verbose diagnostics ran, and went to stderr.
    assert any(line.startswith("DEBUG ") for line in result.stderr.splitlines()), result.stderr


def test_the_stdout_check_fails_a_server_that_never_answered() -> None:
    """A server that exits before answering leaves stdout empty, and a check
    that reads only the lines present would pass it. A stray line still fails."""
    assert not _answers_initialize(_stdout_messages(""))
    with pytest.raises(ValueError):
        _stdout_messages("starting up\n")


def _stdout_messages(stdout: str) -> list[dict]:
    """Every stdout line as a JSON-RPC message; a line that is not one fails."""
    import json

    messages = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0", line
        messages.append(message)
    return messages


def _answers_initialize(messages: list[dict]) -> bool:
    """The only message is the answer to the initialize request, id 1."""
    return [m.get("id") for m in messages] == [1] and "serverInfo" in messages[0].get("result", {})
