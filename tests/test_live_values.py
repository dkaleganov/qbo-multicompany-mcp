"""The live-value registry: exact values, not shapes.

The redactor matches shapes -- a bearer header, a JWT, a long opaque run,
``name=value``. A credential with none of those shapes walks straight past it:
a short punctuated client secret in an exception message, a token reissued in a
format nobody anticipated. The registry holds the values this process actually
loaded, and redaction removes each exact occurrence whatever it looks like.

Every value below is built to dodge every shape rule, so a pass here can only
come from the registry.
"""

from __future__ import annotations

import ast
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from qbo_multicompany_mcp import oauth
from qbo_multicompany_mcp.redaction import REDACTED, RedactingFilter, redact
from qbo_multicompany_mcp.store import AppCredentials, RefreshActor

from .conftest import REALM_MAIN

#: Lowercase, hyphenated, 29 characters, no run long enough for the opaque
#: rule, no `name=` or `name:` in front of it, no bearer, no JWT. Only an
#: exact-value match can catch it.
SHAPELESS_SECRET = "purple-monkey-dishwasher-4471"


def _live_response(access: str, refresh: str) -> oauth.TokenResponse:
    """A pair built the way Intuit's answer is parsed: not marked synthetic."""
    now = datetime.now(UTC)
    return oauth.TokenResponse(
        access_token=access,
        refresh_token=refresh,
        access_token_expires_at=now + timedelta(hours=1),
        refresh_token_expires_at=now + timedelta(days=100),
    )


def test_the_shape_rules_alone_miss_the_shapeless_secret() -> None:
    """The control. Without it, the tests below would pass on a redactor that
    happened to match this value by shape, and prove nothing about a registry."""
    from qbo_multicompany_mcp import redaction

    text = f"boom: {SHAPELESS_SECRET} was rejected"
    for pattern, replacement in redaction._PATTERNS:
        text = pattern.sub(replacement, text)
    assert SHAPELESS_SECRET in text


def test_a_loaded_client_secret_is_redacted_whatever_its_shape() -> None:
    AppCredentials(client_id="some-client", client_secret=SHAPELESS_SECRET)
    masked = redact(f"token endpoint said no to {SHAPELESS_SECRET} today")
    assert SHAPELESS_SECRET not in masked
    assert REDACTED in masked


def test_a_token_committed_to_the_store_is_redacted_whatever_its_shape(
    settings, credentials, actor: RefreshActor
) -> None:
    access = "zq-access-looks-like-nothing-91"
    refresh = "zq-refresh-looks-like-nothing-92"
    settings.open_store(credentials=credentials).commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=_live_response(access, refresh), actor=actor,
    )
    masked = redact(f"sent {access}; kept {refresh}")
    assert access not in masked and refresh not in masked


def test_a_token_decoded_from_disk_is_redacted_whatever_its_shape() -> None:
    """The restart path: a process that never received the pair itself, and
    knows it only because it read the store."""
    from qbo_multicompany_mcp.store import TokenRow

    access = "zq-decoded-from-disk-access-61"
    refresh = "zq-decoded-from-disk-refresh-62"
    TokenRow.from_json(
        {
            "entity": "acme_main",
            "environment": "sandbox",
            "client_id_fingerprint": "f",
            "refresh_token": refresh,
            "access_token": access,
            "access_token_expires_at": "2030-01-01T00:00:00Z",
            "refresh_token_expires_at": "2030-01-01T00:00:00Z",
            "consented_at": "2026-01-01T00:00:00Z",
            "auth_generation": 1,
        }
    )
    masked = redact(f"{access} and {refresh}")
    assert access not in masked and refresh not in masked


def test_a_pair_received_from_the_token_endpoint_is_redacted() -> None:
    access = "zq-fresh-from-intuit-access-31"
    refresh = "zq-fresh-from-intuit-refresh-32"
    response = httpx.Response(
        200,
        json={"access_token": access, "refresh_token": refresh, "expires_in": 3600},
    )
    oauth._parse_token_response(response)
    masked = redact(f"{access} / {refresh}")
    assert access not in masked and refresh not in masked


def test_a_log_record_carrying_a_registered_value_is_redacted() -> None:
    AppCredentials(client_id="some-client", client_secret=SHAPELESS_SECRET)
    record = logging.LogRecord(
        "qbo", logging.ERROR, __file__, 1, "failed with %s", (SHAPELESS_SECRET,), None
    )
    RedactingFilter().filter(record)
    assert SHAPELESS_SECRET not in record.getMessage()


def test_a_value_too_short_for_text_scanning_is_held_but_not_redacted() -> None:
    """A two-letter secret is registered -- identity answers for any value the
    process holds -- and only the substring scan that redaction runs over text
    leaves it out, because redacting every 'is' in every message would be the
    worse failure. That floor is a false-positive policy for text scanning,
    not a claim about credentials."""
    from qbo_multicompany_mcp import live_values

    AppCredentials(client_id="c", client_secret="is")
    assert live_values.holds("is")
    assert redact("this is fine") == "this is fine"


def test_an_exact_held_value_is_redacted_whatever_its_length() -> None:
    """Identity redaction is exact at any length; the sixteen-character floor
    governs SUBSTRING scanning only. ``zq!short`` is eight characters and
    shapeless, so no pattern and no substring scan can catch it: only an exact
    identity check before the scan does. Without one, holds() is true and
    redact() returns the value unchanged."""
    from qbo_multicompany_mcp import live_values

    token = "zq!short"
    live_values.register(token)
    assert live_values.holds(token)
    assert redact(token) == REDACTED
    # The control: the same value as a substring of longer text is not scanned
    # for, because it is under the text floor. That is the scanning policy,
    # not identity.
    assert redact("prefix zq!short suffix") == "prefix zq!short suffix"


def test_the_registry_holds_what_it_was_given_and_nothing_else() -> None:
    from qbo_multicompany_mcp import live_values

    live_values.register("zq-registered-value-for-holds-test")
    assert live_values.holds("zq-registered-value-for-holds-test")
    assert not live_values.holds("zq-never-registered-value-at-all")


def test_a_synthetic_pair_is_not_registered() -> None:
    """Test fixtures mark their pairs synthetic. Nothing else does, so every
    pair Intuit issues -- and every pair of unknown provenance on disk -- is
    live by default."""
    from qbo_multicompany_mcp import live_values

    now = datetime.now(UTC)
    access, refresh = "zq-synthetic-access-not-live-51", "zq-synthetic-refresh-not-live-52"
    oauth.TokenResponse(
        access_token=access,
        refresh_token=refresh,
        access_token_expires_at=now,
        refresh_token_expires_at=now,
        synthetic=True,
    )
    assert not live_values.holds(access)
    assert not live_values.holds(refresh)


def test_the_registry_is_memory_only() -> None:
    """Never persisted. The module may not so much as import a way to write."""
    source = Path("src/qbo_multicompany_mcp/live_values.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "open", "the registry may not open a file"
    assert not imported & {"os", "pathlib", "io", "shutil", "tempfile", "json", "pickle"}


def test_no_source_file_marks_a_pair_synthetic() -> None:
    """The one way to keep a pair out of the registry is `synthetic=True`, and
    only test fixtures may write it. A source file that did would be quietly
    exempting real tokens from redaction and from the test-origin check."""
    offenders = _files_marking_synthetic(Path("src/qbo_multicompany_mcp").rglob("*.py"))
    assert offenders == []


def test_the_synthetic_marker_detector_fires_on_a_planted_source(tmp_path: Path) -> None:
    """The canary for the test above."""
    planted = tmp_path / "planted.py"
    planted.write_text("x = TokenResponse(access_token='a', synthetic=True)\n")
    assert _files_marking_synthetic([planted]) == ["planted.py"]


def _files_marking_synthetic(paths) -> list[str]:
    found: list[str] = []
    for path in paths:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "synthetic":
                if isinstance(node.value, ast.Constant) and node.value.value is True:
                    found.append(Path(path).name)
    return found
