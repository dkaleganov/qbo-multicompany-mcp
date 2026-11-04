"""Proves the gitleaks configuration actually fires.

A secret scanner that matches nothing reports zero findings forever, and looks
exactly like a clean repository. A realm rule anchored on the literal
``realm_id`` never fires on ``tests/conftest.py`` at all, whose fixtures are
named ``REALM_MAIN`` and ``REALM_OPS``: the scan reports zero, and the zero
means nothing.

So every rule declared in the config must be shown to fire on credential-shaped
input, and the allowlist must be shown *not* to be a blanket pass for the test
tree.

Every canary value here is built at run time from parts. None is a literal,
because a literal credential-shaped string in this file would both trip the
scanner it is testing and break the hygiene tests' rule on realistic fixtures.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import string
import subprocess
import tomllib
from pathlib import Path

import pytest

CONFIG = Path(".gitleaks.toml")

pytestmark = pytest.mark.skipif(
    shutil.which("gitleaks") is None, reason="gitleaks is not installed"
)


def rule_ids() -> set[str]:
    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    return {rule["id"] for rule in data["rules"]}


def scan(target: Path, tmp_path: Path) -> list[dict]:
    report = tmp_path / "report.json"
    subprocess.run(
        [
            "gitleaks", "dir", str(target),
            "--no-banner", "--redact",
            "-c", str(CONFIG.resolve()),
            "-f", "json", "-r", str(report),
            "--exit-code", "0",
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )
    if not report.exists() or not report.read_text().strip():
        return []
    return json.loads(report.read_text())


def canary_lines(rng: random.Random) -> dict[str, str]:
    """Credential-shaped, entirely synthetic, assembled at run time."""
    alphabet = string.ascii_letters + string.digits

    def run(n: int) -> str:
        return "".join(rng.choice(alphabet) for _ in range(n))

    # A sequential digit run: unmistakably synthetic, and -- the point -- not
    # the zero-padded shape the allowlist forgives.
    sequential_realm = "".join(str(d % 10) for d in range(1, 17))
    jwt_prefix = "ey" + "J"

    return {
        "qbo-client-id": f"QBO_CLIENT_ID=AB{run(62)}",
        "qbo-client-secret": f"QBO_CLIENT_SECRET={run(40)}",
        "qbo-refresh-token": f'refresh_token = "AB{run(40)}"',
        "qbo-access-token": f'access_token: "{jwt_prefix}{run(20)}.{run(40)}.{run(43)}"',
        "qbo-bearer-token": f"Authorization: Bearer {run(48)}",
        "qbo-token-store-key": f"QBO_TOKEN_KEY={run(43)}=",
        "qbo-realm-id": f'realm_id: "{sequential_realm}"',
        "qbo-realm-id-in-url": f"GET /v3/company/{sequential_realm}/preferences",
    }


def test_every_declared_rule_is_covered_by_a_canary() -> None:
    """Adding a rule without a canary fails here, so no rule ships unproven."""
    assert rule_ids() == set(canary_lines(random.Random(0)))


def test_every_rule_fires_on_credential_shaped_input(tmp_path: Path) -> None:
    rng = random.Random(1111)
    lines = canary_lines(rng)
    target = tmp_path / "canary"
    target.mkdir()
    (target / "leaky.env").write_text("\n".join(lines.values()) + "\n", encoding="utf-8")

    fired = {finding["RuleID"] for finding in scan(target, tmp_path)}
    assert rule_ids() <= fired, f"rules that never fired: {rule_ids() - fired}"


@pytest.mark.parametrize("rule_id", sorted(canary_lines(random.Random(0))))
def test_each_rule_fires_in_isolation(rule_id: str, tmp_path: Path) -> None:
    """One rule per file, so a rule cannot be credited for another's match."""
    target = tmp_path / rule_id
    target.mkdir()
    line = canary_lines(random.Random(4242))[rule_id]
    (target / "leaky.env").write_text(line + "\n", encoding="utf-8")

    fired = {finding["RuleID"] for finding in scan(target, tmp_path)}
    assert rule_id in fired, f"{rule_id} did not fire on its own canary"


def test_the_test_tree_is_not_allowlisted_by_path(tmp_path: Path) -> None:
    """Allowlisting tests/ would silence the fixtures and also switch off every
    rule in the directory most likely to collect a real token while debugging.
    """
    sequential_realm = "".join(str(d % 10) for d in range(1, 17))
    placeholder_realm = "0" * 15 + "1"

    target = tmp_path / "repo"
    (target / "tests").mkdir(parents=True)
    (target / "tests" / "conftest.py").write_text(
        f'REALM_MAIN = "{placeholder_realm}"\nREALM_LEAKED = "{sequential_realm}"\n',
        encoding="utf-8",
    )

    findings = scan(target, tmp_path)
    lines = {finding["StartLine"] for finding in findings}

    assert 1 not in lines, "the zero-padded placeholder realm should be allowlisted"
    assert 2 in lines, "a non-placeholder realm under tests/ must still be caught"


def test_placeholder_realms_are_allowlisted_by_value_not_by_file(tmp_path: Path) -> None:
    placeholder = "0" * 15 + "1"
    target = tmp_path / "anywhere"
    target.mkdir()
    (target / "some_module.py").write_text(f'REALM = "{placeholder}"\n', encoding="utf-8")
    assert scan(target, tmp_path) == []


def test_a_placeholder_realm_with_a_long_tail_is_still_a_finding(tmp_path: Path) -> None:
    """The placeholder is a long run of zeros with a SHORT tail. Unanchored at
    the end, the allowlist would forgive any realm that begins with zeros."""
    placeholder = "0" * 15 + "1"
    long_tail = "0" * 8 + "".join(str(d) for d in range(1, 9))
    target = tmp_path / "realms"
    target.mkdir()
    (target / "module.py").write_text(
        f'REALM = "{placeholder}"\nREALM = "{long_tail}"\n'
        f"URL = '/v3/company/{placeholder}'\nURL = '/v3/company/{long_tail}'\n",
        encoding="utf-8",
    )
    lines = {finding["StartLine"] for finding in scan(target, tmp_path)}
    assert lines == {2, 4}, "only the long-tailed realms should be findings"


def test_a_value_that_only_begins_with_a_fixture_is_still_a_finding(tmp_path: Path) -> None:
    """The fixture JWT is forgiven whole. The same text with more after it is
    a token-shaped value like any other."""
    rng = random.Random(11)
    fixture = "ey" + "J" + "hbGciOiJIUzI1NiJ9" + ".payloadpayload.signature"
    longer = fixture + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(40))
    target = tmp_path / "fixture"
    target.mkdir()
    (target / "module.py").write_text(f'sample = "{fixture}"\nsample = "{longer}"\n', encoding="utf-8")
    findings = scan(target, tmp_path)
    assert not [f for f in findings if f["StartLine"] == 1], "the fixture itself should be allowlisted"
    assert ("qbo-access-token", 2) in {(f["RuleID"], f["StartLine"]) for f in findings}


def test_the_repository_working_tree_is_clean(tmp_path: Path) -> None:
    assert scan(Path("."), tmp_path) == []


def test_the_commit_history_is_clean(tmp_path: Path) -> None:
    report = tmp_path / "history.json"
    subprocess.run(
        [
            "gitleaks", "git", ".",
            "--no-banner", "--redact",
            "-c", str(CONFIG.resolve()),
            "-f", "json", "-r", str(report),
            "--exit-code", "0",
        ],
        capture_output=True,
        check=True,
        timeout=180,
    )
    body = report.read_text().strip() if report.exists() else ""
    findings = json.loads(body) if body else []
    assert findings == [], [f["RuleID"] for f in findings]


def test_config_extends_the_upstream_ruleset() -> None:
    """A contributor is likelier to paste a cloud credential than an Intuit one,
    and those rules are already written and maintained upstream."""
    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    assert data["extend"]["useDefault"] is True


# --------------------------------------------------------------------------
# The code-reference allowlist exempts code references, not a credential shape
# --------------------------------------------------------------------------
#
# A "bare lowercase identifiers" allowlist that matches
# `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$` against the secret a rule found
# forgives any lowercase alphanumeric run of any length: a generated
# 48-character lowercase secret produces no finding while the same value
# uppercased does. What needs exempting is a dotted attribute reference like
# `credentials.client_secret`, and that is all the allowlist admits.


#: The looser shape, kept here so each canary can be shown to be inside it.
#: A canary that happens to start with a digit escapes it by luck and proves
#: nothing about it.
BARE_IDENTIFIER_SHAPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


def lowercase_canaries(rng: random.Random) -> dict[str, str]:
    """High-entropy and entirely lowercase: the shape the looser pattern forgives."""
    alphabet = string.ascii_lowercase + string.digits

    def run(n: int) -> str:
        # A letter first, so every value is inside the looser shape.
        return rng.choice(string.ascii_lowercase) + "".join(
            rng.choice(alphabet) for _ in range(n - 1)
        )

    return {
        "qbo-client-id": f"QBO_CLIENT_ID={run(40)}",
        "qbo-client-secret": f'client_secret = "{run(48)}"',
        "qbo-refresh-token": f'refresh_token = "{run(48)}"',
        "qbo-bearer-token": f"Authorization: Bearer {run(48)}",
    }


@pytest.mark.parametrize("rule_id", sorted(lowercase_canaries(random.Random(0))))
def test_a_lowercase_secret_is_still_a_finding(rule_id: str, tmp_path: Path) -> None:
    target = tmp_path / rule_id
    target.mkdir()
    line = lowercase_canaries(random.Random(2026))[rule_id]
    value = re.split(r"[=\s\"]+", line.rstrip('"'))[-1]
    assert BARE_IDENTIFIER_SHAPE.match(value), "the canary must be in the forgiven shape"
    (target / "leaky.env").write_text(line + "\n", encoding="utf-8")

    fired = {finding["RuleID"] for finding in scan(target, tmp_path)}
    assert rule_id in fired, f"{rule_id} was allowlisted for being lowercase"


def test_an_unquoted_dotted_lowercase_secret_is_still_a_finding(tmp_path: Path) -> None:
    """A `.env` file does not quote, and a dot is inside the secret alphabet.

    Requiring only "dotted" would still forgive `a1b2...c3.d4e5...f6`; the
    reference has to end in the attribute it names.
    """
    rng = random.Random(7)
    alphabet = string.ascii_lowercase + string.digits
    value = ".".join("".join(rng.choice(alphabet) for _ in range(20)) for _ in range(2))
    target = tmp_path / "dotted"
    target.mkdir()
    (target / ".env").write_text(f"QBO_CLIENT_SECRET={value}\n", encoding="utf-8")

    fired = {finding["RuleID"] for finding in scan(target, tmp_path)}
    assert "qbo-client-secret" in fired


@pytest.mark.parametrize(
    "line",
    [
        "        client_secret=credentials.client_secret,",
        '    extra={"QBO_CLIENT_SECRET": credentials.client_secret},',
        "    refresh_token=self.row.refresh_token,",
    ],
)
def test_a_code_reference_is_still_not_a_finding(line: str, tmp_path: Path) -> None:
    """The control. Without it, deleting the allowlist would pass the canaries
    above and fail the working-tree scan for a reason nobody would read."""
    target = tmp_path / "code"
    target.mkdir()
    (target / "module.py").write_text(line + "\n", encoding="utf-8")
    assert scan(target, tmp_path) == []
