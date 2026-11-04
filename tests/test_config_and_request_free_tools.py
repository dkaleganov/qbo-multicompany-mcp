"""Proves the flag and environment contract, and that the two tools documented
as making no request in fact make none."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from qbo_multicompany_mcp import __version__
from qbo_multicompany_mcp.config import add_common_arguments, settings_from_args
from qbo_multicompany_mcp.constants import API_BASES, STALE_REFRESH_SECONDS
from qbo_multicompany_mcp.errors import StartupError

from .conftest import REALM_MAIN, REALM_OPS, make_token_response, run_stdio, stdio_env


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_common_arguments(parser)
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# Flags and environment
# --------------------------------------------------------------------------


def test_env_file_takes_an_explicit_path_and_does_not_search(
    tmp_path: Path, entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """python-dotenv's default walks up from the working directory, which would
    make a server's credentials depend on where it was started from."""
    nearby = tmp_path / ".env"
    nearby.write_text("QBO_CLIENT_ID=from-implicit-search\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QBO_CLIENT_ID", raising=False)
    monkeypatch.delenv("QBO_CLIENT_SECRET", raising=False)

    settings = settings_from_args(parse(["--entities", str(entities_file)]))
    assert settings.credentials() is None  # the neighbouring .env was not read


def test_env_file_loads_the_named_file(
    tmp_path: Path, entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = tmp_path / "creds.env"
    named.write_text(
        "QBO_CLIENT_ID=explicit-id\nQBO_CLIENT_SECRET=explicit-secret\n", encoding="utf-8"
    )
    monkeypatch.delenv("QBO_CLIENT_ID", raising=False)
    monkeypatch.delenv("QBO_CLIENT_SECRET", raising=False)

    settings = settings_from_args(
        parse(["--entities", str(entities_file), "--env-file", str(named)])
    )
    credentials = settings.credentials()
    assert credentials is not None and credentials.client_id == "explicit-id"


def test_an_exported_variable_beats_the_env_file(
    tmp_path: Path, entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = tmp_path / "creds.env"
    named.write_text("QBO_CLIENT_ID=from-file\nQBO_CLIENT_SECRET=s\n", encoding="utf-8")
    monkeypatch.setenv("QBO_CLIENT_ID", "from-shell")

    settings = settings_from_args(
        parse(["--entities", str(entities_file), "--env-file", str(named)])
    )
    credentials = settings.credentials()
    assert credentials is not None and credentials.client_id == "from-shell"


def test_a_missing_env_file_exits_2(entities_file: Path, tmp_path: Path) -> None:
    with pytest.raises(StartupError, match="does not exist"):
        settings_from_args(
            parse(["--entities", str(entities_file), "--env-file", str(tmp_path / "nope.env")])
        )


def test_environment_defaults_to_sandbox(
    entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QBO_ENV", raising=False)
    settings = settings_from_args(parse(["--entities", str(entities_file)]))
    assert settings.environment == "sandbox"
    assert settings.api_base == API_BASES["sandbox"]


def test_production_selects_the_production_origin(
    entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QBO_ENV", "production")
    settings = settings_from_args(parse(["--entities", str(entities_file)]))
    assert settings.api_base == API_BASES["production"]


def test_an_unknown_environment_exits_2(
    entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QBO_ENV", "staging")
    with pytest.raises(StartupError, match="QBO_ENV"):
        settings_from_args(parse(["--entities", str(entities_file)]))


def test_allow_documents_from_the_flag_or_the_variable(
    entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QBO_ALLOW_DOCUMENTS", raising=False)
    assert settings_from_args(parse(["--entities", str(entities_file)])).allow_documents is False
    assert (
        settings_from_args(
            parse(["--entities", str(entities_file), "--allow-documents"])
        ).allow_documents
        is True
    )
    monkeypatch.setenv("QBO_ALLOW_DOCUMENTS", "1")
    assert settings_from_args(parse(["--entities", str(entities_file)])).allow_documents is True


def test_entities_file_may_come_from_the_variable(
    entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QBO_ENTITIES_FILE", str(entities_file))
    assert settings_from_args(parse([])).entities_file == entities_file


def test_no_entities_file_at_all_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QBO_ENTITIES_FILE", raising=False)
    with pytest.raises(StartupError, match="QBO_ENTITIES_FILE"):
        settings_from_args(parse([]))


@pytest.mark.parametrize(
    "module",
    [
        "qbo_multicompany_mcp.server",
        "qbo_multicompany_mcp.auth_cli",
        "qbo_multicompany_mcp.keepalive",
    ],
)
def test_every_entry_point_reports_its_version(module: str) -> None:
    import os

    from .conftest import with_source_root

    result = subprocess.run(
        [sys.executable, "-m", module, "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=with_source_root(dict(os.environ)),
    )
    assert result.returncode == 0
    assert __version__ in (result.stdout + result.stderr)


def test_a_store_row_from_the_wrong_environment_exits_2_from_a_cli(
    settings, credentials, actor, registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration, not consent -- and a CLI is a startup context, so it is
    exit 2 rather than a tool error."""
    from qbo_multicompany_mcp import keepalive

    store = settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )
    with store.lock():
        document = store._load_locked()
        document.rows[REALM_MAIN] = replace(document.rows[REALM_MAIN], environment="production")
        store._commit_locked(document)

    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)

    assert keepalive.run(settings, registry, argparse.Namespace(force=True)) == 2
    row = store.get_row(REALM_MAIN)
    assert row is not None and row.needs_consent is False


# --------------------------------------------------------------------------
# The two request-free tools
# --------------------------------------------------------------------------


def seed(store, actor, entity: str, realm: str, **row_overrides):
    store.commit_consent(
        entity=entity, realm_id=realm, response=make_token_response(), actor=actor
    )
    if row_overrides:
        with store.lock():
            document = store._load_locked()
            document.rows[realm] = replace(document.rows[realm], **row_overrides)
            store._commit_locked(document)


def test_list_entities_reads_the_store_and_makes_no_request(
    mock_settings, mock_intuit, credentials, actor, registry
) -> None:
    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)

    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)

    import anyio

    result = anyio.run(lambda: server.call_tool("list_entities", {}))
    assert result.is_error is False
    assert mock_intuit.requests == []


def test_server_info_makes_no_request(mock_settings, mock_intuit, credentials, actor, registry) -> None:
    """server_info reads the local store and makes no request, which is what
    makes ``openWorldHint: false`` the precise value. list_entities has the
    same test above."""
    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)

    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)

    import anyio

    result = anyio.run(lambda: server.call_tool("server_info", {}))
    assert result.is_error is False
    assert mock_intuit.requests == []


def test_list_entities_reports_state_freshness_and_cached_capabilities(
    entities_file: Path, store_dir: Path, token_key: str, settings, credentials, actor
) -> None:
    store = settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    seed(
        store,
        actor,
        "acme_ops",
        REALM_OPS,
        last_refresh_at=datetime.now(UTC) - timedelta(seconds=STALE_REFRESH_SECONDS + 60),
        capabilities={
            "class_tracking": True,
            "department_tracking": False,
            "inventory": False,
            "multi_currency": True,
            "accounting_basis": "Cash",
        },
    )

    async def body(client):
        return await client.call_tool("list_entities", {})

    result = run_stdio(
        stdio_env(entities_file=entities_file, store_dir=store_dir, token_key=token_key), [], body
    )
    assert result.is_error is False
    entities = {e["entity"]: e for e in result.structured_content["entities"]}

    assert entities["acme_main"]["auth_state"] == "authorized"
    assert entities["acme_main"]["stale_refresh"] is False
    assert entities["acme_main"]["realm_id_last4"] == "0001"
    assert entities["acme_main"]["display_name_from_registry"] == "Acme Main"
    assert entities["acme_main"]["days_to_refresh_token_expiry"] > 99

    # Per realm: acme_main being healthy does not mask acme_ops's age.
    assert entities["acme_ops"]["stale_refresh"] is True
    assert entities["acme_ops"]["capabilities"]["accounting_basis"] == "Cash"
    assert entities["acme_ops"]["capabilities"]["class_tracking"] is True


def test_list_entities_reports_never_authorized_without_a_store_row(
    entities_file: Path, store_dir: Path, token_key: str
) -> None:
    async def body(client):
        return await client.call_tool("list_entities", {})

    result = run_stdio(
        stdio_env(entities_file=entities_file, store_dir=store_dir, token_key=token_key), [], body
    )
    states = {e["entity"]: e["auth_state"] for e in result.structured_content["entities"]}
    assert states == {"acme_main": "never_authorized", "acme_ops": "never_authorized"}


def test_server_info_reports_the_read_only_contract(
    entities_file: Path, store_dir: Path, token_key: str, settings, credentials, actor
) -> None:
    store = settings.open_store(credentials=credentials)
    seed(
        store,
        actor,
        "acme_ops",
        REALM_OPS,
        last_refresh_at=datetime.now(UTC) - timedelta(seconds=STALE_REFRESH_SECONDS + 60),
    )

    async def body(client):
        return await client.call_tool("server_info", {})

    result = run_stdio(
        stdio_env(entities_file=entities_file, store_dir=store_dir, token_key=token_key), [], body
    )
    payload = result.structured_content

    assert payload["read_only"] is True
    assert payload["writes_enabled"] is False
    assert payload["documents_enabled"] is False
    assert payload["environment"] == "sandbox"
    assert payload["entity_count"] == 2
    assert payload["authorized_entity_count"] == 1
    assert "aes-256-gcm" in payload["token_store_protection"]
    assert payload["any_stale"] is True
    assert payload["stale_entities"] == ["acme_ops"]
    assert payload["version"] == __version__


def test_server_info_reports_documents_enabled_when_the_flag_is_set(
    entities_file: Path, store_dir: Path, token_key: str
) -> None:
    async def body(client):
        return await client.call_tool("server_info", {})

    result = run_stdio(
        stdio_env(entities_file=entities_file, store_dir=store_dir, token_key=token_key),
        ["--allow-documents"],
        body,
    )
    assert result.structured_content["documents_enabled"] is True


# --------------------------------------------------------------------------
# QBO_REDIRECT_URI
# --------------------------------------------------------------------------


def test_redirect_uri_defaults_to_the_localhost_value(
    entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qbo_multicompany_mcp.constants import DEFAULT_REDIRECT_URI

    monkeypatch.delenv("QBO_REDIRECT_URI", raising=False)
    settings = settings_from_args(parse(["--entities", str(entities_file)]))
    assert settings.redirect_uri == DEFAULT_REDIRECT_URI
    assert "127.0.0.1" not in settings.redirect_uri


def test_redirect_uri_comes_from_the_environment(
    entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QBO_REDIRECT_URI", "https://oauth.example.com/callback")
    settings = settings_from_args(parse(["--entities", str(entities_file)]))
    assert settings.redirect_uri == "https://oauth.example.com/callback"


def test_the_flag_beats_the_environment(
    entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QBO_REDIRECT_URI", "https://from-env.example.com/callback")
    settings = settings_from_args(
        parse([
            "--entities", str(entities_file),
            "--redirect-uri", "https://from-flag.example.com/callback",
        ])
    )
    assert settings.redirect_uri == "https://from-flag.example.com/callback"


def test_an_unregistrable_redirect_uri_exits_2(
    entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caught at startup rather than at the consent screen, where the failure
    is an opaque "The redirect_uri query parameter value is invalid"."""
    monkeypatch.setenv("QBO_REDIRECT_URI", "http://127.0.0.1:8765/callback")
    with pytest.raises(StartupError):
        settings_from_args(parse(["--entities", str(entities_file)]))


def test_an_env_file_does_not_leak_into_the_rest_of_the_session(
    tmp_path: Path, entities_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load_dotenv` writes into os.environ and never takes it back.

    Two tests above point `--env-file` at a fixture file, which would leave
    `QBO_CLIENT_ID=explicit-id` set for every test that runs afterwards in the
    same process. The autouse fixture in conftest restores the environment;
    this is what would fail if it were removed.
    """
    import os
    import subprocess
    import sys

    probe = tmp_path / "test_leak_probe.py"
    probe.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from qbo_multicompany_mcp.config import settings_from_args\n"
        "from tests.test_config_and_request_free_tools import parse\n"
        "\n"
        "def test_a_reads_an_env_file(tmp_path):\n"
        "    named = tmp_path / 'creds.env'\n"
        "    named.write_text('QBO_CLIENT_ID=leaked-id\\nQBO_CLIENT_SECRET=s\\n')\n"
        "    settings_from_args(parse(['--entities', %r, '--env-file', str(named)]))\n"
        "\n"
        "def test_b_starts_clean():\n"
        "    assert os.environ.get('QBO_CLIENT_ID') != 'leaked-id'\n"
        "\n"
        "def test_c_imports_the_tree_under_test():\n"
        "    import qbo_multicompany_mcp.config as config\n"
        "    expected = Path(os.environ['EXPECTED_CONFIG']).resolve()\n"
        "    assert Path(config.__file__).resolve() == expected, config.__file__\n"
        % str(entities_file),
        encoding="utf-8",
    )
    from qbo_multicompany_mcp import config as config_module

    from .conftest import with_source_root

    result = subprocess.run(
        # `-p tests.conftest` loads the shared fixtures as a plugin, so the
        # autouse restore applies to a probe living outside the tests package.
        [
            sys.executable, "-m", "pytest", "-q",
            "-p", "no:cacheprovider", "-p", "tests.conftest", str(probe),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        # The repository root is for the test plugin; the active source root
        # comes first, so the probe imports the tree under test and not the
        # installed package. The probe asserts it.
        env=with_source_root(
            {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "EXPECTED_CONFIG": str(config_module.__file__),
            }
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout, result.stdout
