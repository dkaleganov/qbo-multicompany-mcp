"""Proves: a registry that is ambiguous, malformed or contradictory stops the
process with exit 2 and one line, rather than starting and serving the wrong
company file."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from qbo_multicompany_mcp.errors import StartupError
from qbo_multicompany_mcp.registry import load_registry

from .conftest import REALM_MAIN, REALM_OPS


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "entities.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_valid_registry_loads_both_entities(registry) -> None:
    assert registry.keys() == ["acme_main", "acme_ops"]
    assert registry["acme_main"].realm_id == REALM_MAIN
    assert registry["acme_main"].realm_id_last4 == "0001"


def test_duplicate_realm_is_refused(tmp_path: Path) -> None:
    """Two keys on one realm makes every echoed entity meaningless."""
    path = write(
        tmp_path,
        f"""
entities:
  acme_main:
    display_name: Acme Main
    realm_id: "{REALM_MAIN}"
    environment: sandbox
  acme_ops:
    display_name: Acme Ops
    realm_id: "{REALM_MAIN}"
    environment: sandbox
""",
    )
    with pytest.raises(StartupError, match="share the same realm_id"):
        load_registry(path)


def test_shipped_example_has_distinct_realms_and_starts() -> None:
    """The shipped example must be startable as-is.

    One shared placeholder realm across both entities would make the file this
    project tells people to copy fail on first run.
    """
    registry = load_registry("entities.example.yaml", expected_environment="sandbox")
    realms = [entity.realm_id for entity in registry]
    assert len(realms) == len(set(realms))


def test_non_numeric_realm_is_refused(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
entities:
  acme_main:
    display_name: Acme Main
    realm_id: "not-a-number"
    environment: sandbox
""",
    )
    with pytest.raises(StartupError, match="non-numeric realm_id"):
        load_registry(path)


def test_malformed_yaml_is_refused_without_echoing_content(tmp_path: Path) -> None:
    path = write(tmp_path, "entities: [unclosed\n")
    with pytest.raises(StartupError) as caught:
        load_registry(path)
    assert "not valid YAML" in str(caught.value)
    assert "unclosed" not in str(caught.value)


def test_unreadable_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StartupError, match="cannot read entities file"):
        load_registry(tmp_path / "absent.yaml")


@pytest.mark.parametrize("value", ["true", "yes", "1", "'false'"])
def test_writes_must_be_literal_false(tmp_path: Path, value: str) -> None:
    """Anything but `false` means the author expected a capability that does
    not exist, and reading it as "no" would be the worst possible answer."""
    path = write(
        tmp_path,
        f"""
entities:
  acme_main:
    display_name: Acme Main
    realm_id: "{REALM_MAIN}"
    environment: sandbox
    writes: {value}
""",
    )
    with pytest.raises(StartupError, match="read-only and 'writes' may only be false"):
        load_registry(path)


def test_a_refused_writes_value_is_not_repeated(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        f"""
entities:
  acme_main:
    display_name: Acme Main
    realm_id: "{REALM_MAIN}"
    environment: sandbox
    writes: marked-value
""",
    )
    with pytest.raises(StartupError) as refused:
        load_registry(path)
    assert "marked-value" not in str(refused.value)


@pytest.mark.parametrize("key", ["acme_12345678", "12345678_acme", "a12345678901z", "a" * 41])
def test_a_key_the_redactor_would_cut_is_refused_at_load(tmp_path: Path, key: str) -> None:
    """Errors and logs mask a long run. A key holding one couldn't be read or
    pasted from an error, and two of them would look the same."""
    path = write(
        tmp_path,
        f"""
entities:
  {key}:
    display_name: Acme Main
    realm_id: "{REALM_MAIN}"
    environment: sandbox
""",
    )
    with pytest.raises(StartupError, match="errors and logs would mask") as refused:
        load_registry(path)
    assert key not in str(refused.value)


def test_a_key_with_seven_digits_in_a_row_is_accepted(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        f"""
entities:
  acme_1234567:
    display_name: Acme Main
    realm_id: "{REALM_MAIN}"
    environment: sandbox
""",
    )
    assert len(load_registry(path)) == 1


def test_writes_false_is_accepted(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        f"""
entities:
  acme_main:
    display_name: Acme Main
    realm_id: "{REALM_MAIN}"
    environment: sandbox
    writes: false
""",
    )
    assert len(load_registry(path)) == 1


def test_environment_must_match_qbo_env(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        f"""
entities:
  acme_main:
    display_name: Acme Main
    realm_id: "{REALM_OPS}"
    environment: production
""",
    )
    with pytest.raises(StartupError, match="QBO_ENV"):
        load_registry(path, expected_environment="sandbox")


def test_unknown_entity_field_is_refused(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        f"""
entities:
  acme_main:
    display_name: Acme Main
    realm_id: "{REALM_MAIN}"
    environment: sandbox
    reads: true
""",
    )
    with pytest.raises(StartupError, match="unknown field"):
        load_registry(path)


def test_invalid_entity_key_is_refused(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        f"""
entities:
  Acme-Main:
    display_name: Acme Main
    realm_id: "{REALM_MAIN}"
    environment: sandbox
""",
    )
    with pytest.raises(StartupError, match=r"\[a-z0-9_\]"):
        load_registry(path)


# --------------------------------------------------------------------------
# The process-level contract
# --------------------------------------------------------------------------


def run_server(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "qbo_multicompany_mcp.server", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def base_env() -> dict[str, str]:
    import os

    from .conftest import with_source_root

    return with_source_root(
        {k: v for k, v in os.environ.items() if k.startswith(("PATH", "HOME", "PYTHON"))}
    )


def test_startup_failure_is_one_line_exit_2_no_traceback(tmp_path: Path) -> None:
    env = base_env() | {"QBO_ENTITIES_FILE": str(tmp_path / "missing.yaml")}
    result = run_server([], env)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert len([line for line in result.stderr.splitlines() if line.strip()]) == 1


def test_disallowed_api_base_exits_2(entities_file: Path) -> None:
    env = base_env() | {"QBO_ENTITIES_FILE": str(entities_file)}
    result = run_server(["--api-base", "https://not-intuit.example.com"], env)
    assert result.returncode == 2
    assert "--allow-custom-api-base" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(("environment", "other"), [("production", "sandbox"), ("sandbox", "production")])
def test_the_other_environments_intuit_host_exits_2(
    entities_file: Path, environment: str, other: str
) -> None:
    """Through a real process: production refuses the sandbox API base at
    startup, and sandbox the production one, as one line and exit 2 -- before
    the registry, the store or any credential is read."""
    from qbo_multicompany_mcp.constants import API_BASES

    env = base_env() | {"QBO_ENTITIES_FILE": str(entities_file), "QBO_ENV": environment}
    result = run_server(["--api-base", API_BASES[other]], env)
    assert result.returncode == 2
    assert "other environment" in result.stderr
    assert "Traceback" not in result.stderr
    assert len([line for line in result.stderr.splitlines() if line.strip()]) == 1


def test_bad_key_source_exits_2(entities_file: Path) -> None:
    env = base_env() | {
        "QBO_ENTITIES_FILE": str(entities_file),
        "QBO_TOKEN_KEY_SOURCE": "magic",
    }
    result = run_server([], env)
    assert result.returncode == 2
    assert "QBO_TOKEN_KEY_SOURCE" in result.stderr


# --------------------------------------------------------------------------
# A secret in a startup error, before credentials are built
# --------------------------------------------------------------------------

#: Punctuated, so no shape rule of the redactor knows it, and long enough for
#: its exact-value scan: only the live-value registry can catch it. Assembled
#: at run time, so the tree's own secret scan never sees it spelled out.
PASTED = "-".join(["kq7", "Zt2.w9", "Rr4.mm8", "Vx"])

#: Each entry point as a module, and the subcommand it needs to reach the registry.
ENTRY_POINTS = {
    "server": ("qbo_multicompany_mcp.server", []),
    "auth": ("qbo_multicompany_mcp.auth_cli", ["status"]),
    "keepalive": ("qbo_multicompany_mcp.keepalive", []),
}


def secret_as_a_field(tmp_path: Path) -> Path:
    """A registry with the secret pasted in as a field name, which the
    registry's refusal repeats."""
    return write(
        tmp_path,
        f"""
entities:
  acme_main:
    display_name: Acme Main
    realm_id: "{REALM_MAIN}"
    environment: sandbox
    {PASTED}: pasted here by mistake
""",
    )


def run_entry_point(entry: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """A fresh process: nothing an earlier test registered can hide the order."""
    module, subcommand = ENTRY_POINTS[entry]
    return subprocess.run(
        [sys.executable, "-m", module, *args, *subcommand],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("variable", ["QBO_CLIENT_SECRET", "QBO_TOKEN_KEY", "QBO_IMPORT_REFRESH_TOKEN"])
@pytest.mark.parametrize("entry", list(ENTRY_POINTS))
def test_a_secret_in_the_environment_is_redacted_from_a_registry_error(
    tmp_path: Path, entry: str, variable: str
) -> None:
    """The registry loads before app credentials are built, so each entry
    point registers what the environment holds before anything can print."""
    from qbo_multicompany_mcp.redaction import REDACTED

    env = base_env() | {"QBO_ENTITIES_FILE": str(secret_as_a_field(tmp_path)), variable: PASTED}
    result = run_entry_point(entry, [], env)
    assert result.returncode == 2
    assert "unknown field" in result.stderr and REDACTED in result.stderr
    assert PASTED not in result.stderr + result.stdout


@pytest.mark.parametrize("entry", list(ENTRY_POINTS))
def test_a_secret_from_the_env_file_is_redacted_from_the_registry_error_after_it(
    tmp_path: Path, entry: str
) -> None:
    from qbo_multicompany_mcp.redaction import REDACTED

    env_file = tmp_path / "app.env"
    env_file.write_text(f"QBO_CLIENT_SECRET={PASTED}\n", encoding="utf-8")
    env = base_env() | {"QBO_ENTITIES_FILE": str(secret_as_a_field(tmp_path))}
    result = run_entry_point(entry, ["--env-file", str(env_file)], env)
    assert result.returncode == 2
    assert "unknown field" in result.stderr and REDACTED in result.stderr
    assert PASTED not in result.stderr + result.stdout
