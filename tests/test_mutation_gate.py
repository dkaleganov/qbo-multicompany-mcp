"""Keeps the mutation gate honest between runs of the mutation gate.

The gate deletes a safety behaviour and requires the tests to notice. Its own
failure mode is going stale: rename the line an entry matches on and the entry
stops describing anything, the gate finds nothing to mutate, and it prints a
pass. That is the same shape of failure the gate exists to catch, one level up.

So the staleness check runs here, in the ordinary suite, where it costs
milliseconds -- a refactor that moves one of these lines fails in seconds
instead of at release. The gate itself stays a separate script because each
entry runs a whole test family in a subprocess.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_gate():  # noqa: ANN201
    source = REPO_ROOT / "scripts" / "mutation_gate.py"
    spec = importlib.util.spec_from_file_location("mutation_gate", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mutation_gate"] = module
    spec.loader.exec_module(module)
    return module


def test_every_mutation_in_the_gate_still_applies() -> None:
    """Each edit must match its target exactly once.

    A zero match means the gate would run that entry against unchanged source
    and report the behaviour protected. Two matches means it would edit more
    than it names.
    """
    module = load_gate()
    problems = module.check_every_entry_applies(REPO_ROOT)
    assert problems == [], "\n".join(problems)


def test_a_stale_edit_is_an_error_and_never_a_skip() -> None:
    """The control for the test above."""
    module = load_gate()
    stale = module.Mutation(
        id="deliberately-stale",
        behaviour="a behaviour that no longer exists at this line",
        path="src/qbo_multicompany_mcp/guard.py",
        edits=(("a line that is not in guard.py", "something else"),),
        families=("tests/test_guard.py",),
    )
    with pytest.raises(module.StaleEntry, match="matched 0 times"):
        module.mutate("def names_match():\n    return True\n", stale)


def test_the_gate_covers_every_named_safety_behaviour() -> None:
    """The safety behaviours whose deletion the suite must notice, by id.

    Named individually rather than counted, so adding another entry cannot
    quietly stand in for one of these being dropped.
    """
    module = load_gate()
    ids = {mutation.id for mutation in module.MUTATIONS}
    assert {
        "netpolicy-send-enforce",    # send-time read-only enforcement
        "store-flock",               # the cross-process lock
        "store-fsync-file",          # write_atomic contents sync
        "store-fsync-dir",           # write_atomic directory sync
        "redaction-basic",           # Basic-auth redaction
        "redaction-bearer-jwt",      # bearer and JWT redaction
        "scrub-scanners",            # both scrub-check traversals
        "oauth-revoke-noop",         # the revocation request
        "cli-revoke-local-noop",     # the local removal on revoke
    } <= ids


def test_an_edit_that_breaks_the_file_is_an_error_and_never_a_catch() -> None:
    """A mutation that leaves the module unparseable fails its family at
    import. The gate would count that as caught, having tested nothing."""
    module = load_gate()
    broken = module.Mutation(
        id="deliberately-unparseable",
        behaviour="an edit that leaves an empty loop body",
        path="src/qbo_multicompany_mcp/guard.py",
        edits=(("        return True\n", ""),),
        families=("tests/test_guard.py",),
    )
    with pytest.raises(module.StaleEntry, match="unparseable"):
        module.mutate("for x in y:\n    if x:\n        return True\n", broken)


def test_a_server_subprocess_imports_the_tree_under_test(tmp_path: Path) -> None:
    """The gate's import control covers the subprocesses it starts itself. A
    test that starts its own server with a minimal environment and drops
    PYTHONPATH imports the installed package -- the unmutated tree -- so a
    deleted behaviour is checked against source that still has it."""
    import os

    import qbo_multicompany_mcp

    from .conftest import stdio_env

    entities = tmp_path / "entities.yaml"
    entities.write_text("entities: {}\n", encoding="utf-8")
    env = stdio_env(entities_file=entities)
    # Outside the gate the installed package and the tree under test are the
    # same files, so the subprocess check below would pass either way. This is
    # the part that can fail.
    assert env.get("PYTHONPATH") == str(Path(qbo_multicompany_mcp.__file__).resolve().parents[1])
    where = subprocess.run(
        [sys.executable, "-c", "import qbo_multicompany_mcp as m; print(m.__file__)"],
        env=env, capture_output=True, text=True, check=True, cwd=os.sep,
    ).stdout.strip()
    assert Path(where).resolve() == Path(qbo_multicompany_mcp.__file__).resolve()


def test_the_gate_carries_a_control() -> None:
    """A gate with no control cannot tell a clean tree from a broken harness."""
    module = load_gate()
    controls = [mutation for mutation in module.MUTATIONS if mutation.control]
    assert len(controls) == 1
    assert controls[0].families, "the control must run something"


def test_the_gate_is_tracked() -> None:
    """An untracked gate protects this clone and nobody else's."""
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True, cwd=REPO_ROOT
    ).stdout.splitlines()
    assert "scripts/mutation_gate.py" in tracked
    assert "tests/refresh_child.py" in tracked


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def test_the_copy_holds_what_git_lists_and_no_local_configuration(tmp_path: Path) -> None:
    """A local .env and entities.yaml hold credentials and company
    configuration, and the copy is a temporary tree test subprocesses run in.
    Git's ignore rules keep them out, and one tracked by mistake stays out too,
    its staged content included: the copy's repository is a fresh one, never
    the original's git directory. A new file nobody ignores is copied: a
    contributor's new test has to run."""
    module = load_gate()
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text(
        ".env\n.env.*\n!.env.example\nentities.yaml\ntokens.*\n.venv/\n", encoding="utf-8"
    )
    (repo / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".env.example").write_text("QBO_CLIENT_ID=\n", encoding="utf-8")
    (repo / "entities.example.yaml").write_text("entities: {}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    # Assembled here, so the repository's own secret rules don't read this
    # file as holding one.
    local = "=".join(["QBO_CLIENT_SECRET", "not-for-the-copy"]) + "\n"
    (repo / ".env.local").write_text(local, encoding="utf-8")
    _git(repo, "add", "-f", ".env.local")
    (repo / "new_test.py").write_text("def test_new() -> None: ...\n", encoding="utf-8")
    (repo / ".env").write_text(local, encoding="utf-8")
    (repo / "entities.yaml").write_text("entities: {}\n", encoding="utf-8")
    (repo / "tokens.json").write_text("{}\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker").write_text("", encoding="utf-8")

    copy = tmp_path / "copy"
    module.copy_tree(repo, copy)

    present = sorted(
        str(p.relative_to(copy)) for p in copy.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(copy).parts
    )
    assert present == [".env.example", ".gitignore", "entities.example.yaml", "new_test.py", "src/module.py"]
    tracked = subprocess.run(
        ["git", "-C", str(copy), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert tracked == [".env.example", ".gitignore", "entities.example.yaml", "src/module.py"]
    stored = subprocess.run(
        ["git", "-C", str(copy), "cat-file", "--batch-all-objects", "--batch"], capture_output=True, check=True
    ).stdout
    assert b"not-for-the-copy" not in stored, "the staged .env's content came along in the git objects"


def _copy_tracks(copy: Path) -> list[str]:
    listed = subprocess.run(["git", "-C", str(copy), "ls-files", "-z"], capture_output=True, check=True).stdout
    return sorted(path for path in listed.decode("utf-8").split("\0") if path)


def test_a_tracked_file_an_ignore_rule_matches_is_tracked_in_the_copy(tmp_path: Path) -> None:
    """A lock file tracked before a rule ignored it is still tracked. Added by
    name without force, git refuses it and the gate can't start."""
    module = load_gate()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "uv.lock").write_text("lock\n", encoding="utf-8")
    (repo / "module.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    (repo / ".gitignore").write_text("*.lock\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    copy = tmp_path / "copy"
    module.copy_tree(repo, copy)
    assert _copy_tracks(copy) == [".gitignore", "module.py", "uv.lock"]


def test_the_copy_answers_to_no_git_variable_or_user_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GIT_DIR set by a hook or a shell would point the listing and the copy's
    own init and add at another repository. The user's configuration stays
    out of the copy too: its default branch stands in for any setting there."""
    module = load_gate()
    repo, other, home = tmp_path / "repo", tmp_path / "other", tmp_path / "home"
    for folder in (repo, other, home):
        folder.mkdir()
    _git(repo, "init", "-q")
    (repo / "module.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(other, "init", "-q")
    (other / "elsewhere.py").write_text("y = 2\n", encoding="utf-8")
    _git(other, "add", "-A")
    (home / ".gitconfig").write_text("[init]\n\tdefaultBranch = from-the-user\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    copy = tmp_path / "copy"
    module.copy_tree(repo, copy)
    monkeypatch.delenv("GIT_DIR")
    assert sorted(p.name for p in copy.iterdir() if p.name != ".git") == ["module.py"]
    assert _copy_tracks(copy) == ["module.py"]
    assert _copy_tracks(other) == ["elsewhere.py"], "the copy's add went into another repository"
    head = subprocess.run(
        ["git", "-C", str(copy), "symbolic-ref", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head != "refs/heads/from-the-user"


def test_a_tracked_name_is_read_as_a_name(tmp_path: Path) -> None:
    """':notes.md' would read as pathspec magic and '[ab].txt' as a pattern
    that also takes the untracked a.txt."""
    module = load_gate()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name in (":notes.md", "[ab].txt"):
        (repo / name).write_text("tracked\n", encoding="utf-8")
    _git(repo, "--literal-pathspecs", "add", ":notes.md", "[ab].txt")
    (repo / "a.txt").write_text("new\n", encoding="utf-8")
    copy = tmp_path / "copy"
    module.copy_tree(repo, copy)
    assert _copy_tracks(copy) == [":notes.md", "[ab].txt"]
    assert (copy / "a.txt").is_file(), "a new file is copied, untracked"


def _one_file_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "module.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    return repo


def test_a_decomposed_name_is_tracked_in_the_copy_as_listed(tmp_path: Path) -> None:
    """A tree committed on Linux can track a decomposed name, and a macOS clone
    keeps it so. On macOS git init turns precomposing on, and the copy's add
    would then match nothing for it. Git elsewhere never precomposes, so there
    this test can't fail; the index check makes a miss loud everywhere."""
    import unicodedata

    module = load_gate()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    name = unicodedata.normalize("NFD", "café.txt")
    (repo / name).write_text("tracked\n", encoding="utf-8")
    _git(repo, "-c", "core.precomposeunicode=false", "add", "--", name)
    copy = tmp_path / "copy"
    module.copy_tree(repo, copy)
    assert _copy_tracks(copy) == [name]


def test_a_copy_whose_index_differs_from_the_list_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Git can leave a name unmatched and still exit 0. The copy fails instead,
    because its tests would read another tree."""
    module = load_gate()
    repo = _one_file_repo(tmp_path)
    copy = tmp_path / "copy"
    real_run = subprocess.run

    def unmatched(args, *rest, **kwargs):  # noqa: ANN001, ANN202
        result = real_run(args, *rest, **kwargs)
        if "ls-files" in args and str(copy) in args:
            result.stdout = b""
        return result

    monkeypatch.setattr(module.subprocess, "run", unmatched)
    with pytest.raises(OSError, match=r"lacks 1 tracked file"):
        module.copy_tree(repo, copy)


@pytest.mark.parametrize("given", ["GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"])
def test_the_listing_keeps_the_users_own_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: str
) -> None:
    """Configuration a user names with GIT_CONFIG_GLOBAL, or gives through the
    environment or `git -c`, is theirs, not a pointer at another repository: its
    ignore rules still keep a file out. HOME is an empty folder, so no rule of
    the developer's own can keep it out instead."""
    module = load_gate()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    repo = _one_file_repo(tmp_path)
    excludes = tmp_path / "excludes"
    excludes.write_text("*.secret\n", encoding="utf-8")
    if given == "GIT_CONFIG_GLOBAL":
        (tmp_path / "gitconfig").write_text(f'[core]\n\texcludesFile = "{excludes}"\n', encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    elif given == "GIT_CONFIG_COUNT":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.excludesFile")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(excludes))
    else:
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.excludesfile={excludes}'")
    (repo / "local-notes.secret").write_text("not for the copy\n", encoding="utf-8")
    assert module.gate_files(repo) == ["module.py"]


@pytest.mark.parametrize(("said", "refused"), [("git version 2.31.8", True), ("git version 2.32.0 (Apple Git-1)", False)])
def test_a_git_too_old_for_the_copy_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, said: str, refused: bool
) -> None:
    """Git reads GIT_CONFIG_GLOBAL from 2.32 on. An older one would read the
    user's configuration into the copy, so the gate stops instead."""
    module = load_gate()
    repo = _one_file_repo(tmp_path)
    monkeypatch.setattr(module, "_git_version", lambda: said)
    if refused:
        with pytest.raises(OSError, match=r"needs git 2\.32 or later, and this is git version 2\.31\.8"):
            module.copy_tree(repo, tmp_path / "copy")
    else:
        module.copy_tree(repo, tmp_path / "copy")
        assert _copy_tracks(tmp_path / "copy") == ["module.py"]


def test_family_runs_get_no_variable_naming_a_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The families' git commands run in the copy, and a GIT_DIR from a hook
    would point them back at the original. The user's own configuration stays."""
    module = load_gate()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "index"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    env = module._child_env(tmp_path)
    assert "GIT_DIR" not in env and "GIT_INDEX_FILE" not in env
    assert env["GIT_CONFIG_GLOBAL"] == str(tmp_path / "gitconfig")
    assert env["PYTHONPATH"] == str(tmp_path / "src")


def test_the_suite_drops_variables_naming_another_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from a git hook, a test's git add would stage into the hook's index.
    Configuration given through the environment stays."""
    from . import conftest

    monkeypatch.setenv("GIT_DIR", "/nonexistent/elsewhere")
    monkeypatch.setenv("GIT_INDEX_FILE", "/nonexistent/index")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.abbrev=12'")
    conftest.pytest_configure(None)  # type: ignore[arg-type]
    assert "GIT_DIR" not in os.environ and "GIT_INDEX_FILE" not in os.environ
    assert os.environ.get("GIT_CONFIG_COUNT") == "0", "configuration given through the environment was dropped"
    assert os.environ.get("GIT_CONFIG_PARAMETERS") == "'core.abbrev=12'", "git -c configuration was dropped"


def test_no_entry_runs_the_staleness_test() -> None:
    """Almost every entry's edit removes its own text from its own file, so the
    staleness test fails under it, whatever file it targets. A family holding it
    would report the entry caught, whatever its behaviour tests did."""
    module = load_gate()
    gate_tests = os.path.normpath(module.GATE_TESTS)
    for mutation in module.MUTATIONS:
        for family in mutation.families:
            path, _, node = family.partition("::")
            path = os.path.normpath(path)
            whole = not node and path in (gate_tests, "tests", ".")
            staleness = path == gate_tests and node.split("[")[0] == "test_every_mutation_in_the_gate_still_applies"
            assert not (whole or staleness), f"{mutation.id} runs {family}"


def test_the_control_runs_first_and_a_miss_stops_the_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Selected or not, the control runs before any entry. When it isn't
    caught, no other entry runs: a harness that can't catch it can't vouch for
    anything after it."""
    module = load_gate()
    target = "pkg/mod.py"
    control = module.Mutation(
        id="the-control", behaviour="b", path=target, edits=(("x = 1\n", "x = 2\n"),),
        families=("tests/c.py",), control=True,
    )
    other = module.Mutation(
        id="the-other", behaviour="b", path=target, edits=(("x = 1\n", "x = 3\n"),),
        families=("tests/o.py",),
    )
    monkeypatch.setattr(module, "MUTATIONS", [other, control])
    monkeypatch.setattr(module, "check_every_entry_applies", lambda: [])

    def fake_copy(root: Path, copy: Path) -> None:
        (copy / "pkg").mkdir(parents=True)
        (copy / target).write_text("x = 1\n", encoding="utf-8")

    ran: list[tuple[str, ...]] = []

    def passing(copy: Path, families: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        ran.append(tuple(families))
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="1 passed in 0.01s\n", stderr="")

    monkeypatch.setattr(module, "copy_tree", fake_copy)
    monkeypatch.setattr(module, "import_control_passes", lambda copy: (True, "inside the copy"))
    monkeypatch.setattr(module, "collection_control_passes", lambda copy: (True, "infrastructure"))
    monkeypatch.setattr(module, "run_families", passing)
    monkeypatch.setattr(sys, "argv", ["mutation_gate.py", "--only", "the-other"])

    assert module.main() == 2
    out = capsys.readouterr().out
    assert ran[0] == ("tests/c.py", "tests/o.py"), "the baseline covers every family"
    assert ran[1:] == [("tests/c.py",)], "an entry ran after the control was missed"
    assert "THE CONTROL DID NOT FIRE" in out
    assert "the-other" not in out


# --------------------------------------------------------------------------
# "Caught" means a test failed, not that pytest exited nonzero
# --------------------------------------------------------------------------


def _pytest_on(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    probe = tmp_path / "test_probe.py"
    probe.write_text(body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(probe)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )


def test_a_failing_test_is_caught(tmp_path: Path) -> None:
    module = load_gate()
    result = _pytest_on(tmp_path, "def test_x() -> None:\n    assert False\n")
    assert module.classify(result) == module.CAUGHT


def test_a_passing_family_is_a_survivor(tmp_path: Path) -> None:
    module = load_gate()
    result = _pytest_on(tmp_path, "def test_x() -> None:\n    assert True\n")
    assert module.classify(result) == module.SURVIVED


def test_a_collection_error_is_an_infrastructure_failure_never_a_catch(tmp_path: Path) -> None:
    """pytest exits 2 when an import fails at collection, so counting every
    nonzero exit as detection credits a run that tested nothing."""
    module = load_gate()
    result = _pytest_on(
        tmp_path,
        "import qbo_module_that_does_not_exist\n\ndef test_x() -> None:\n    assert False\n",
    )
    assert result.returncode != 0, "the planted error did not fail the run at all"
    assert module.classify(result) == module.INFRASTRUCTURE


def test_a_failure_beside_an_error_is_not_a_clean_catch(tmp_path: Path) -> None:
    """One test fails and another errors in setup: the failure may be the
    error's shadow, so the run is not trusted as detection."""
    module = load_gate()
    result = _pytest_on(
        tmp_path,
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken():\n    raise RuntimeError('setup')\n"
        "def test_a() -> None:\n    assert False\n"
        "def test_b(broken) -> None:\n    assert True\n",
    )
    assert module.classify(result) == module.INFRASTRUCTURE


def test_nothing_collected_is_an_infrastructure_failure(tmp_path: Path) -> None:
    module = load_gate()
    result = _pytest_on(tmp_path, "def helper() -> None:\n    pass\n")
    assert result.returncode == 5
    assert module.classify(result) == module.INFRASTRUCTURE


def test_a_signal_is_an_infrastructure_failure() -> None:
    module = load_gate()
    killed = subprocess.CompletedProcess(args=[], returncode=-9, stdout="", stderr="")
    assert module.classify(killed) == module.INFRASTRUCTURE


def test_the_gate_plants_a_collection_error_as_its_own_control(tmp_path: Path) -> None:
    """The control runs inside the gate, against the copy, before any entry."""
    module = load_gate()
    copy = tmp_path / "tree"
    module.copy_tree(REPO_ROOT, copy)
    fires, detail = module.collection_control_passes(copy)
    assert fires, detail
    assert not (copy / "tests" / "test_zz_planted_collection_error.py").exists()
    source = (REPO_ROOT / "scripts" / "mutation_gate.py").read_text(encoding="utf-8")
    assert "collection_control_passes(copy)" in source.split("def main")[1]


# --------------------------------------------------------------------------
# Every subprocess a test starts imports the tree under test
# --------------------------------------------------------------------------


def _probe_module_path(env: dict[str, str]) -> Path:
    import os

    where = subprocess.run(
        [sys.executable, "-c", "import qbo_multicompany_mcp.config as c; print(c.__file__)"],
        env=env, capture_output=True, text=True, check=True, cwd=os.sep,
    ).stdout.strip()
    return Path(where).resolve()


def _environments(tmp_path: Path) -> dict[str, dict[str, str]]:
    import os

    from .conftest import stdio_env, with_source_root
    from .test_registry_and_startup import base_env
    from .test_token_store import refresh_child_env

    entities = tmp_path / "entities.yaml"
    entities.write_text("entities: {}\n", encoding="utf-8")
    os.environ.setdefault("QBO_TOKEN_KEY", "not-a-real-key")
    return {
        "stdio_env": stdio_env(entities_file=entities),
        "registry base_env": base_env(),
        "refresh child": refresh_child_env(),
        "the whole environment": with_source_root(
            {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        ),
    }


def test_every_subprocess_environment_imports_the_active_source_root(tmp_path: Path) -> None:
    """Each environment builder the tests use, checked by asking a subprocess
    where it imported the package from. The repository root alone is not
    enough in this src layout: a subprocess given only that imports the
    installed package while the gate mutates a copy."""
    from .conftest import source_root

    for name, env in _environments(tmp_path).items():
        assert env["PYTHONPATH"].split(":")[0] == str(source_root()), name
        assert _probe_module_path(env).is_relative_to(source_root()), (name, env["PYTHONPATH"])


def test_the_repository_root_alone_does_not_reach_the_package(tmp_path: Path) -> None:
    """The control: the repository root with no source root. A subprocess
    then finds the package only if one is installed, and then it is not the
    tree under test. Run against a scratch copy so the installed package and
    the tree differ, as they do under the gate."""
    from .conftest import with_source_root

    copy = tmp_path / "tree"
    module = load_gate()
    module.copy_tree(REPO_ROOT, copy)
    pretend_active = str(copy / "src")
    without = {"PATH": __import__("os").environ.get("PATH", ""), "PYTHONPATH": str(copy)}
    assert not _probe_module_path(without).is_relative_to(copy)
    corrected = {**without, "PYTHONPATH": pretend_active + ":" + str(copy)}
    assert _probe_module_path(corrected).is_relative_to(copy)
    assert with_source_root({"PYTHONPATH": str(copy)})["PYTHONPATH"].endswith(":" + str(copy))
