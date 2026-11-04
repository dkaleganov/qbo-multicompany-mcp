"""Proves: a refused command line keeps option-shaped words and drops the rest.

``argparse`` prints what it refuses straight to stderr, past the redacting
hooks, and stderr can end up in a client's log. A stray argument can be a
company id or a secret. So each case here goes through a real parser, and the
last through the real entry points, with a marked value that must not come back.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from qbo_multicompany_mcp import auth_cli, keepalive, server
from qbo_multicompany_mcp.redaction import ValueFreeParser

from .conftest import REALM_MAIN, with_source_root

PARSERS = {
    "server": server.build_parser,
    "auth": auth_cli.build_parser,
    "keepalive": keepalive.build_parser,
}

#: What each parser needs before the argument under test.
HEAD = {"server": [], "auth": ["status"], "keepalive": []}

#: Nothing the redactor would recognise, so only dropping it keeps it out.
MARK = "marked-value"


def _refused(parser, argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as exited:
        parser.parse_args(argv)
    assert exited.value.code == 2
    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err.startswith("usage:")
    return printed.err


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        ([MARK], "unrecognized arguments: <value>"),
        ([f"--thing={MARK}"], "unrecognized arguments: --thing"),
        (["--thing", MARK], "unrecognized arguments: --thing <value>"),
        ([f"--thing={MARK}", MARK, "--other"], "unrecognized arguments: --thing <value> --other"),
        ([f"-x{MARK}"], "unrecognized arguments: <value>"),
    ],
)
@pytest.mark.parametrize("name", sorted(PARSERS))
def test_a_stray_argument_is_named_at_most_and_its_value_is_dropped(name, argv, reason, capsys) -> None:
    complaint = _refused(PARSERS[name](), [*HEAD[name], *argv], capsys)
    assert reason in complaint, complaint
    assert MARK not in complaint


@pytest.mark.parametrize("name", sorted(PARSERS))
def test_a_value_a_flag_cannot_take_is_dropped(name, capsys) -> None:
    argv = [f"--allow-plaintext-store={MARK}", *HEAD[name]]
    complaint = _refused(PARSERS[name](), argv, capsys)
    assert "argument --allow-plaintext-store: ignored explicit argument" in complaint, complaint
    assert MARK not in complaint


@pytest.mark.parametrize("name", sorted(PARSERS))
def test_text_joined_to_a_short_option_is_not_repeated(name, capsys) -> None:
    """``-hvalue`` is a refusal on one Python and plain help on another."""
    with pytest.raises(SystemExit):
        PARSERS[name]().parse_args([f"-h{MARK}", *HEAD[name]])
    printed = capsys.readouterr()
    assert MARK not in printed.out + printed.err


def test_a_bad_command_name_is_refused_without_repeating_it(capsys) -> None:
    complaint = _refused(auth_cli.build_parser(), [MARK], capsys)
    assert "argument command: invalid choice" in complaint, complaint
    assert MARK not in complaint


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["connect", "acme_main", MARK], "unrecognized arguments: <value>"),
        (["connect", "acme_main", f"--thing={MARK}"], "unrecognized arguments: --thing"),
        (["connect", "acme_main", f"--manual={MARK}"], "argument --manual: ignored explicit argument"),
        (["revoke", f"--all={MARK}"], "argument --all: ignored explicit argument"),
        (["revoke", f"--al={MARK}"], "unrecognized arguments: --al"),
    ],
)
def test_a_command_refuses_the_same_way_as_the_top_level(argv, reason, capsys) -> None:
    """Each command is a parser of its own, so the rule has to reach it too."""
    complaint = _refused(auth_cli.build_parser(), argv, capsys)
    assert reason in complaint, complaint
    assert MARK not in complaint


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["--entities"], "argument --entities: expected one argument"),
        (["connect"], "the following arguments are required: entity"),
    ],
)
def test_a_reason_with_no_value_in_it_is_printed_whole(argv, reason, capsys) -> None:
    assert reason in _refused(auth_cli.build_parser(), argv, capsys)


@pytest.mark.parametrize("word", [f"--realm{REALM_MAIN}", f"--realm-{REALM_MAIN}", "--" + "Ab1" * 16])
def test_a_value_shaped_like_an_option_still_goes_through_the_redactor(word, capsys) -> None:
    complaint = _refused(server.build_parser(), [word], capsys)
    assert word not in complaint
    assert REALM_MAIN not in complaint


@pytest.mark.parametrize(
    ("module", "head"),
    [
        ("qbo_multicompany_mcp.server", []),
        ("qbo_multicompany_mcp.keepalive", []),
        ("qbo_multicompany_mcp.auth_cli", ["status"]),
    ],
)
def test_the_real_entry_points_print_no_value(module: str, head: list[str], tmp_path: Path) -> None:
    env = with_source_root({"PATH": "", "HOME": str(tmp_path), "QBO_ENV": "sandbox"})
    done = subprocess.run(
        [sys.executable, "-m", module, *head, f"--thing={MARK}", REALM_MAIN],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 2
    assert "unrecognized arguments: --thing <value>" in done.stderr
    assert MARK not in done.stderr and REALM_MAIN not in done.stderr


@pytest.mark.parametrize("command", ["connect", "import", "verify"])
def test_the_auth_cli_does_not_repeat_a_company_key_it_does_not_know(
    command: str, entities_file: Path, tmp_path: Path
) -> None:
    """Past the parser, this is the one word the auth CLI takes as it comes."""
    env = with_source_root({"PATH": "", "HOME": str(tmp_path), "QBO_ENV": "sandbox"})
    done = subprocess.run(
        [sys.executable, "-m", "qbo_multicompany_mcp.auth_cli", "--entities", str(entities_file), command, MARK],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode != 0
    assert "unknown entity. Configured entities: acme_main, acme_ops" in done.stderr + done.stdout
    assert MARK not in done.stderr + done.stdout


def test_the_parser_class_turns_abbreviation_off_by_itself(capsys) -> None:
    """A prefix that fits two options is the one refusal argparse prints
    unquoted, so a parser that forgot the flag would print ``--al=value``."""
    parser = ValueFreeParser(prog="sample")
    parser.add_argument("--alpha")
    parser.add_argument("--alps")
    complaint = _refused(parser, [f"--al={MARK}"], capsys)
    assert "unrecognized arguments: --al" in complaint, complaint
    assert MARK not in complaint
