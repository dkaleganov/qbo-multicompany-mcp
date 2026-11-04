"""No option may be abbreviated.

argparse expands any unambiguous prefix of an option by default, so
``--allow-doc`` would be ``--allow-documents`` and ``--enti`` would be
``--entities``. Anything that vets a command line by full option names misses
a prefix the package reads as the option it names: documents could be switched
on, and the registry, the token store and the env file replaced. These tests
are the package's own guarantee, on all three entry points.

The auth CLI's commands are parsers of their own, and ``add_parser`` does not
pass ``allow_abbrev`` down: ``revoke --al`` would be ``revoke --all``, every
company. Their options are held to the same rule. (Command NAMES already
need an exact match: ``rev`` is refused as an invalid choice.)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from qbo_multicompany_mcp import auth_cli, keepalive, server
from qbo_multicompany_mcp.config import add_common_arguments

from .conftest import with_source_root

PARSERS = {
    "server": server.build_parser,
    "auth": auth_cli.build_parser,
    "keepalive": keepalive.build_parser,
}

#: What each parser needs after the option under test: the auth CLI needs a
#: command, and ``status`` takes nothing of its own.
TAIL = {"server": [], "auth": ["status"], "keepalive": []}


def _long_options(parser: argparse.ArgumentParser) -> list[str]:
    return sorted(s for s in parser._option_string_actions if s.startswith("--"))


def _common_options() -> list[str]:
    """Every long option config.add_common_arguments defines."""
    probe = argparse.ArgumentParser(add_help=False)
    add_common_arguments(probe)
    return _long_options(probe)


def _takes_value(parser: argparse.ArgumentParser, option: str) -> bool:
    return parser._option_string_actions[option].nargs != 0


def _prefixes(option: str, siblings: list[str]) -> list[str]:
    """The prefixes argparse would have expanded to ``option``: the shortest
    one no other option of the parser shares, and the one a character short
    of the whole name (the likeliest typo)."""
    others = [s for s in siblings if s != option]
    unambiguous = [
        option[:n] for n in range(3, len(option))
        if not any(other.startswith(option[:n]) for other in others)
    ]
    return sorted({unambiguous[0], unambiguous[-1]}) if unambiguous else []


def _refused(parser: argparse.ArgumentParser, argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """Parse ``argv`` and return argparse's complaint; fail if it parsed."""
    try:
        parsed = parser.parse_args(argv)
    except SystemExit as exited:
        assert exited.code == 2, f"{argv} exited {exited.code}"
        return capsys.readouterr().err
    pytest.fail(f"{argv} was accepted as {vars(parsed)}")


@pytest.mark.parametrize("option", _common_options())
@pytest.mark.parametrize("name", sorted(PARSERS))
def test_no_prefix_of_a_common_option_is_accepted(name: str, option: str, capsys) -> None:
    parser = PARSERS[name]()
    prefixes = _prefixes(option, _long_options(parser))
    assert prefixes, f"{option} has no unambiguous prefix on {name}: this case would test nothing"
    for prefix in prefixes:
        given = f"{prefix}=synthetic" if _takes_value(parser, option) else prefix
        complaint = _refused(parser, [given, *TAIL[name]], capsys)
        assert f"unrecognized arguments: {prefix}" in complaint, complaint
        assert "synthetic" not in complaint


@pytest.mark.parametrize("option", ["--force", "--require-all"])
def test_no_prefix_of_a_keepalive_option_is_accepted(option: str, capsys) -> None:
    parser = keepalive.build_parser()
    for prefix in _prefixes(option, _long_options(parser)):
        complaint = _refused(parser, [prefix], capsys)
        assert f"unrecognized arguments: {prefix}" in complaint, complaint


@pytest.mark.parametrize(
    ("argv", "given"),
    [
        (["connect", "acme_main", "--man"], "--man"),
        (["connect", "acme_main", "--manua"], "--manua"),
        (["revoke", "--al"], "--al"),
    ],
)
def test_no_prefix_of_a_command_option_is_accepted(argv: list[str], given: str, capsys) -> None:
    """``revoke --al`` meant ``revoke --all``: every company's credential."""
    complaint = _refused(auth_cli.build_parser(), argv, capsys)
    assert f"unrecognized arguments: {given}" in complaint, complaint


@pytest.mark.parametrize("name", sorted(PARSERS))
def test_every_common_option_still_works_under_its_full_name(name: str) -> None:
    parser = PARSERS[name]()
    for option in _common_options():
        if option == "--version":
            continue  # prints and exits; covered below
        dest = parser._option_string_actions[option].dest
        if _takes_value(parser, option):
            for argv in ([option, "synthetic"], [f"{option}=synthetic"]):
                value = getattr(parser.parse_args([*argv, *TAIL[name]]), dest)
                assert value in ("synthetic", ["synthetic"]), (option, value)
        else:
            assert getattr(parser.parse_args([option, *TAIL[name]]), dest) is True, option


def test_the_version_and_the_command_options_still_work_under_their_full_names(capsys) -> None:
    with pytest.raises(SystemExit) as exited:
        server.build_parser().parse_args(["--version"])
    assert exited.value.code == 0 and "qbo-multicompany-mcp" in capsys.readouterr().out
    parser = auth_cli.build_parser()
    assert parser.parse_args(["connect", "acme_main", "--manual"]).manual is True
    assert parser.parse_args(["revoke", "--all"]).all is True
    assert parser.parse_args(["revoke", "acme_main"]).all is False
    options = keepalive.build_parser().parse_args(["--force", "--require-all"])
    assert options.force is True and options.require_all is True


def test_abbreviated_document_options_in_production_are_refused_by_the_server(tmp_path: Path) -> None:
    """Through the real entry point: ``--allow-doc --attachment-h`` in
    production. It is refused by the parser, exit 2, before the registry or
    any credential is read."""
    env = with_source_root({
        "PATH": "",
        "HOME": str(tmp_path),
        "QBO_ENV": "production",
        "QBO_ENTITIES_FILE": str(tmp_path / "absent.yaml"),
    })
    done = subprocess.run(
        [sys.executable, "-m", "qbo_multicompany_mcp.server", "--allow-doc", "--attachment-h", "files.example"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 2
    assert "unrecognized arguments: --allow-doc --attachment-h <value>" in done.stderr
    assert "files.example" not in done.stderr
    assert "absent.yaml" not in done.stderr, "the registry was reached; the parser should refuse first"
