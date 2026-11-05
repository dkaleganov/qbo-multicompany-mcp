"""Checks the repository for the leaks it knows to look for, from its first commit.

Everything here is about what a stranger reading this tree could mistake for
real configuration, or could harvest. A realistic realm id in a public
repository is a standing invitation to guess the rest; a realistic company name
in an example file gets copied into a real one.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO = Path(".")

TRACKED_TEXT_SUFFIXES = {
    ".py", ".yaml", ".yml", ".toml", ".md", ".html", ".txt", ".cfg", ".json", ".lock", ""
}

#: Fixture and example realms must be this shape: a long run of zeros then one
#: digit. Unmistakably a placeholder, and the right length to exercise the code.
FIXTURE_REALM_RE = re.compile(r"^0{12,15}[0-9]$")

#: Any long digit run tracked in this repository must be this obviously a
#: placeholder: a run of zeros with a short tail. Broader than the realm rule
#: above, because fixtures legitimately carry other long fake numbers -- a bank
#: account number, say -- and those must look fake too.
PLACEHOLDER_DIGITS_RE = re.compile(r"^0{8,}[0-9]{0,4}$")


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True, cwd=REPO
    )
    return [Path(line) for line in result.stdout.splitlines() if line]


def text_files() -> list[Path]:
    return [
        path
        for path in tracked_files()
        if path.suffix in TRACKED_TEXT_SUFFIXES and path.is_file()
    ]


# --------------------------------------------------------------------------
# Fixture realms
# --------------------------------------------------------------------------


def test_example_registry_realms_are_obvious_placeholders() -> None:
    document = yaml.safe_load(Path("entities.example.yaml").read_text(encoding="utf-8"))
    realms = [entity["realm_id"] for entity in document["entities"].values()]
    assert realms
    for realm in realms:
        assert FIXTURE_REALM_RE.match(str(realm)), realm


def test_example_registry_realms_are_distinct() -> None:
    """One shared placeholder would make the file this project tells people to
    copy fail on first run."""
    document = yaml.safe_load(Path("entities.example.yaml").read_text(encoding="utf-8"))
    realms = [str(entity["realm_id"]) for entity in document["entities"].values()]
    assert len(realms) == len(set(realms))


def test_conftest_fixture_realms_are_obvious_placeholders() -> None:
    from . import conftest

    for name in ("REALM_MAIN", "REALM_OPS", "REALM_ABSENT"):
        assert FIXTURE_REALM_RE.match(getattr(conftest, name)), name


def test_no_realistic_realm_id_is_tracked_anywhere() -> None:
    """A realistic realm id is a 16-digit run that is not a placeholder."""
    long_digits = re.compile(r"(?<!\d)\d{12,20}(?!\d)")
    offenders: list[tuple[str, str]] = []
    for path in text_files():
        if path.name == "uv.lock":
            continue  # hashes, not identifiers
        for match in long_digits.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            if not PLACEHOLDER_DIGITS_RE.match(match.group()):
                offenders.append((str(path), match.group()))
    assert not offenders, offenders


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------


def test_example_company_names_are_obviously_fake() -> None:
    document = yaml.safe_load(Path("entities.example.yaml").read_text(encoding="utf-8"))
    names = [entity["display_name"] for entity in document["entities"].values()]
    assert names == ["Acme Main", "Acme Ops"]


def _host_is_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    """An exact match or a dot-delimited subdomain, so a sibling such as
    notexample.com doesn't pass as example.com."""
    return any(host == name.lstrip(".") or host.endswith("." + name.lstrip(".")) for name in allowed)


def test_a_sibling_of_an_allowed_host_is_not_allowed() -> None:
    assert _host_is_allowed("docs.example.com", ("example.com",))
    assert _host_is_allowed("example.com", ("example.com",))
    assert not _host_is_allowed("notexample.com", ("example.com",))
    assert not _host_is_allowed("example.com.other.test", ("example.com",))


def test_only_reserved_example_domains_appear() -> None:
    """Hosts in tracked text are RFC 2606 / RFC 6761 reserved names, Intuit's own,
    the two public hosts the docs link to, or the two hosts the listing files add."""
    hosts = re.compile(r"https?://(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.\-:@]+)")
    # RFC 2606 / RFC 6761 reserved names, plus the one real vendor host this
    # package talks to and the host of this repository's own URLs. `.example`
    # is a reserved TLD in its own right, which is what documentation hostnames
    # like oauth.your-domain.example use. server.json and glama.json name two of
    # the registries the package is listed on, PyPI and Glama.
    allowed_suffixes = (
        ".intuit.com",
        ".example",
        ".example.com",
        ".example.invalid",
        "example.com",
        "example.invalid",
        "127.0.0.1",
        "localhost",
        "modelcontextprotocol.io",
        "github.com",
        "pypi.org",
        "glama.ai",
    )
    offenders: list[tuple[str, str]] = []
    for path in text_files():
        if path.name == "uv.lock":
            continue
        for host in hosts.findall(path.read_text(encoding="utf-8", errors="ignore")):
            bare = host.rsplit("@", 1)[-1]
            if not bare.startswith("["):
                bare = bare.split(":")[0]
            if _host_is_allowed(bare, allowed_suffixes):
                continue
            if _is_documentation_address(bare):
                continue
            offenders.append((str(path), host))
    assert not offenders, offenders


def _is_documentation_address(host: str) -> bool:
    """RFC 5737 and RFC 3849 ranges reserved for documentation.

    These are the correct thing to write in a test that needs an IP literal --
    they are guaranteed not to route anywhere and cannot become somebody's real
    host. Reserved *names* were already allowed above; this adds reserved
    *addresses*, which is what the redirect-URI tests need.
    """
    import ipaddress

    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    documentation = (
        ipaddress.ip_network("192.0.2.0/24"),      # RFC 5737 TEST-NET-1
        ipaddress.ip_network("198.51.100.0/24"),   # RFC 5737 TEST-NET-2
        ipaddress.ip_network("203.0.113.0/24"),    # RFC 5737 TEST-NET-3
        ipaddress.ip_network("2001:db8::/32"),     # RFC 3849
    )
    return any(address in network for network in documentation if address.version == network.version)


# --------------------------------------------------------------------------
# Secrets and ignored paths
# --------------------------------------------------------------------------


def test_gitignore_covers_real_configuration_and_credentials() -> None:
    body = Path(".gitignore").read_text(encoding="utf-8")
    for pattern in ("entities.yaml", ".env", "tokens.*", "*.enc"):
        assert any(line.strip() == pattern for line in body.splitlines()), pattern


def test_gitignore_has_no_lock_wildcard_that_would_exclude_uv_lock() -> None:
    """A blanket *.lock would silently exclude the committed uv.lock."""
    body = Path(".gitignore").read_text(encoding="utf-8")
    assert not any(line.strip() == "*.lock" for line in body.splitlines())


def test_uv_lock_is_committed() -> None:
    assert Path("uv.lock") in tracked_files()


def test_no_real_configuration_file_is_tracked() -> None:
    tracked = {str(path) for path in tracked_files()}
    for forbidden in ("entities.yaml", ".env"):
        assert forbidden not in tracked


def test_no_token_store_or_encrypted_blob_is_tracked() -> None:
    for path in tracked_files():
        assert not path.name.startswith("tokens."), path
        assert path.suffix != ".enc", path


#: Assembled from parts so the literal never appears contiguously in this file
#: -- otherwise the scan below finds itself.
PEM_PRIVATE_KEY_RE = re.compile("-{5}BEGIN [A-Z ]{0,24}PRIVATE" + " KEY-{5}")


def test_no_private_key_material_is_tracked() -> None:
    offenders = [
        str(path)
        for path in text_files()
        if PEM_PRIVATE_KEY_RE.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not offenders, offenders


def test_the_private_key_scan_actually_matches_a_key_header() -> None:
    """A scan that matches nothing would pass forever. Prove it fires."""
    sample = "-" * 5 + "BEGIN RSA PRIVATE" + " KEY" + "-" * 5
    assert PEM_PRIVATE_KEY_RE.search(sample)
    assert not PEM_PRIVATE_KEY_RE.search("BEGIN OF THE FILE")


def test_commit_messages_carry_no_realm_or_secret() -> None:
    result = subprocess.run(
        ["git", "log", "--format=%B"], capture_output=True, text=True, check=True, cwd=REPO
    )
    body = result.stdout
    for match in re.finditer(r"(?<!\d)\d{12,20}(?!\d)", body):
        assert FIXTURE_REALM_RE.match(match.group()), match.group()
    for marker in ("client_secret", "refresh_token=", "Bearer "):
        assert marker not in body, marker


# --------------------------------------------------------------------------
# The scanner config's fixture credentials
# --------------------------------------------------------------------------

#: A fixture entry as the scanner config lists it: anchored literal text of
#: letters, digits, ``_``, ``-`` and ``\.``, with alternatives only inside
#: groups. Anything looser also forgives a real credential that begins, ends or
#: merely looks like a fixture, in any file, so any other character fails it too.
LISTED_WHOLE = re.compile(
    r"\^(?:[A-Za-z0-9_-]|\\\.|\((?:[A-Za-z0-9_-]|\\\.)+(?:\|(?:[A-Za-z0-9_-]|\\\.)+)*\))+\$"
)


@pytest.mark.parametrize(
    ("regex", "whole"),
    [
        ("^test-client-(id|secret)$", True),
        ("^tok\\.(a\\.b|c\\.d)$", True),
        ("^fixture", False),
        ("^a|b$", False),
        ("^tok\\..*$", False),
        ("^XAB11[A-Za-z0-9]+$", False),
        ("^fixture\\$", False),
    ],
)
def test_the_listed_whole_shape_admits_only_anchored_literals(regex: str, whole: bool) -> None:
    assert bool(LISTED_WHOLE.fullmatch(regex)) is whole


def test_every_fixture_credential_in_the_scanner_config_is_listed_whole() -> None:
    """Read from the config itself, so it runs where gitleaks isn't installed."""
    import tomllib

    data = tomllib.loads(Path(".gitleaks.toml").read_text(encoding="utf-8"))
    (listed,) = [a for a in data["allowlists"] if a["description"].startswith("Synthetic fixture credentials")]
    assert [regex for regex in listed["regexes"] if not LISTED_WHOLE.fullmatch(regex)] == []


# --------------------------------------------------------------------------
# Packaging
# --------------------------------------------------------------------------


def test_pyproject_declares_python_the_dependency_ranges_and_the_build_backend() -> None:
    """Dependency ranges here, their resolved versions in uv.lock. Hatchling is
    declared here without a version and isn't in uv.lock, as README and
    SECURITY say."""
    import tomllib

    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]

    assert project["requires-python"] == ">=3.11"
    assert project["license"] == "MIT"  # PEP 639

    dependencies = {
        name.split(">")[0].split("<")[0].split("=")[0].strip(): name
        for name in project["dependencies"]
    }
    assert dependencies["mcp"] == "mcp>=2.2,<3"
    assert dependencies["httpx"] == "httpx>=0.28,<0.29"
    assert dependencies["pydantic"] == "pydantic>=2.13,<3"
    assert dependencies["pyyaml"] == "pyyaml>=6,<7"
    assert dependencies["python-dotenv"] == "python-dotenv>=1.2,<2"
    assert dependencies["cryptography"] == "cryptography>=46,<60"

    dev = {name.split(">")[0].strip(): name for name in project["optional-dependencies"]["dev"]}
    assert dev["pytest"] == "pytest>=9.1,<10"
    assert dev["anyio"] == "anyio>=4.15,<5"

    assert data["build-system"]["requires"] == ["hatchling"]
    assert data["build-system"]["build-backend"] == "hatchling.build"
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/qbo_multicompany_mcp"
    ]


def test_all_three_console_scripts_are_declared() -> None:
    import tomllib

    scripts = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert set(scripts) == {
        "qbo-multicompany-mcp",
        "qbo-multicompany-mcp-auth",
        "qbo-multicompany-mcp-keepalive",
    }


def test_the_third_party_fastmcp_package_is_not_a_dependency() -> None:
    """The official SDK's v2 line is what this pins; `fastmcp` on PyPI is a
    different project."""
    import tomllib

    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    everything = data["project"]["dependencies"] + data["project"]["optional-dependencies"]["dev"]
    assert not any(name.strip().startswith("fastmcp") for name in everything)


def test_license_file_is_present_and_is_mit() -> None:
    body = Path("LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in body
    assert "WITHOUT WARRANTY OF ANY KIND" in body


def test_the_design_doc_states_the_incomplete_sync_detection() -> None:
    """Saying so is part of the check's job."""
    body = " ".join(Path("docs/design.md").read_text(encoding="utf-8").split())
    assert "arbitrary sync software cannot be detected" in body


def test_the_secret_scanning_config_is_tracked() -> None:
    """An untracked .gitleaks.toml protects nobody's clone but this one."""
    assert Path(".gitleaks.toml") in tracked_files()


# --------------------------------------------------------------------------
# The scrub-check gate
# --------------------------------------------------------------------------


def test_the_scrub_check_passes() -> None:
    """Tree and full history, with the script's own control.

    A scan that errors can report clean: a shell check whose pattern the local
    grep rejects, followed by a trailing `||`, prints a pass because it is
    broken. So the script refuses to report a pass unless its control fires.
    """
    import sys

    result = subprocess.run(
        [sys.executable, "scripts/scrub_check.py"],
        capture_output=True, text=True, check=False, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "control fired" in result.stdout


def test_the_scrub_check_detector_actually_fires() -> None:
    """The control, asserted here too rather than trusted from the script."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("scrub_check", "scripts/scrub_check.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["scrub_check"] = module
    spec.loader.exec_module(module)

    assert module.control_fires()
    # Built from parts: this file is scanned by the thing it is testing.
    realistic = "1234567890" + "123456"
    assert module.LONG_DIGITS.search(realistic)
    assert not module.PLACEHOLDER.match(realistic)
    # And the placeholder shape really is forgiven.
    assert module.PLACEHOLDER.match("0" * 15 + "1")


def _load_scrub_check():  # noqa: ANN202
    """Import the script by absolute path, so it survives a chdir."""
    import importlib.util
    import sys

    source = Path(__file__).resolve().parents[1] / "scripts" / "scrub_check.py"
    spec = importlib.util.spec_from_file_location("scrub_check", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["scrub_check"] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c", "user.email=nobody@example.invalid",
            "-c", "user.name=Scrub Check Fixture",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=repo, check=True, capture_output=True,
    )


def test_both_scrub_scanners_find_a_planted_identifier(
    tmp_path: Path, monkeypatch
) -> None:
    """The scanners are asserted by traversal, not by regex.

    The script's own control proves `LONG_DIGITS` matches a sample string. It
    says nothing about whether either scanner ever opens a file: with both
    replaced by `return []`, the two tests above still pass. So this plants a
    real identifier in a real repository and requires each scan to walk far
    enough to find it.

    The second half matters as much as the first. Deleting the file leaves the
    working tree clean while the blob stays in history, which is the case the
    history scan exists for and the one a tree-only scan reports as fine.
    """
    module = _load_scrub_check()

    # Assembled from parts: spelled out, this would be a long digit run in a
    # tracked file, and the thing under test scans tracked files.
    planted = "1234567890" + "1234"
    assert module.LONG_DIGITS.search(planted)
    assert not module.PLACEHOLDER.match(planted)

    repo = tmp_path / "planted"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "ledger.txt").write_text(f"account {planted} here\n", encoding="utf-8")
    _git(repo, "add", "ledger.txt")
    _git(repo, "commit", "-q", "-m", "plant an identifier")

    monkeypatch.chdir(repo)

    in_tree = module.scan_worktree()
    assert in_tree == [("ledger.txt", planted)], (
        "the working-tree scan did not reach a tracked file"
    )

    in_history = module.scan_objects()
    assert (("ledger.txt", planted)) in in_history, (
        "the history scan did not reach a committed blob"
    )

    # Now remove it from the tree. The blob survives in history, which is the
    # whole reason there are two scans.
    (repo / "ledger.txt").unlink()
    _git(repo, "rm", "-q", "--cached", "ledger.txt")
    _git(repo, "commit", "-q", "-m", "remove it from the tree only")

    assert module.scan_worktree() == []
    assert any(value == planted for _path, value in module.scan_objects()), (
        "a value removed from the tree but left in history was reported clean"
    )


def _repository_holding(text: str, tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "ledger.txt").write_text(text, encoding="utf-8")
    _git(repo, "add", "ledger.txt")
    _git(repo, "commit", "-q", "-m", "one file")
    monkeypatch.chdir(repo)
    return repo


def test_a_failed_scan_names_the_file_and_never_the_value(tmp_path: Path, monkeypatch, capsys) -> None:
    """A scan log must not become a second copy of what the scan found."""
    module = _load_scrub_check()
    planted = "1234567890" + "1234"
    repo = _repository_holding(f"account {planted} here\n", tmp_path, monkeypatch)

    assert module.main() == 1
    printed = capsys.readouterr()
    assert "in the working tree" in printed.out
    assert "ledger.txt: 1 long digit run(s)" in printed.out
    assert planted not in printed.out + printed.err

    (repo / "ledger.txt").write_text("nothing here\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "clean the tree, not the history")

    assert module.main() == 1
    printed = capsys.readouterr()
    assert "in history" in printed.out
    assert "ledger.txt: 1 long digit run(s)" in printed.out
    assert planted not in printed.out + printed.err


def test_an_unreadable_tracked_file_fails_the_scan(tmp_path: Path, monkeypatch, capsys) -> None:
    """The committed blob is clean and the file on disk can't be read, so what
    the working tree holds is unknown. That can't be a pass."""
    module = _load_scrub_check()
    _repository_holding("nothing here\n", tmp_path, monkeypatch)
    read_text = Path.read_text

    def refuse(self: Path, *args, **kwargs) -> str:
        if self.name == "ledger.txt":
            raise PermissionError(13, "Permission denied")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse)

    assert module.main() == 2
    printed = capsys.readouterr()
    assert "INCOMPLETE" in printed.err and "ledger.txt" in printed.err
    assert "PASS" not in printed.out


def test_a_tracked_file_deleted_from_the_tree_is_left_to_the_history_scan(tmp_path: Path, monkeypatch) -> None:
    """Nothing on disk to read isn't a failed read: its blob is still scanned."""
    module = _load_scrub_check()
    planted = "1234567890" + "1234"
    repo = _repository_holding(f"account {planted} here\n", tmp_path, monkeypatch)
    (repo / "ledger.txt").unlink()

    assert module.scan_worktree() == []
    assert module.main() == 1


def test_an_object_git_cannot_supply_fails_the_scan(tmp_path: Path, monkeypatch, capsys) -> None:
    module = _load_scrub_check()
    _repository_holding("nothing here\n", tmp_path, monkeypatch)
    absent = "f" * 40
    monkeypatch.setattr(module, "object_paths", lambda: {absent: "ledger.txt"})

    assert module.main() == 2
    printed = capsys.readouterr()
    assert "INCOMPLETE -- git could not supply object ffffffff" in printed.err
    assert "PASS" not in printed.out


def test_an_object_listing_that_stops_short_fails_the_scan(monkeypatch) -> None:
    module = _load_scrub_check()
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=b"no newline here"))

    with pytest.raises(module.ScanIncomplete, match="ended early"):
        module.object_contents(["f" * 40])


def _listing_of(monkeypatch, module, *objects: tuple[str, str, bytes]) -> None:
    """Have git's object listing answer with ``objects``."""
    listing = b"".join(
        f"{sha} {kind} {len(body)}\n".encode() + body + b"\n" for sha, kind, body in objects
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=listing))


def test_a_listing_with_an_object_missing_fails_the_scan(monkeypatch) -> None:
    module = _load_scrub_check()
    _listing_of(monkeypatch, module, ("a" * 40, "blob", b"one"))

    with pytest.raises(module.ScanIncomplete, match="1 of 2"):
        module.object_contents(["a" * 40, "b" * 40])


def test_a_listing_that_cuts_an_object_short_fails_the_scan(monkeypatch) -> None:
    module = _load_scrub_check()
    monkeypatch.setattr(
        module.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=f"{'a' * 40} blob 100\nshort".encode())
    )

    with pytest.raises(module.ScanIncomplete, match="cut an object short"):
        module.object_contents(["a" * 40])


def test_a_hash_in_a_commit_is_not_an_identifier_and_its_message_is_read(monkeypatch) -> None:
    """A hash can hold a long digit run by chance, in a header or quoted in a
    message, as ``git revert`` quotes one. Failing on that would leave only a
    history rewrite as the way out."""
    module = _load_scrub_check()
    planted = "1234567890" + "1234"
    run = "0123456789" + "012345"
    tree, parent = "abcdef" + run + "abcdef" * 3, "fedcba" + run + "fedcba" * 3
    assert module.LONG_DIGITS.search(tree) and module.LONG_DIGITS.search(parent)
    commit = (
        f"tree {tree}\nparent {parent}\nauthor A <a@example.invalid> 1 +0000\n\n"
        f"realm {planted}\n\nThis reverts commit {parent}.\n"
    )
    tag = f"object {tree}\ntype commit\ntag v1\n\nrealm {planted}\n"
    _listing_of(monkeypatch, module, ("a" * 40, "commit", commit.encode()), ("b" * 40, "tag", tag.encode()))

    contents = module.object_contents(["a" * 40, "b" * 40])

    assert module.identifiers_in(contents["a" * 40]) == [planted]
    assert module.identifiers_in(contents["b" * 40]) == [planted]


def test_a_commit_message_and_a_tag_message_are_scanned(tmp_path: Path, monkeypatch) -> None:
    module = _load_scrub_check()
    in_commit, in_tag = "1234567890" + "1234", "1234567890" + "5678"
    repo = _repository_holding("nothing here\n", tmp_path, monkeypatch)
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"realm {in_commit}")
    _git(repo, "tag", "-a", "v1", "-m", f"realm {in_tag}")

    assert {value for _path, value in module.scan_objects()} == {in_commit, in_tag}


def test_a_file_named_for_an_identifier_fails_and_its_name_is_masked(tmp_path: Path, monkeypatch, capsys) -> None:
    module = _load_scrub_check()
    planted = "1234567890" + "1234"
    repo = _repository_holding("nothing here\n", tmp_path, monkeypatch)
    (repo / f"entities_{planted}.yaml").write_text("nothing here\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a telling name")

    assert module.main() == 1
    printed = capsys.readouterr()
    assert "in the working tree" in printed.out
    assert "entities_<digits>.yaml: 1 long digit run(s)" in printed.out
    assert planted not in printed.out + printed.err

    _git(repo, "rm", "-q", f"entities_{planted}.yaml")
    _git(repo, "commit", "-q", "-m", "gone from the tree, not from history")

    assert module.main() == 1
    printed = capsys.readouterr()
    assert "in history" in printed.out
    assert planted not in printed.out + printed.err


def test_a_file_added_only_in_a_merge_is_seen(tmp_path: Path, monkeypatch) -> None:
    module = _load_scrub_check()
    planted = "1234567890" + "1234"
    repo = _repository_holding("nothing here\n", tmp_path, monkeypatch)
    _git(repo, "checkout", "-q", "-b", "side")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "side")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "main")
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "side")
    (repo / f"entities_{planted}.yaml").write_text("nothing here\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a merge that adds a file")

    assert f"entities_{planted}.yaml" in module.history_paths()


def test_a_run_longer_than_any_realm_id_is_still_an_identifier() -> None:
    module = _load_scrub_check()
    assert module.identifiers_in("value " + "1234567890" * 3 + " here")


def test_a_name_in_history_that_is_not_utf8_is_read_and_can_be_printed(tmp_path: Path, monkeypatch) -> None:
    module = _load_scrub_check()
    repo = _repository_holding("nothing here\n", tmp_path, monkeypatch)
    name = os.fsdecode(b"caf\xe9.txt")
    blob = subprocess.run(
        ["git", "rev-parse", "HEAD:ledger.txt"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    _git(repo, "rm", "-q", "--cached", "ledger.txt")
    _git(repo, "commit", "-q", "-m", "a name that isn't text")

    assert name in module.object_paths().values()
    assert module.masked(name) == "caf\\udce9.txt"


def test_a_git_that_fails_is_an_incomplete_scan(tmp_path: Path, monkeypatch, capsys) -> None:
    module = _load_scrub_check()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    assert module.main() == 2
    assert "INCOMPLETE" in capsys.readouterr().err


def test_a_tracked_link_to_a_folder_is_not_a_failed_read(tmp_path: Path, monkeypatch) -> None:
    module = _load_scrub_check()
    repo = _repository_holding("nothing here\n", tmp_path, monkeypatch)
    (repo / "folder").mkdir()
    (repo / "link").symlink_to("folder")
    _git(repo, "add", "link")
    _git(repo, "commit", "-q", "-m", "a link")

    assert module.main() == 0


def test_a_tracked_file_with_a_non_ascii_name_is_read(tmp_path: Path, monkeypatch) -> None:
    """Plain ``git ls-files`` quotes this name, so it would read as deleted."""
    module = _load_scrub_check()
    planted = "1234567890" + "1234"
    repo = _repository_holding("nothing here\n", tmp_path, monkeypatch)
    name = "caf\u00e9.txt"
    (repo / name).write_text("nothing here\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a name git quotes")
    (repo / name).write_text(f"account {planted} here\n", encoding="utf-8")

    assert any(value == planted for _path, value in module.scan_worktree())


def test_a_clean_repository_reports_clean(tmp_path: Path, monkeypatch) -> None:
    """The negative control for the test above: the scanners are not simply
    reporting every file they see."""
    module = _load_scrub_check()

    repo = tmp_path / "clean"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "ledger.txt").write_text("account " + "0" * 15 + "1 here\n", encoding="utf-8")
    _git(repo, "add", "ledger.txt")
    _git(repo, "commit", "-q", "-m", "a placeholder is fine")

    monkeypatch.chdir(repo)
    assert module.scan_worktree() == []
    assert module.scan_objects() == []


def test_there_is_no_history_tolerance_mechanism() -> None:
    """History is held to the same rule as the tree: nothing is tolerated.

    There is deliberately no tolerance list, not even an empty one: an empty
    list is still a list, and it accepts a first entry far more easily than
    absent machinery does. This test fails if the slot appears, so adding a
    tolerance has to be an explicit change here too.
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("scrub_check", "scripts/scrub_check.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["scrub_check"] = module
    spec.loader.exec_module(module)

    assert not hasattr(module, "TOLERATED")
    # Nothing may be filtered out between finding an offender and failing on it.
    source = Path("scripts/scrub_check.py").read_text()
    assert "offenders = scan_objects()" in source
    # And the pass path reports on history too, rather than passing the tree
    # while listing history exceptions underneath it.
    assert "tree and history clean" in source
