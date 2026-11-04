#!/usr/bin/env python3
"""Delete a safety behaviour, and require the test suite to notice.

A passing test suite says nothing about whether a safety behaviour is protected:
a behaviour can be removed with every test still reporting green. That matters
most for send-time read-only enforcement, the package's central claim, and the
cross-process file lock, which stops two processes destroying a refresh chain.
Finding those by hand does not scale.

So this is the mechanical version. Each entry below names a behaviour, the
exact edit that removes it, and the test family that is supposed to be
protecting it. The gate applies the edit to a throwaway copy of the tree, runs
that family, and **fails if the family still passes**. A test that has never
been observed failing is not evidence of anything.

Controls, because a gate that cannot fail is the problem it exists to catch:

*The import control.* The copy is only useful if the subprocess imports from
it. A gate that silently imported the unmutated source would report every
behaviour protected, forever. So before anything else, a subprocess is asked
where it imports the package from, and the answer must be inside the copy.

*The mutation control.* One entry is marked ``control``: a behaviour whose
detectors are known to be alive (the guard's exact name comparison). It runs
first, whatever was selected. If it is NOT caught, the harness is broken rather
than the tree being clean, and the gate exits 2 before any other entry runs.

*The collection control.* "Caught" means a test FAILED. It does not mean
pytest exited nonzero: a collection error, an internal error, a usage error,
nothing collected and a signal all exit nonzero having tested nothing. The
verdict is read from pytest's own summary --
at least one ``failed`` and no ``error`` -- and anything else is an
infrastructure failure that fails the run. Before any entry runs, a test
file whose import fails at collection is planted and must be classified as
infrastructure, never as caught.

*What is copied.* The files git lists, tracked or new and not ignored, into a
fresh repository with one commit of the tracked ones, which is all the hygiene
tests read. A local ``.env``, ``entities.yaml`` or token file never goes in,
even one tracked by mistake, and nor does the original's git directory, which
keeps whatever was ever staged: the copy is a temporary tree that test
subprocesses run in, and credentials have no business there.

Staleness is the third failure mode, and the quiet one. If a refactor renames
the line an entry matches on, the entry stops describing anything and the gate
becomes a no-op that prints a pass. So every edit must match exactly once, and
a miss is an error rather than a skip. ``tests/test_mutation_gate.py`` makes
that check part of the ordinary suite, so the gate going stale fails in
seconds rather than at release.

Usage::

    python scripts/mutation_gate.py            # every entry
    python scripts/mutation_gate.py --list
    python scripts/mutation_gate.py --only store-flock

Exit codes: 0 every behaviour is protected, 1 at least one is not, 2 the gate
itself could not be trusted (stale entry, or a control that did not fire).
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SRC = "src/qbo_multicompany_mcp"
#: The gate's own tests. An entry whose family is here names test nodes, never
#: the whole file: the staleness test fails under almost every entry's own edit,
#: so the whole file would report nearly any entry caught.
GATE_TESTS = "tests/test_mutation_gate.py"

#: Local configuration and credentials, by file name, as ``.gitignore`` names
#: them. Never copied, even if tracked by mistake.
_LOCAL_CONFIG = re.compile(r"\.env(\..+)?|entities(\..+)?\.yaml|tokens\..+|.+\.enc")
_EXAMPLES = frozenset({".env.example", "entities.example.yaml"})


#: Every git command run on the copy: an identity of its own, none of the
#: user's signing or hooks, and names kept as listed. On macOS ``git init``
#: turns precomposing on, and a decomposed name read from the list would then
#: match nothing on disk.
_COPY_GIT = (
    "-c", "user.name=mutation-gate", "-c", "user.email=mutation-gate@example.invalid",
    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
    "-c", "core.precomposeunicode=false",
)

#: The oldest git the copy can trust: ``GIT_CONFIG_GLOBAL`` is read from 2.32
#: on, and ``add --pathspec-from-file`` from 2.26.
_GIT_FLOOR = (2, 32)


def _git_version() -> str:
    return subprocess.run(["git", "version"], capture_output=True, text=True, check=True).stdout.strip()


def _require_git() -> None:
    said = _git_version()
    found = re.search(r"(\d+)\.(\d+)", said)
    if found is None or (int(found[1]), int(found[2])) < _GIT_FLOOR:
        raise OSError(f"the copy needs git {_GIT_FLOOR[0]}.{_GIT_FLOOR[1]} or later, and this is {said or 'unknown'}")


#: Of git's local variables, the ones that carry configuration rather than
#: name a repository: ``git -c`` settings and configuration given through the
#: environment. They are the user's own, like ``GIT_CONFIG_GLOBAL``.
_CONFIGURATION_VARIABLES = frozenset({"GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"})


@functools.cache
def _repository_variables() -> frozenset[str]:
    """The variables that point git at one repository.

    ``git rev-parse --local-env-vars`` lists git's local variables, ``GIT_DIR``,
    ``GIT_WORK_TREE`` and ``GIT_INDEX_FILE`` among them, before it looks for a
    repository, so a bad one in the environment can't stop it. Those that carry
    configuration are left out.
    """
    listed = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True)
    return frozenset(listed.stdout.split()) - _CONFIGURATION_VARIABLES


def _git_environment(*, own_config: bool) -> dict[str, str]:
    """This process's environment, minus what would point git elsewhere.

    A variable naming a repository always goes: a ``GIT_DIR`` from a hook or a
    shell would point a command at some other repository. With ``own_config``
    the user's own configuration stays, a ``GIT_CONFIG_GLOBAL`` included, so
    their ignore rules apply when the original's files are listed. For the
    copy every ``GIT_*`` variable goes, and the user's and the system's
    configuration with them, so no setting there changes what it holds.
    """
    if own_config:
        local = _repository_variables()
        return {name: value for name, value in os.environ.items() if name not in local}
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _listed(root: Path, *which: str) -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", *which],
        capture_output=True, check=True, env=_git_environment(own_config=True),
    ).stdout.decode("utf-8", "surrogateescape")
    return [path for path in listed.split("\0") if path]


def _not_local_configuration(path: str) -> bool:
    name = Path(path).name
    return name in _EXAMPLES or not _LOCAL_CONFIG.fullmatch(name)


def gate_files(root: Path) -> list[str]:
    """What the copy holds: the files git lists, tracked or new, never ignored.

    Copying the folder whole would carry a local ``.env`` and ``entities.yaml``
    into the copy, and with them credentials and company configuration. Git's
    ignore rules keep those out, along with virtualenvs and caches, and local
    configuration stays out even when it is tracked.
    """
    listed = _listed(root, "--cached", "--others", "--exclude-standard")
    return sorted(path for path in set(listed) if _not_local_configuration(path))


def copy_tree(root: Path, copy: Path) -> None:
    """The listed files, in a fresh repository with one commit of the tracked ones.

    The original's git directory stays behind. It keeps every file ever staged,
    a ``.env`` added by mistake included, and the repository's own settings;
    the hygiene tests need only a repository whose tracked files are these.
    """
    _require_git()
    copy.mkdir(parents=True, exist_ok=True)
    for relative in gate_files(root):
        source, target = root / relative, copy / relative
        if not (source.is_file() or source.is_symlink()):
            continue  # tracked, but deleted in the working tree
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, target)
    tracked = [
        path for path in _listed(root, "--cached")
        if _not_local_configuration(path) and ((copy / path).is_file() or (copy / path).is_symlink())
    ]

    def git(*args: str, names: bytes | None = None) -> bytes:
        return subprocess.run(
            ["git", "-C", str(copy), *_COPY_GIT, *args], input=names,
            capture_output=True, check=True, env=_git_environment(own_config=False),
        ).stdout

    git("init", "-q")
    # By name, read literally, and forced: a tracked file an ignore rule matches
    # is still tracked, and a name like ':notes.md' or '[ab].txt' is a name.
    git(
        "--literal-pathspecs", "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",
        names="\0".join(tracked).encode("utf-8", "surrogateescape"),
    )
    # Git can leave a name unmatched without an error. A copy tracking anything
    # else would give the hygiene tests another tree to read, so it fails here.
    staged = {path for path in git("ls-files", "-z").decode("utf-8", "surrogateescape").split("\0") if path}
    if staged != set(tracked):
        missing, other = sorted(set(tracked) - staged), sorted(staged - set(tracked))
        raise OSError(
            f"the copy's index lacks {len(missing)} tracked file(s) and holds {len(other)} other(s), so "
            f"its tests would read another tree; a file system that folds case or decomposes names can "
            f"do this: {', '.join(ascii(path) for path in (missing + other)[:3])}"
        )
    git("commit", "-q", "--no-verify", "--allow-empty", "-m", "mutation-gate copy")


@dataclass(frozen=True)
class Mutation:
    """One safety behaviour, and the edit that deletes it."""

    id: str
    #: What stops working, in the terms a reader would use to argue it matters.
    behaviour: str
    path: str
    #: ``(old, new)`` pairs. Each ``old`` must appear exactly once.
    edits: tuple[tuple[str, str], ...]
    #: The test families that claim to protect this behaviour.
    families: tuple[str, ...]
    control: bool = False


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        id="netpolicy-send-enforce",
        behaviour=(
            "send-time read-only enforcement: the per-request check that makes "
            "'read-only' a property of this code rather than of the scope"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                "    enforce(request, ctx)\n    response = client.send(",
                "    response = client.send(",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="store-flock",
        behaviour=(
            "the cross-process file lock: without it two processes refresh one "
            "realm at once and one of the two issued token pairs is lost"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "                    break",
                "                    break",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="store-fsync-file",
        behaviour=(
            "the contents fsync in write_atomic: without it a crash can publish "
            "a directory entry pointing at bytes that never reached the disk"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            os.fsync(fd)\n        finally:\n            os.close(fd)",
                "        finally:\n            os.close(fd)",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="store-fsync-dir",
        behaviour=(
            "the directory fsync in write_atomic: without it the rename itself "
            "is not durable and a crash can leave the old inode in place"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "        os.fsync(dir_fd)\n    finally:\n        os.close(dir_fd)",
                "        pass\n    finally:\n        os.close(dir_fd)",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="redaction-basic",
        behaviour=(
            "Basic-auth redaction: the header decodes straight back to the "
            "client id and secret"
        ),
        path=f"{SRC}/redaction.py",
        edits=((r'''    (re.compile(r"(?i)\b(basic)\s+\S+"), rf"\g<1> {REDACTED}"),
''', ""),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="redaction-bearer-jwt",
        behaviour="bearer and JWT redaction: access tokens in any log line or traceback",
        path=f"{SRC}/redaction.py",
        edits=(
            (r'''    (re.compile(r"(?i)\b(bearer)\s+\S+"), rf"\g<1> {REDACTED}"),
''', ""),
            (r'''    (re.compile(r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{8,}+(?:\.[A-Za-z0-9_-]*){2,4}"), REDACTED),
''', ""),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="scrub-scanners",
        behaviour=(
            "both scrub-check traversals: the scan reports zero because it "
            "opened nothing, which is indistinguishable from a clean tree"
        ),
        path="scripts/scrub_check.py",
        edits=(
            (
                '    """Every blob, commit and tag reachable from a ref, and every path."""\n    paths = object_paths()',
                '    """Every blob, commit and tag reachable from a ref, and every path."""\n    return []\n    paths = object_paths()',
            ),
            (
                '    """Tracked files as they stand now."""\n    # -z',
                '    """Tracked files as they stand now."""\n'
                "    return []\n    # -z",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="oauth-revoke-noop",
        behaviour="the revocation request itself: revoke reports success having sent nothing",
        path=f"{SRC}/oauth.py",
        edits=(
            (
                '    """Revoke a refresh token. CLI only."""\n    response = _post(',
                '    """Revoke a refresh token. CLI only."""\n    return None\n    response = _post(',
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="cli-revoke-local-noop",
        behaviour=(
            "the local removal in revoke: the CLI says the credential is gone "
            "while the row, and the usable token in it, stay on disk"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                "            removed = store.revoke_row(entity.realm_id)\n"
                "        except QboMcpError as exc:\n",
                "            removed = None\n"
                "        except QboMcpError as exc:\n",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="crypto-hex-key-shape",
        behaviour=(
            "deciding the key encoding by the string's shape: without it every "
            "64-character hex key decodes as base64 to 48 bytes and the "
            "documented hex option rejects every valid key"
        ),
        path=f"{SRC}/crypto.py",
        edits=(
            (
                "    if _HEX_KEY.match(text):\n"
                "        key = _from_hex(text)\n"
                "    else:\n"
                "        key = _from_base64(text)\n"
                "        if key is None:\n"
                "            key = _from_hex(text)\n",
                "    key = _from_base64(text)\n"
                "    if key is None:\n"
                "        key = _from_hex(text)\n",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="accounts-walk-dedup",
        behaviour=(
            "list_accounts reporting the walker's own deduplication and cap: "
            "with literal values in their place, a row repeated across a moving page "
            "boundary is a complete list with zero duplicates, and these rows "
            "carry balances"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                "                    truncated=walked.truncated,\n"
                "                    duplicates_dropped=walked.duplicates_dropped,\n",
                "                    truncated=False,\n"
                "                    duplicates_dropped=0,\n",
            ),
        ),
        families=("tests/test_pagination_and_limits.py",),
    ),
    Mutation(
        id="walk-count-anchor",
        behaviour=(
            "the count anchor: without it a walk that lost a page is "
            "indistinguishable from one that read every row, and every list "
            "tool in the package goes through here"
        ),
        path=f"{SRC}/pagination.py",
        edits=(
            (
                "    if len(seen) != anchor:\n        raise WalkIncomplete(",
                "    if False:\n        raise WalkIncomplete(",
            ),
        ),
        families=("tests/test_pagination_and_limits.py",),
    ),
    Mutation(
        id="store-usable-row-binding",
        behaviour=(
            "the configuration binding checked before every credential use: "
            "without it a still-valid cached token is sent under a store "
            "belonging to another app, environment or entity"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "        row = self._row_of(self.read(), realm_id)\n"
                "        if row is not None:\n"
                "            self._assert_configuration(row, expected_entity=entity_key)\n"
                "        return row",
                "        return self._row_of(self.read(), realm_id)",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="store-entity-binding-leg",
        behaviour=(
            "the entity leg of the entity/environment/realm tuple"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "        if expected_entity is not None and row.entity != expected_entity:\n"
                "            raise ConfigurationError("
                'config_mismatch_text(expected_entity, "entity"))\n',
                "",
            ),
        ),
        families=("tests/test_token_store.py", "tests/test_keepalive.py"),
    ),
    Mutation(
        id="keepalive-entity-binding",
        behaviour=(
            "the keepalive naming the registry entry it refreshes for: without "
            "it a row bound to another entity has its refresh token submitted"
        ),
        path=f"{SRC}/keepalive.py",
        edits=(
            (
                '    kwargs = {"actor": actor, "force": force, "entity_key": entity_key}',
                '    kwargs = {"actor": actor, "force": force}',
            ),
        ),
        families=("tests/test_keepalive.py",),
    ),
    Mutation(
        id="netpolicy-synthetic-credential",
        behaviour=(
            "the synthetic-credential condition on custom and loopback bases: "
            "without it a real stored token is sent to any origin the flag, or "
            "loopback, admits"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                "    if not _base_is_intuit(base):\n",
                "    if False:\n",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="netpolicy-refresh-body",
        behaviour=(
            "the body half of the refresh policy: without it a refresh-class "
            "POST to the pinned endpoint may carry any grant"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                "    _token_post(request, ctx, cli_only=False)\n"
                "    _check_form_body(\n"
                "        request,\n"
                '        grant_type="refresh_token",\n'
                '        required=frozenset({"grant_type", "refresh_token"}),\n'
                "    )\n",
                "    _token_post(request, ctx, cli_only=False)\n",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="netpolicy-revoke-body",
        behaviour="the revocation body check: anything may ride along with the token",
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                "    _check_token_headers(request, opt_in=False)\n"
                "    _check_revoke_body(request)\n",
                "    _check_token_headers(request, opt_in=False)\n",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="gitleaks-code-reference-narrowing",
        behaviour=(
            "the narrowing of the code-reference allowlist: widened back to "
            "any lowercase run, it exempts a whole credential shape and a "
            "generated lowercase secret produces no finding"
        ),
        path=".gitleaks.toml",
        edits=(
            (
                'regexTarget = "match"\n'
                "regexes = [\n"
                "  '''[:=>]\\s*[a-z_][a-z0-9_]{0,40}(\\.[a-z_][a-z0-9_]{0,40}){0,4}"
                "\\.(client_id|client_secret|refresh_token|access_token)$''',\n"
                "]\n",
                'regexTarget = "secret"\n'
                "regexes = [\n"
                "  '''^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$''',\n"
                "]\n",
            ),
        ),
        families=("tests/test_secret_scanning.py",),
    ),
    Mutation(
        id="gitleaks-fixture-credentials-listed-whole",
        behaviour=(
            "anchoring a fixture credential at both ends: anchored at the start only, "
            "it forgives a real token that begins with the fixture, in any file"
        ),
        path=".gitleaks.toml",
        edits=(
            (
                "  '''^eyJhbGciOiJIUzI1NiJ9\\.(payloadpayload\\.signature|"
                "cGF5bG9hZA\\.c2lnbmF0dXJl)$''',\n",
                "  '''^eyJhbGciOiJIUzI1NiJ9\\.(payloadpayload\\.signature|"
                "cGF5bG9hZA\\.c2lnbmF0dXJl)''',\n",
            ),
        ),
        families=("tests/test_secret_scanning.py", "tests/test_repo_hygiene.py"),
    ),
    Mutation(
        id="gitleaks-placeholder-realm-short-tail",
        behaviour=(
            "ending the placeholder-realm allowlist at its short tail: unanchored, it "
            "forgives any realm id that begins with eight zeros"
        ),
        path=".gitleaks.toml",
        edits=(
            (
                "0{8,}[0-9]{0,4}[\"']?$''',\n",
                "0{8,}[0-9]{0,4}[\"']?''',\n",
            ),
        ),
        families=("tests/test_secret_scanning.py",),
    ),
    Mutation(
        id="gitleaks-placeholder-realm-in-url-short-tail",
        behaviour=(
            "ending the placeholder-realm allowlist for API paths at its short tail: "
            "unanchored, it forgives any realm in a path that begins with eight zeros"
        ),
        path=".gitleaks.toml",
        edits=(
            (
                "  '''/v3/company/0{8,}[0-9]{0,4}$''',\n",
                "  '''/v3/company/0{8,}[0-9]{0,4}''',\n",
            ),
        ),
        families=("tests/test_secret_scanning.py",),
    ),
    Mutation(
        id="redaction-unquoted-separator",
        behaviour=(
            "the unquoted `name: value` redaction the module documents: without "
            "it a short or punctuated secret after a colon is printed as-is"
        ),
        path=f"{SRC}/redaction.py",
        edits=(('            rf"(?i)(\\b(?:{_KEY_ALTERNATION})\\b[\\"\']?[ \\t]*[:=][ \\t]*)"\n', '            rf"(?i)(\\b(?:{_KEY_ALTERNATION})\\b[\\"\']?=)"\n'),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="redaction-scheme-word-lookahead",
        behaviour=(
            "leaving the auth scheme word to its own rule: without it the colon "
            "rule redacts `Bearer` and the token after it survives"
        ),
        path=f"{SRC}/redaction.py",
        edits=(('            rf"(?![\\"\'])(?!(?:bearer|basic)\\b)[^&\\s\\"\']+"\n', '            rf"(?![\\"\'])[^&\\s\\"\']+"\n'),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="redaction-env-names",
        behaviour=(
            "this package's own environment names for its secrets: without "
            "them `QBO_CLIENT_SECRET=...` passes the word-boundary key rules"
        ),
        path=f"{SRC}/redaction.py",
        edits=(('    "qbo_client_secret",\n    "qbo_import_refresh_token",\n', ""),),
        families=("tests/test_error_boundary.py",),
    ),
    # -- credentials, faults and report columns -------------------------------
    Mutation(
        id="live-values-redaction",
        behaviour=(
            "exact-value redaction: a credential with no recognisable shape is "
            "printed as-is in any log line, error or traceback"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                "        for value in live_values.snapshot():\n"
                "            if value in text:\n"
                "                text = text.replace(value, REDACTED)\n",
                "",
            ),
        ),
        families=("tests/test_live_values.py",),
    ),
    Mutation(
        id="live-values-client-secret",
        behaviour="registering the client secret the moment it is loaded",
        path=f"{SRC}/store.py",
        edits=(("        live_values.register(self.client_secret)\n", "        pass\n"),),
        families=("tests/test_live_values.py",),
    ),
    Mutation(
        id="live-values-stored-tokens",
        behaviour=(
            "registering every token read from the store: a restarted process "
            "neither redacts nor fences the pair it loaded from disk"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "        if not self.synthetic:\n"
                "            live_values.register(self.access_token, self.refresh_token)\n\n"
                "    # -- derived",
                "        pass\n\n    # -- derived",
            ),
        ),
        families=("tests/test_live_values.py", "tests/test_network_policy.py"),
    ),
    Mutation(
        id="live-values-no-synthetic-in-src",
        behaviour=(
            "the rule that no source file marks a pair synthetic: planted here, "
            "every real token Intuit issues would skip the registry"
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            (
                "            received_at + timedelta(seconds=hard_ttl) if hard_ttl > 0 else None\n"
                "        ),\n    )",
                "            received_at + timedelta(seconds=hard_ttl) if hard_ttl > 0 else None\n"
                "        ),\n        synthetic=True,\n    )",
            ),
        ),
        families=("tests/test_live_values.py",),
    ),
    Mutation(
        id="netpolicy-production-leg",
        behaviour=(
            "first leg of the test-only-base rule: production refusing a test-only base whatever the "
            "credential looks like"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(("        _refuse_in_production(ctx)\n", ""),),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="netpolicy-identity-leg",
        behaviour=(
            "second leg of the test-only-base rule: a credential the store holds never reaching a "
            "test-only base, matched by identity -- without it a UUID-shaped "
            "token passes the shape rule and is sent"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(("        _refuse_held_credentials(request)\n", ""),),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="startup-production-api-base",
        behaviour="refusing a test-only --api-base in production at startup",
        path=f"{SRC}/netpolicy.py",
        edits=(("    if environment == ENV_PRODUCTION:\n        raise StartupError(", "    if False:\n        raise StartupError("),),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="store-no-app-no-credential",
        behaviour=(
            "with no app configured, no stored credential is released "
            "for sending -- without it a cached token from any app's row goes out"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "        if self.credentials is None:\n"
                "            raise ConfigurationError(NO_APP_CREDENTIALS_TEXT)\n"
                "        row = self._row_of(self.read(), realm_id)\n",
                "        row = self._row_of(self.read(), realm_id)\n",
            ),
        ),
        families=("tests/test_app_credentials_required.py",),
    ),
    Mutation(
        id="startup-no-app-warning",
        behaviour="the startup warning that credential use is disabled",
        path=f"{SRC}/server.py",
        edits=(
            (
                '        print(f"qbo-multicompany-mcp: {NO_APP_CREDENTIALS_STARTUP_TEXT}", file=sys.stderr)\n',
                "        pass\n",
            ),
        ),
        families=("tests/test_app_credentials_required.py",),
    ),
    Mutation(
        id="client-sends-from-get-row",
        behaviour=(
            "the rule that nothing which sends reads its credential through "
            "get_row: planted here, the configuration binding is skipped"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                "        row = self.store.usable_row(entity.realm_id, entity_key=entity.key)\n"
                "        if row is None:\n            raise NeedsConsentError",
                "        row = self.store.get_row(entity.realm_id)\n"
                "        if row is None:\n            raise NeedsConsentError",
            ),
        ),
        families=("tests/test_ast_regression.py",),
    ),
    Mutation(
        id="fault-code-table",
        behaviour=(
            "the fault-code table: without it 5030 and 6190 read the same, and "
            "the accounts-payable detection has nothing to key on"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                "            known = next((code for code in codes if code in FAULT_HINTS), None)\n",
                "            known = None\n",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="fault-inside-a-200",
        behaviour=(
            "failing on a fault inside a 200: parsed as a payload it has no "
            "QueryResponse, which a walk reads as zero rows"
        ),
        path=f"{SRC}/client.py",
        edits=(("        if status != 200 or codes:\n", "        if status != 200:\n"),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="no-echo-orderby",
        behaviour="never echoing a caller's ORDERBY field into an error",
        path=f"{SRC}/query_grammar.py",
        edits=(
            (
                '            f"the ORDERBY field is not sortable on {spec.name}. Sortable: "',
                '            f"\'{token.value}\' is not sortable on {spec.name}. Sortable: "',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="no-echo-change-type",
        behaviour="never echoing a caller's change type into an error",
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                '                f"types[{position}] is not a supported change type. Supported: "',
                '                f"\'{name}\' is not a supported change type. Supported: "',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="cli-realm-last-four",
        behaviour=(
            "printing only the last four of an approved realm that does not "
            "match: the full id went to whatever the terminal was logged to"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                '        out(f"  approved realm:    ...{params.realm_id[-4:]}")',
                '        out(f"  approved realm:    {params.realm_id}")',
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="report-malformed-column-fails",
        behaviour=(
            "failing on a malformed column definition: skipped instead, every "
            "later cell lands one column left and a private value can come out "
            "under a money key"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                # The check lives in parse_columns' read_definition; without it a
                # definition that is not an object becomes a keyless leaf
                # instead of failing.
                '            raise _structural(f"column definition {where} is not an object")\n',
                "            return {}, None, None\n",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-malformed-amount-disclosed",
        behaviour="listing a malformed amount: without it the amount is a silent null",
        path=f"{SRC}/reports.py",
        edits=(
            (
                "        if amount is None:\n"
                "            anomalies.append(\n"
                "                ReportAnomaly(\n"
                "                    row_id, column.key, ANOMALY_MALFORMED_AMOUNT,\n"
                "                    _MALFORMED_AMOUNT_DETAIL if column.is_money else _MALFORMED_QUANTITY_DETAIL,\n"
                "                )\n"
                "            )\n",
                "",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-cell-count-checked",
        behaviour=(
            "checking a row's cell count against its columns: short rows are "
            "truncated and surplus cells dropped, silently"
        ),
        path=f"{SRC}/reports.py",
        edits=(("    if upstream_count != expected:\n", "    if False:\n"),),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-amount-shape",
        behaviour=(
            "reading an amount only in the shape a report writes one: without "
            "it NaN, Infinity and exponents become amounts"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                '    if not candidate or candidate in ("-", ".", "-.") or not _AMOUNT_RE.match(candidate):\n',
                '    if not candidate or candidate in ("-", ".", "-."):\n',
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-anomaly-not-complete",
        behaviour="never reporting 'complete' while an anomaly exists",
        path=f"{SRC}/reports.py",
        edits=(("    if client_truncated or anomalous:\n", "    if client_truncated:\n"),),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-stated-totals-flagged",
        behaviour=(
            "flagging a QuickBooks-stated total the returned cells cannot "
            "reproduce"
        ),
        path=f"{SRC}/reports.py",
        edits=(("    anomalies.extend(_stated_total_anomalies(rows, anomalies, kept))\n", ""),),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-computed-total-unknown",
        behaviour=(
            "a total this server computes over an affected cell being unknown: "
            "without it the sum silently skips the cell"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "        if anomaly.row_id in data_rows and anomaly.column_key in (None, column_key):\n"
                '            return unknown("a cell it covers could not be read, so any sum would be wrong")\n',
                "        pass\n",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="result-summary-first",
        behaviour=(
            "putting the summary sentence first in the text: without it a "
            "partial report says so somewhere after its rows"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                '    if "summary" in payload:\n'
                '        payload = {"summary": payload.pop("summary"), **payload}\n',
                "",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="projection-transfer-endpoints",
        behaviour="a Transfer keeping both of its accounts after projection",
        path=f"{SRC}/projections.py",
        edits=(
            (
                '    Nested("FromAccountRef", "from_account", _REF_SPEC),\n'
                '    Nested("ToAccountRef", "to_account", _REF_SPEC),\n',
                "",
            ),
        ),
        families=("tests/test_detector_canaries.py",),
    ),
    Mutation(
        id="projection-billpayment-account",
        behaviour="a BillPayment keeping the account that paid it",
        path=f"{SRC}/projections.py",
        edits=(
            (
                '    Nested("CheckPayment", "check_payment", (Nested("BankAccountRef", "bank_account", _REF_SPEC),)),\n',
                "",
            ),
        ),
        families=("tests/test_detector_canaries.py",),
    ),
    Mutation(
        id="projection-payment-method-ref",
        behaviour=(
            "PaymentMethodRef projected as a reference: as a scalar the whole "
            "dict, unknown children included, lands in an id field"
        ),
        path=f"{SRC}/projections.py",
        edits=(
            (
                '    Nested("PaymentMethodRef", "payment_method", _REF_SPEC),\n    Scalar("Credit"',
                '    Scalar("PaymentMethodRef", "payment_method", as_str),\n    Scalar("Credit"',
            ),
        ),
        families=("tests/test_detector_canaries.py",),
    ),
    Mutation(
        id="limits-identity-encoding",
        behaviour="asking for Accept-Encoding: identity on accounting reads",
        path=f"{SRC}/client.py",
        edits=(('                "Accept-Encoding": "identity",\n', ""),),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="limits-body-cap",
        behaviour="the decoded-byte cap on a response body",
        path=f"{SRC}/netpolicy.py",
        edits=(("        if total > max_bytes:\n", "        if False:\n"),),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="limits-streamed-read",
        behaviour="reading the body as a stream, so an oversized one is abandoned early",
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                "    response = client.send(request, follow_redirects=False, stream=True)\n",
                "    response = client.send(request, follow_redirects=False)\n",
            ),
        ),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="limits-oversized-token-ambiguous",
        behaviour=(
            "treating an unreadably large token response as a possibly-accepted "
            "refresh, so the realm is quarantined rather than the token resubmitted"
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            (
                "        except ResponseTooLarge as exc:\n"
                "            # A response arrived, so the request was delivered and may have\n"
                "            # been accepted; only its body could not be kept. That is the\n"
                "            # ambiguous outcome, not a clean failure to retry.\n"
                "            raise TokenResponseLost(\n"
                '                "the token response was too large to read; the request may have been accepted"\n'
                "            ) from exc\n",
                "",
            ),
        ),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="limits-5xx-retry",
        behaviour="retrying a transient 5xx on an idempotent read",
        path=f"{SRC}/client.py",
        edits=(("    if not 500 <= status <= 599:\n        return False\n", "    return False\n"),),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="limits-no-report-504-retry",
        behaviour="never retrying a 504 on a report",
        path=f"{SRC}/client.py",
        edits=(("    return not (status == 504 and _is_report(tail))\n", "    return True\n"),),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="limits-call-deadline",
        behaviour=(
            "the whole-call deadline: without it PER_CALL_BUDGET_SECONDS bounds "
            "nothing and a walk runs as long as it has pages"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                "        with call_deadline() as deadline:\n"
                "            yield binding\n"
                "            # The end-of-call checkpoint: work done after the budget ran out\n"
                "            # -- serialisation, or anything that consulted no checkpoint --\n"
                "            # does not produce a result as if it had been in time.\n"
                "            deadline.final_checkpoint()\n",
                "        yield binding\n",
            ),
        ),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="limits-cancellation",
        behaviour="stopping business reads when the caller cancels",
        path=f"{SRC}/deadline.py",
        edits=(("        anyio.from_thread.check_cancelled()\n", "        return\n"),),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="limits-refresh-budget",
        behaviour=(
            "not beginning a refresh the budget cannot finish, checked under the "
            "lock after the wait for it: once submitted it may not be cut short "
            "without losing the pair"
        ),
        path=f"{SRC}/store.py",
        edits=(("            deadline.checkpoint(need=REFRESH_BUDGET_SECONDS)\n", "            pass\n"),),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="limits-lock-wait-bounded",
        behaviour="bounding a store-lock wait by the call's budget",
        path=f"{SRC}/store.py",
        edits=(
            (
                "        with canonical_lock(self.lock_path, call_deadline.bounded(wait)):\n",
                "        with canonical_lock(self.lock_path, wait):\n",
            ),
        ),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="live-values-received-tokens",
        behaviour=(
            "registering every pair received from Intuit's token endpoint, "
            "before it reaches the store"
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            (
                "        if not self.synthetic:\n"
                "            live_values.register(self.access_token, self.refresh_token)\n\n"
                "    def access_token_digest",
                "        pass\n\n    def access_token_digest",
            ),
        ),
        families=("tests/test_live_values.py",),
    ),
    Mutation(
        id="no-echo-where-field",
        behaviour="never echoing a caller's WHERE field into an error",
        path=f"{SRC}/query_grammar.py",
        edits=(
            (
                '        f"the WHERE field is not a filterable field on {spec.name}. Filterable: "',
                '        f"\'{name}\' is not a filterable field on {spec.name}. Filterable: "',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="no-echo-escape",
        behaviour="never echoing the character after a bad escape",
        path=f"{SRC}/query_grammar.py",
        edits=(
            (
                '                    f"unsupported escape sequence in a literal (position {index}); "',
                '                    f"unsupported escape \'\\\\{nxt}\' in a literal (position {index}); "',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="no-echo-unserved-entity",
        behaviour="naming an unserved entity in this package's spelling, not the caller's",
        path=f"{SRC}/query_grammar.py",
        edits=(
            (
                '                f"{unserved} is not served by this server. It has no projection, "',
                '                f"{name} is not served by this server. It has no projection, "',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="no-echo-columns",
        behaviour="naming an unavailable column by position, never by value",
        # list_transactions shares the report tools' column
        # helper, so this one edit covers both.
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                '    unknown = [f"columns[{i}]" for i, key in enumerate(columns) if key not in allowed]\n',
                '    unknown = [key for key in columns if key not in allowed]\n',
            ),
        ),
        families=("tests/test_changes_and_tools.py", "tests/test_report_tools.py"),
    ),
    Mutation(
        id="no-echo-report-filter",
        behaviour="a report-filter refusal never repeating the value it refused",
        path=f"{SRC}/reports.py",
        edits=(
            (
                '        f"the transaction_type given cannot be used: {reason}. {_REFUSED_SUFFIX} "',
                '        f"transaction_type \'{value}\' cannot be used: {reason}. {_REFUSED_SUFFIX} "',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="report-unsupported-shape-fails",
        behaviour=(
            "failing on a row that is not an object: skipped, nothing would say "
            "a row was missing"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                '                raise _structural(f"row {row_id} is not an object")\n',
                "                continue\n",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-unrecognised-column-type",
        behaviour=(
            "disclosing a published column of an unrecognised type, or an "
            "amount column declared non-monetary: its amounts are otherwise a "
            "silent null"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "    money_keys = MONEY_COLUMN_KEYS.get(report_name, frozenset())\n"
                "    found: list[ReportAnomaly] = []\n",
                "    money_keys = MONEY_COLUMN_KEYS.get(report_name, frozenset())\n"
                "    found: list[ReportAnomaly] = []\n"
                "    kept = []\n",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-mismatched-row-withheld",
        behaviour=(
            "withholding every value of a row with the wrong number of cells: "
            "with the return removed the cells are paired by position and a "
            "value lands under the wrong key, such as a private value under "
            "tx_date or an amount under txn_type"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "        return [], upstream_count\n"
                "\n"
                "    cells: list[ReportCell] = []\n",
                "\n"
                "    cells: list[ReportCell] = []\n",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    # -- the credential store -------------------------------------------------
    Mutation(
        id="store-revoke-recovers-first",
        behaviour=(
            "journal recovery under the lock before a revoke commits: without "
            "it revoking realm B discards realm A's only durable refreshed pair "
            "and leaves A's old, possibly spent token eligible for use"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            document = self._load_locked()\n"
                "            self._recover_locked(document)\n"
                "            row = document.rows.pop(realm_id, None)\n",
                "            document = self._load_locked()\n"
                "            row = document.rows.pop(realm_id, None)\n",
            ),
        ),
        families=("tests/test_store_lifecycle.py",),
    ),
    Mutation(
        id="store-revoke-clears-only-its-realm",
        behaviour=(
            "a revoke clears only its own realm's journal records: without it "
            "the shared journal is cleared whole"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "        remaining = [record for record in records if record.realm_id != realm_id]\n",
                "        remaining = []\n",
            ),
        ),
        families=("tests/test_store_lifecycle.py",),
    ),
    Mutation(
        id="store-401-blocked-before-newer",
        behaviour=(
            "the blocked-state checks before a 401 releases a newer generation: "
            "without them the retry sends a quarantined or consent-required token"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            if row.refresh_uncertain:\n"
                "                raise RefreshUncertainError(REFRESH_UNCERTAIN_TEXT)\n"
                "            if row.needs_consent:\n"
                "                raise NeedsConsentError(needs_consent_text(row.entity))\n"
                "            if row.auth_generation > rejected_generation:\n"
                "                return row\n",
                "            if row.auth_generation > rejected_generation:\n"
                "                return row\n"
                "            if row.refresh_uncertain:\n"
                "                raise RefreshUncertainError(REFRESH_UNCERTAIN_TEXT)\n"
                "            if row.needs_consent:\n"
                "                raise NeedsConsentError(needs_consent_text(row.entity))\n",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="store-consent-epoch-on-consent",
        behaviour=(
            "a consent advances the realm's consent epoch: without it a ceremony "
            "begun before a newer consent commits over it"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            epoch = document.advance_epoch(realm_id)\n"
                "            row = TokenRow(\n",
                "            epoch = document.epoch_of(realm_id)\n"
                "            row = TokenRow(\n",
            ),
        ),
        families=("tests/test_store_lifecycle.py",),
    ),
    Mutation(
        id="store-consent-epoch-on-revoke",
        behaviour=(
            "a revoke advances the realm's consent epoch: without it a ceremony "
            "begun before a revoke resurrects the row"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            document.advance_epoch(realm_id)\n"
                "            self._commit_locked(document)\n"
                "            self._drop_journal_records_locked(realm_id)\n",
                "            self._commit_locked(document)\n"
                "            self._drop_journal_records_locked(realm_id)\n",
            ),
        ),
        families=("tests/test_store_lifecycle.py",),
    ),
    Mutation(
        id="store-guard-verdict-consent-binding",
        behaviour=(
            "a guard verdict is bound to the consent it checked: without it a "
            "late result from the previous consent is written after a re-consent"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            if verdict.consent_epoch != row.consent_epoch:\n"
                "                return row\n"
                "            current = row.name_mismatch\n",
                "            current = row.name_mismatch\n",
            ),
        ),
        families=("tests/test_store_lifecycle.py",),
    ),
    Mutation(
        id="store-update-row-commit-under-lock",
        behaviour=(
            "update_row's commit inside its critical section: with the commit "
            "moved outside the lock a refresh that lands between the load and "
            "the commit is rolled back to the old pair"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            document.rows[realm_id] = updated\n"
                "            self._commit_locked(document)\n"
                "            return updated\n",
                "            document.rows[realm_id] = updated\n"
                "        self._commit_locked(document)\n"
                "        return updated\n",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="cli-revoke-binding",
        behaviour=(
            "revoke reads the stored token through the binding check: without it "
            "a row bound to another app, environment or entity is submitted "
            "under this configuration and then removed"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                "            row = store.usable_row(entity.realm_id, entity_key=entity.key)\n"
                "        except MalformedRowError:\n",
                "            row = store.get_row(entity.realm_id)\n"
                "        except MalformedRowError:\n",
            ),
        ),
        families=("tests/test_oauth_cli.py", "tests/test_ast_regression.py"),
    ),
    Mutation(
        id="cli-import-verifies-before-commit",
        behaviour=(
            "import verifies with the fresh token before it commits, and a "
            "failed verification leaves the existing consent alone: committing "
            "first, a failed replacement destroys a healthy pair"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                "        intuit_name = _probe_company_name(settings, store, entity, response.access_token)\n"
                "        if intuit_name is None:\n"
                "            out(\"\")\n"
                "            out(\"REFUSING: that token could not read the realm this entity names.\")\n"
                "            out(\"Nothing was stored; any consent this entity already had is untouched.\")\n"
                "            pair.discard()\n"
                "            return 2\n",
                "        store.commit_consent(\n"
                "            entity=entity.key, realm_id=entity.realm_id, response=response,\n"
                "            actor=RefreshActor.here(ROLE), origin=\"import\",\n"
                "        )\n"
                "        intuit_name = _probe_company_name(settings, store, entity, response.access_token)\n"
                "        if intuit_name is None:\n"
                "            store.revoke_row(entity.realm_id)\n"
                "            out(\"REFUSING: that token could not read the realm this entity names.\")\n"
                "            return 2\n",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    # -- the AST scanners -----------------------------------------------------
    Mutation(
        id="ast-import-scanner-dead",
        behaviour=(
            "the import scanner behind the forbidden-client and listener-only "
            "rules: returning the empty set would leave the AST tests green"
        ),
        path="tests/test_ast_regression.py",
        edits=(
            (
                "def imported_modules(tree: ast.AST) -> set[str]:\n"
                "    found: set[str] = set()\n",
                "def imported_modules(tree: ast.AST) -> set[str]:\n"
                "    return set()\n"
                "    found: set[str] = set()\n",
            ),
        ),
        families=("tests/test_ast_regression.py",),
    ),
    Mutation(
        id="ast-outbound-call-scanner-dead",
        behaviour="the listener's outbound-connection scanner",
        path="tests/test_ast_regression.py",
        edits=(
            (
                "def outbound_calls(tree: ast.AST) -> list[str]:\n"
                "    dialling = {\"connect\", \"connect_ex\", \"create_connection\", \"urlopen\"}\n",
                "def outbound_calls(tree: ast.AST) -> list[str]:\n"
                "    return []\n"
                "    dialling = {\"connect\", \"connect_ex\", \"create_connection\", \"urlopen\"}\n",
            ),
        ),
        families=("tests/test_ast_regression.py",),
    ),
    Mutation(
        id="ast-send-site-scanner-dead",
        behaviour="the request-building call-site scanner that requires a literal GET",
        path="tests/test_ast_regression.py",
        edits=(
            (
                "    found: list[tuple[str, str | None]] = []\n",
                "    return []\n"
                "    found: list[tuple[str, str | None]] = []\n",
            ),
        ),
        families=("tests/test_ast_regression.py",),
    ),
    Mutation(
        id="ast-method-literal-scanner-dead",
        behaviour="the whole-tree sweep for non-GET method literals",
        path="tests/test_ast_regression.py",
        edits=(
            (
                "def non_get_method_literals(tree: ast.AST) -> set[str]:\n"
                "    return {\n",
                "def non_get_method_literals(tree: ast.AST) -> set[str]:\n"
                "    return set()\n"
                "    return {\n",
            ),
        ),
        families=("tests/test_ast_regression.py",),
    ),
    # -- wrong numbers and leaked fields --------------------------------------
    Mutation(
        id="vendor-payments-finite-amount",
        behaviour=(
            "accepting only finite, representable amounts: without the check "
            "Decimal('Infinity') parses, fails to become cents and is summed as "
            "0.00 with no review item"
        ),
        path=f"{SRC}/vendor_payments.py",
        edits=(
            (
                "    if not parsed.is_finite():\n"
                "        return None\n"
                "    try:\n"
                "        return parsed.quantize(CENTS)\n"
                "    except InvalidOperation:\n"
                "        return None\n",
                "    try:\n"
                "        return parsed.quantize(CENTS)\n"
                "    except InvalidOperation:\n"
                "        return Decimal(\"0.00\")\n",
            ),
        ),
        families=("tests/test_vendor_payments.py",),
    ),
    Mutation(
        id="projection-scalar-leaf-container",
        behaviour=(
            "omitting a container at a scalar leaf: without it an object under "
            "PaymentMethodRef.value is stringified whole into payment_method.id, "
            "unreviewed fields included"
        ),
        path=f"{SRC}/projections.py",
        edits=(
            (
                "        if value is None or not is_scalar(value):\n",
                "        if value is None:\n",
            ),
            (
                "    return str(value) if is_scalar(value) else None\n",
                "    return str(value)\n",
            ),
        ),
        families=("tests/test_projections.py", "tests/test_detector_canaries.py"),
    ),
    # -- capabilities and output contracts --------------------------------------
    Mutation(
        id="session-ap-per-process",
        behaviour=(
            "accounts payable established by this process's own first use: "
            "with a persisted answer imported, a company "
            "whose plan gained the feature stays 'unavailable' until re-consent"
        ),
        path=f"{SRC}/session.py",
        edits=(
            (
                "        capabilities = derive_capabilities(projected)\n"
                "        # Accounts payable is not in Preferences and is not read from the\n",
                "        capabilities = derive_capabilities(projected)\n"
                "        persisted = Capabilities.from_json(row.capabilities)\n"
                "        if persisted is not None and persisted.accounts_payable != AP_UNKNOWN:\n"
                "            capabilities = capabilities.with_accounts_payable(\n"
                "                available=persisted.accounts_payable == 'available',\n"
                "                fault_code=persisted.accounts_payable_fault_code,\n"
                "            )\n"
                "        # Accounts payable is not in Preferences and is not read from the\n",
            ),
            (
                "        self._states[entity.key] = replace(state, capabilities=updated)\n"
                "\n"
                "    def cached_capabilities(",
                "        self._states[entity.key] = replace(state, capabilities=updated)\n"
                "        self.store().update_row(\n"
                "            entity.realm_id, lambda r: replace(r, capabilities=updated.to_json())\n"
                "        )\n"
                "\n"
                "    def cached_capabilities(",
            ),
        ),
        families=("tests/test_accounts_payable.py",),
    ),
    Mutation(
        id="netpolicy-bounded-read",
        behaviour=(
            "each blocking body read bounded by the remaining budget: without it "
            "a response arriving in pieces takes up to a full read timeout per "
            "piece and the deadline is noticed only afterwards"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                "    reader = _BoundedReader(iterator, response) if deadline is not None else None\n",
                "    reader = None\n",
            ),
        ),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="deadline-final-checkpoint",
        behaviour=(
            "the end-of-call checkpoint: without it work done after the budget "
            "ran out returns a result as if it had been in time"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                "            deadline.final_checkpoint()\n",
                "            pass\n",
            ),
        ),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="keepalive-early-configuration-exit-2",
        behaviour=(
            "exit 2 for a configuration error met on the first store read: "
            "without it a permanent setup failure exits 1, which a scheduler "
            "reads as transient"
        ),
        path=f"{SRC}/keepalive.py",
        edits=(
            (
                "        out(f\"token store unavailable: {redact(exc)}\")\n"
                "        return 2\n",
                "        out(f\"token store unavailable: {redact(exc)}\")\n"
                "        return 1\n",
            ),
        ),
        families=("tests/test_keepalive.py",),
    ),
    Mutation(
        id="keepalive-unchanged-label",
        behaviour=(
            "saying a company left alone is unchanged: without it a run inside "
            "the hour claims a refresh that did not happen"
        ),
        path=f"{SRC}/keepalive.py",
        edits=(
            (
                "if age is not None and age > 0 and not args.force:",
                "if age is not None and age > STALE_REFRESH_SECONDS and not args.force:",
            ),
        ),
        families=("tests/test_keepalive.py",),
    ),
    Mutation(
        id="run-query-transaction-rows-refused",
        behaviour=(
            "refusing rows of a transaction type before any request: without it "
            "the walk reads every page and then fails with a generic error"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "            if not parsed.count_only and not has_projection(parsed.entity):\n"
                "                raise QueryError(TRANSACTION_ROWS_REFUSED)\n",
                "",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="keychain-key-through-stdin",
        behaviour=(
            "passing the new store key to security on stdin: as an argument it "
            "sits in the process listing while the child runs"
        ),
        path=f"{SRC}/crypto.py",
        edits=(
            (
                'result = _security("-i", input_text=f"{command}\\n")',
                "result = _security(*shlex.split(command))",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="callback-malformed-query-rejected",
        behaviour=(
            "turning a malformed callback query into the fixed rejection: without "
            "it the parser's error, which quotes the fragment, reaches stderr"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(
            (
                '        raise CallbackRejected("the callback query could not be read") from None\n',
                "        raise\n",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="callback-paste-unreadable-rejected",
        behaviour=(
            "turning an unreadable pasted address into the fixed rejection: "
            "without it the parser's error reaches the operator's terminal"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(
            (
                """        raise CallbackRejected("that address could not be read; paste the full address "
                               "the browser ended up on") from None
""",
                "        raise\n",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="callback-server-fixed-error-line",
        behaviour=(
            "keeping a failed request's traceback out of stderr: it can hold the "
            "request line, which carries the authorization code"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(
            (
                'print("qbo-multicompany-mcp: a callback request failed", file=sys.stderr)',
                "super().handle_error(request, client_address)",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="callback-each-connection-on-its-own-thread",
        behaviour=(
            "a thread for each callback connection: on one serving thread, a local "
            "connection that sends nothing keeps the real callback waiting"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(("class _Server(ThreadingHTTPServer):\n", "class _Server(HTTPServer):\n"),),
        families=("tests/test_oauth_cli.py::test_an_idle_connection_doesnt_hold_the_callback",),
    ),
    Mutation(
        id="callback-close-doesnt-wait-for-a-connection",
        behaviour=(
            "connection threads that closing never waits for: otherwise an idle connection "
            "keeps the consent command from exiting until its deadline"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(("    daemon_threads = True\n", "    daemon_threads = False\n"),),
        families=("tests/test_oauth_cli.py::test_closing_the_listener_doesnt_wait_for_an_idle_connection",),
    ),
    Mutation(
        id="callback-request-deadline",
        behaviour=(
            "one deadline for a whole callback request: a client sending a byte at a time "
            "would otherwise keep a connection, and a thread, for ever"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(("        self._deadline.start()\n", "        pass\n"),),
        families=("tests/test_oauth_cli.py::test_a_connection_that_trickles_is_cut_at_the_deadline",),
    ),
    Mutation(
        id="callback-connection-cap",
        behaviour=(
            "a cap on callback connections served at once: a local flood of idle ones "
            "would use up the descriptors the token exchange and the store write need"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(
            (
                "        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)\n",
                "        self._slots = threading.BoundedSemaphore(10_000)\n",
            ),
        ),
        families=("tests/test_oauth_cli.py::test_a_flood_of_connections_cant_hold_the_listener",),
    ),
    Mutation(
        id="callback-cut-request-never-claims",
        behaviour=(
            "a request the deadline cut never claiming the attempt: what had arrived "
            "parses as complete, and a slow client holding the state would spend it"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(
            (
                "        if self._expired.is_set():\n"
                "            return  # cut off by the deadline, so never a callback\n",
                "",
            ),
        ),
        families=("tests/test_oauth_cli.py::test_a_request_the_deadline_cut_never_claims_the_attempt",),
    ),
    Mutation(
        id="revoke-unconfirmed-pair-exits-nonzero",
        behaviour=(
            "a nonzero exit when a pair stored during a revoke can't be "
            "revoked: without it, someone answering a suspected leak reads success"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """            f"({redact(exc)}); disconnect the app in QuickBooks")
        return 1
""",
                """            f"({redact(exc)}); disconnect the app in QuickBooks")
        return 0
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="status-stops-on-an-unreadable-store",
        behaviour=(
            "status stopping when the store can't be read: without it every "
            "company reads as never authorized and invites a connect"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                "        return 2 if isinstance(exc, ConfigurationError) else 1\n",
                "        rows = {}\n",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="connect-prints-a-quoted-display-name",
        behaviour=(
            "printing display_name as a quoted scalar: without it a name holding "
            "': ' breaks the pasted registry and ' #' cuts it short"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                'out(f"    display_name: {_yaml_scalar(intuit_name)}")',
                'out(f"    display_name: {intuit_name}")',
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="request-lines-stay-out-of-the-log",
        behaviour=(
            "holding httpx and httpcore at WARNING: without it --verbose logs "
            "whole URLs, signed attachment links included"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
""",
                "",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="capped-read-ends-its-worker",
        behaviour=(
            "ending the reader on every exit from a capped read: without it a "
            "size failure leaves a thread waiting until the process exits"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                """        if reader is not None:
            reader._abandon()
    return b"".join(chunks)
""",
                """        pass
    return b"".join(chunks)
""",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="accounting-body-refused",
        behaviour=(
            "refusing an accounting request that carries a body: a GET with one "
            "is not the request the read-only claim describes"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                """    if request.content:
        raise NetworkPolicyViolation("accounting request carries a body")
""",
                "",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="accounting-method-override-refused",
        behaviour=(
            "refusing a method-override header on an accounting request: read-only "
            "must not rest on Intuit ignoring one"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                """    if any("method" in name for name in names):
        raise NetworkPolicyViolation("accounting request carries a method-override header")
""",
                "",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="accounting-host-must-match",
        behaviour=(
            "refusing an accounting Host header that isn't the URL's: the origin "
            "check reads the URL, and the server reads the header"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                """    if request.headers.get("host", netloc) != netloc:
        raise NetworkPolicyViolation("accounting request Host header does not match its URL")
""",
                "",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="attachment-multicast-refused",
        behaviour=(
            "refusing a multicast destination: is_global says yes to one, so the "
            "pre-check would pass it"
        ),
        path=f"{SRC}/attachments.py",
        edits=(
            (
                """    if address.is_multicast:
        # Before is_global, which says yes to multicast addresses.
        return "multicast"
""",
                "",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="fault-code-only-ascii-short-digits-parsed",
        behaviour=(
            "reading only short ASCII digits as a number: without it a superscript "
            "digit or a run of thousands raises inside the error path"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                "numeric = text.isascii() and text.isdigit() and len(text) < 20",
                "numeric = text.isdigit()",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="days-window-counts-both-ends",
        behaviour=(
            "counting both ends of a Days window: without it 201 days pass the "
            "200-day limit"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                "if (end - start).days + 1 > MAX_DAYS_WINDOW:",
                "if (end - start).days > MAX_DAYS_WINDOW:",
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="list-transactions-cleared-is-published",
        behaviour=(
            "publishing cleared as an enum on list_transactions: without it a "
            "client can't read the values from the schema"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "            ClearedFilter | None,\n",
                "            str | None,\n",
            ),
        ),
        families=("tests/test_tool_schema.py",),
    ),
    Mutation(
        id="journal-unreadable-stops-refreshes",
        behaviour=(
            "stopping refreshes after an unreadable journal: without it the record "
            "of an accepted refresh can vanish and the stored token is resubmitted"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            return self._stop_after_an_unreadable_journal_locked(document)\n",
                "            return False\n",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-unreadable-moved-aside",
        behaviour=(
            "moving an unreadable journal aside: left in place the next refresh "
            "or consent overwrites the evidence"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                """            self.journal_path.replace(self.journal_path.with_name(f"{self.journal_path.name}.unreadable-{stamp}"))
""",
                "            pass\n",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-invalid-record-is-unreadable",
        behaviour=(
            "treating an invalid journal record as unreadable: dropped quietly, it "
            "could be the intent that says a token was submitted"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                """        if any(record is None or not _can_act_on(record) for record in parsed):
            raise _JournalUnreadable
""",
                "",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-failed-read-is-not-damage",
        behaviour=(
            "keeping a failed journal read apart from damage: without it one failed "
            "open stops every company and discards a good journal"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                """            raise StoreError(
                f"could not read {self.journal_path.name}: {exc.strerror}. Nothing was changed. "
                f"If this lasts, fix the file's owner or mode, and don't move it: it may be "
                f"the only record of a refresh"
            ) from None
""",
                """            raise _JournalUnreadable from None
""",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-symlink-is-damage",
        behaviour=(
            "treating a symlink in the journal's place as damage: read as a passing "
            "failure it refuses every call until someone removes it"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                """            # and it won't pass.
            raise _JournalUnreadable from None
""",
                """            # and it won't pass.
            raise
""",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-failed-move-tolerated",
        behaviour=(
            "tolerating a failed move of an unreadable journal: raised, it stops "
            "every call on the store, status and verify included"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                """.unreadable-{stamp}"))
        except OSError:
            pass
""",
                """.unreadable-{stamp}"))
        except ZeroDivisionError:
            pass
""",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-stops-every-usable-row",
        behaviour=(
            "stopping every usable row after an unreadable journal: nothing can say "
            "which company it concerned"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                """                row, refresh_uncertain=True, recovery_hint=_unreadable_journal_hint(row.entity)
            )
            changed = True
""",
                """                row, refresh_uncertain=True, recovery_hint=_unreadable_journal_hint(row.entity)
            )
            changed = True
            break
""",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-keeps-a-stronger-stop",
        behaviour=(
            "leaving a row that needs consent as it was: relabelled, the operator "
            "is sent to verify a token already known dead"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                """        for realm_id, row in document.rows.items():
            if row.needs_consent or row.refresh_uncertain:
                continue
            document.rows[realm_id] = replace(
""",
                """        for realm_id, row in document.rows.items():
            if row.refresh_uncertain:
                continue
            document.rows[realm_id] = replace(
""",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-drop-survives-a-failed-read",
        behaviour=(
            "finishing a revoke when the journal fails to read: raised after the row "
            "is gone, it ends a revoke --all with companies still to go"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                """        except (_JournalUnreadable, StoreError):
            return
""",
                """        except ZeroDivisionError:
            return
""",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-unusable-result-stops-the-row",
        behaviour=(
            "stopping a row on a result it can't adopt: skipped, the stored token "
            "is resubmitted though the record says it was spent"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "        return _adoptable_tokens(record) is not None\n",
                "        return isinstance(record.tokens, dict)\n",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="callback-state-compared-as-bytes",
        behaviour=(
            "comparing state as bytes: as text, compare_digest raises a TypeError "
            "on a non-ASCII value and the rejection never happens"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(
            (
                'if not hmac.compare_digest(self.state.encode(), candidate.encode("utf-8", "surrogatepass")):',
                "if not hmac.compare_digest(self.state, candidate):",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="journal-directory-is-damage",
        behaviour=(
            "treating a directory in the journal's place as damage: read as a "
            "passing failure it refuses every call until someone removes it"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "        except (StoreError, IsADirectoryError):\n",
                "        except StoreError:\n",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-failed-read-names-the-file",
        behaviour=(
            "naming the file in a failed journal read: an operator can't fix what "
            "the error doesn't name, and moving it blind resubmits a spent token"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                """                f"could not read {self.journal_path.name}: {exc.strerror}. Nothing was changed. "
""",
                """                f"could not read the journal. Nothing was changed. "
""",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-each-aside-keeps-its-own-name",
        behaviour=(
            "giving each unreadable journal its own name: two inside one second "
            "would overwrite the evidence"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                'stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"',
                'stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"',
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="journal-overflow-is-malformed",
        behaviour=(
            "reading an infinite generation as a malformed record: raised, it "
            "stops every call on the store with a raw error"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "        except (KeyError, TypeError, ValueError, OverflowError):\n",
                "        except (KeyError, TypeError, ValueError):\n",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="connect-unstored-pair-is-revoked",
        behaviour=(
            "revoking a pair connect could not store: left alone it stays live at "
            "Intuit with no local copy"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """        # The exchange spent the grant, and a pair nobody holds shouldn't stay live.
        _say(f"Could not store the credential: {redact(exc)}")
        _discard_unstored_tokens(settings, credentials, response)
""",
                """        # The exchange spent the grant, and a pair nobody holds shouldn't stay live.
        _say(f"Could not store the credential: {redact(exc)}")
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="import-unstored-pair-is-revoked",
        behaviour=(
            "revoking a pair import could not store: the supplied token is spent, "
            "so the new pair would stay live with no local copy"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """        # The refresh spent the supplied token, and a pair nobody holds shouldn't stay live.
        _say(f"Could not store the credential: {redact(exc)}")
        _discard_unstored_tokens(settings, credentials, response)
""",
                """        # The refresh spent the supplied token, and a pair nobody holds shouldn't stay live.
        _say(f"Could not store the credential: {redact(exc)}")
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="connect-wrong-realm-pair-is-revoked",
        behaviour=(
            "revoking a pair whose token response names another realm: refused "
            "but left alone, it stays live at Intuit with no local copy"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """            out("REFUSING: the token Intuit issued is for a different realm.")
            out("Nothing was stored.")
            pair.discard()
""",
                """            out("REFUSING: the token Intuit issued is for a different realm.")
            out("Nothing was stored.")
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="revoke-malformed-row-not-confirmed",
        behaviour=(
            "exiting 1 when revoke removes a row it can't read: exit 0 tells someone "
            "answering a suspected leak the company is done while a token in the row may be live"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """                f"was not revoked at Intuit; disconnect the app in QuickBooks to be sure"
            )
            exit_code = max(exit_code, 1)
""",
                """                f"was not revoked at Intuit; disconnect the app in QuickBooks to be sure"
            )
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="revoke-all-survives-a-failed-removal-malformed",
        behaviour=(
            "carrying on when an unreadable row can't be removed: raised, a revoke "
            "--all stops before the next company"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """            try:
                arrived = store.revoke_row(entity.realm_id)
            except QboMcpError as exc:
                # One company's store failure doesn't end a revoke --all.
                out(f"{entity.key}: the store could not be updated ({redact(exc)}); run revoke again")
                continue
            out(f"{entity.key}: removed from the local store")
""",
                """            arrived = store.revoke_row(entity.realm_id)
            out(f"{entity.key}: removed from the local store")
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="revoke-all-survives-a-failed-removal-unstored",
        behaviour=(
            "carrying on when a company with nothing stored can't be updated: raised, "
            "a revoke --all stops before the next company"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """            try:
                arrived = store.revoke_row(entity.realm_id)
            except QboMcpError as exc:
                out(f"{entity.key}: the store could not be updated ({redact(exc)}); run revoke again")
                exit_code = max(exit_code, 1)
                continue
""",
                """            arrived = store.revoke_row(entity.realm_id)
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="keepalive-configuration-error-continues",
        behaviour=(
            "moving on after one company's configuration error: a return stops every "
            "later company's refresh, and their 100 days keep running"
        ),
        path=f"{SRC}/keepalive.py",
        edits=(
            (
                """            out(f"{entity.key}: {redact(exc)}")
            exit_code = max(exit_code, 2)
            continue
""",
                """            out(f"{entity.key}: {redact(exc)}")
            return 2
""",
            ),
        ),
        families=("tests/test_keepalive.py",),
    ),
    Mutation(
        id="keepalive-first-read-waits-once-more",
        behaviour="the extra lock wait on the keepalive's first read, as its refreshes get",
        path=f"{SRC}/keepalive.py",
        edits=(
            (
                """    try:
        return store.read()
    except StoreLockTimeout:
        return store.read()
""",
                """    return store.read()
""",
            ),
        ),
        families=("tests/test_keepalive.py",),
    ),
    Mutation(
        id="refresh-unbuilt-client-not-submitted",
        behaviour=(
            "reading a token client that couldn't be built as nothing sent: otherwise "
            "the intent survives and the company is quarantined for a refresh that never left"
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            (
                """    try:
        client = isolated_client(timeout=OAUTH_POST_TIMEOUT_SECONDS)
    except OSError as exc:
        raise OAuthError("could not build the HTTP client for the Intuit token endpoint") from exc
""",
                """    client = isolated_client(timeout=OAUTH_POST_TIMEOUT_SECONDS)
""",
            ),
        ),
        families=("tests/test_store_lifecycle.py", "tests/test_oauth_cli.py"),
    ),
    Mutation(
        id="refresh-policy-refusal-not-submitted",
        behaviour=(
            "reading a refresh the policy refused as nothing sent: otherwise the intent "
            "survives and the company is quarantined"
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            (
                "            raise RefusedBeforeSending(str(exc)) from exc\n",
                "            raise\n",
            ),
        ),
        families=("tests/test_store_lifecycle.py",),
    ),
    Mutation(
        id="report-column-keys-unique",
        behaviour=(
            "refusing two report columns whose paths join to one key: cells are "
            "published by key, so an excluded column's cells would pass as a published one's"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "            if path in seen or key in seen_keys:\n",
                "            if path in seen:\n",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-unrecognised-row-flagged",
        behaviour="listing a report row with nothing this server reads, rather than dropping it unseen",
        path=f"{SRC}/reports.py",
        edits=(
            (
                """                anomalies.append(
                    ReportAnomaly(row_id, None, ANOMALY_UNRECOGNISED_ROW, _UNRECOGNISED_ROW_DETAIL)
                )
""",
                "",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-unrecognised-row-kept-as-a-row",
        behaviour=(
            "keeping a row nothing reads as an empty row, as a short row is: dropped, the "
            "section total over it and the zero-row re-check can't see it"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """                row_total += 1
                rows.append(
                    FlatRow(
                        row_id=row_id,
                        parent_row_id=parent,
                        row_type=ROW_DATA,
                        depth=depth,
                        group=group,
                        cells=[],
                    )
                )
                continue
""",
                """                continue
""",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-rows-held-outside-the-list-flagged",
        behaviour=(
            "listing rows a report holds outside its row list: passed over, they vanish "
            "and the result reads as whole"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """        if any(key != "Row" and value not in (None, "", [], {}) for key, value in container.items()):
""",
                """        if False:
""",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-missing-row-list-still-checked",
        behaviour=(
            "checking a rows object with no row list for rows held elsewhere: returning "
            "early drops them unseen"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """        if block is None:
            block = []
""",
                """        if block is None:
            return
""",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-rows-held-elsewhere-stay-in-their-section",
        behaviour=(
            "numbering rows held outside the list inside their section: numbered at the "
            "top, the section's stated total isn't flagged"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """            row_id = f"{parent}.{len(block)}" if parent else str(len(block))
""",
                """            row_id = str(len(block))
""",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-empty-member-is-no-loss",
        behaviour=(
            "reading an empty member of a rows object as holding nothing: counted as "
            "rows, every such report would read partial"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """        if any(key != "Row" and value not in (None, "", [], {}) for key, value in container.items()):
""",
                """        if any(key != "Row" and value is not None for key, value in container.items()):
""",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="unstored-pair-revoked-on-the-way-out",
        behaviour=(
            "revoking an issued pair when an error or an interrupt ends consent or "
            "import before the commit: it would stay live with no local copy"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """        if kind is not None and self._open:
""",
                """        if False:
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="unstored-pair-revoked-once",
        behaviour=(
            "one revoke per unstored pair: an interrupt during a refusal's revoke "
            "would start a second on the way out"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """        if self._open:
            self._open = False
            _discard_unstored_tokens(self._settings, self._credentials, self._response)
""",
                """        if self._open:
            _discard_unstored_tokens(self._settings, self._credentials, self._response)
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="client-unbuildable-is-a-fixed-error",
        behaviour=(
            "an accounting HTTP client that can't be built raised as fixed text: a "
            "bare OSError skips consent's refusal and reaches a model as an "
            "unexpected failure"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                """        try:
            http = self._client()
        except OSError as exc:
""",
                """        try:
            http = self._client()
        except ValueError as exc:
""",
            ),
        ),
        families=("tests/test_oauth_cli.py", "tests/test_changes_and_tools.py"),
    ),
    Mutation(
        id="report-row-member-unread-flagged",
        behaviour=(
            "listing what a report row holds in a member this server doesn't read: "
            "passed over, it vanishes and the row reads as whole"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """                if any(key not in _ROW_MEMBERS and value not in (None, "", [], {}) for key, value in raw.items())
""",
                """                if False
""",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-row-member-unread-inside-its-section",
        behaviour=(
            "numbering what a row holds unread under that row: numbered outside, the "
            "section's stated total isn't flagged"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """                f"{row_id}.unread"
""",
                """                f"unread.{row_id}"
""",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-row-member-unread-before-the-total",
        behaviour=(
            "listing what a section row holds unread before the section's total: read "
            "in order, a row of the section would follow its total"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """            if unread_id is not None and isinstance(summary_block, Mapping):
""",
                """            if False:
""",
            ),
            (
                """            if unread_id is not None and not isinstance(summary_block, Mapping):
""",
                """            if unread_id is not None:
""",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-row-known-members-stay-clean",
        behaviour=(
            "reading a row's type and group as members this server knows: counted as "
            "unread, every report would read partial"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """_ROW_MEMBERS: Final = frozenset({"type", "group", "Header", "Rows", "Summary", "ColData"})
""",
                """_ROW_MEMBERS: Final = frozenset({"Header", "Rows", "Summary", "ColData"})
""",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-header-or-total-member-unread-flagged",
        behaviour=(
            "listing what a section's header or total holds beside its cells: passed "
            "over, it vanishes and the section reads as whole"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """                    key != "ColData" and value not in (None, "", [], {})
""",
                """                    False
""",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="consent-kept-when-the-journal-cant-be-cleared",
        behaviour=(
            "keeping a stored consent whose journal can't be cleared: read as a failed "
            "commit, the CLI revokes the pair the store holds and the connection breaks"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                """            try:
                self._clear_journal_locked()
            except StoreError:
                log.warning("the consent is stored, but the store's journal could not be cleared; run status")
""",
                """            self._clear_journal_locked()
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="unstored-pair-revoked-past-a-failing-line",
        behaviour=(
            "revoking an unstored pair even when the line saying the step stopped "
            "fails: a closed terminal would leave the pair live"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """            try:
                _say("", "Stopped before the credential was stored; nothing was written.")
            finally:
                # An interrupt while the lines print still gets the revoke.
                self.discard()
""",
                """            _say("", "Stopped before the credential was stored; nothing was written.")
            self.discard()
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="unstored-pair-keeps-the-original-error",
        behaviour=(
            "keeping the error that ended the step when a stopped line fails: the "
            "line's error would replace it, and an interrupt would read as a crash"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """                _say("", "Stopped before the credential was stored; nothing was written.")
""",
                """                out("")
                out("Stopped before the credential was stored; nothing was written.")
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="revoke-line-keeps-the-original-error",
        behaviour=(
            "keeping the error that ended the step when the revoke's own line fails: "
            "the line's error would replace it"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """        _say("  (the unstored credential was revoked at Intuit)")
""",
                """        out("  (the unstored credential was revoked at Intuit)")
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="say-keeps-the-original-error",
        behaviour=(
            "printing the lines on the way to a revoke so a closed terminal can't fail "
            "them: its error would cost the revoke or replace the error that ended the step"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """    with contextlib.suppress(OSError, ValueError):
        for line in lines:
            out(line)
""",
                """    if True:
        for line in lines:
            out(line)
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="connect-stale-line-keeps-the-revoke",
        behaviour=(
            "printing connect's stale-ceremony lines so a closed terminal can't skip "
            "the revoke after them"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """            origin="connect",
        )
    except StaleConsentError as exc:
        _say("", "STALE CEREMONY", f"  {exc}")
""",
                """            origin="connect",
        )
    except StaleConsentError as exc:
        out("")
        out("STALE CEREMONY")
        out(f"  {exc}")
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="connect-store-line-keeps-the-revoke",
        behaviour=(
            "printing connect's could-not-store line so a closed terminal can't skip "
            "the revoke after it"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """        # The exchange spent the grant, and a pair nobody holds shouldn't stay live.
        _say(f"Could not store the credential: {redact(exc)}")
""",
                """        # The exchange spent the grant, and a pair nobody holds shouldn't stay live.
        out(f"Could not store the credential: {redact(exc)}")
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="import-stale-line-keeps-the-revoke",
        behaviour=(
            "printing import's stale-ceremony lines so a closed terminal can't skip "
            "the revoke after them"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """            origin="import",
        )
    except StaleConsentError as exc:
        _say("", "STALE CEREMONY", f"  {exc}")
""",
                """            origin="import",
        )
    except StaleConsentError as exc:
        out("")
        out("STALE CEREMONY")
        out(f"  {exc}")
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="import-store-line-keeps-the-revoke",
        behaviour=(
            "printing import's could-not-store line so a closed terminal can't skip "
            "the revoke after it"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """        # The refresh spent the supplied token, and a pair nobody holds shouldn't stay live.
        _say(f"Could not store the credential: {redact(exc)}")
""",
                """        # The refresh spent the supplied token, and a pair nobody holds shouldn't stay live.
        out(f"Could not store the credential: {redact(exc)}")
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="zero-row-recheck-says-a-partial-reread",
        behaviour=(
            "saying the unfiltered re-read was itself partial: its row count would read "
            "as an exact count of transactions"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                """                        unfiltered_partial=any(
                            a.kind == reports_mod.ANOMALY_UNRECOGNISED_ROW for a in unfiltered.anomalies
                        ),
""",
                """                        unfiltered_partial=False,
""",
            ),
        ),
        families=("tests/test_sandbox_probe_results.py",),
    ),
    Mutation(
        id="zero-row-recheck-exact-after-a-cell-anomaly",
        behaviour=(
            "calling a re-read's count inexact only for rows it couldn't read: a cell "
            "it couldn't read leaves the count exact, and saying otherwise is wrong"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                """                            a.kind == reports_mod.ANOMALY_UNRECOGNISED_ROW for a in unfiltered.anomalies
""",
                """                            True for a in unfiltered.anomalies
""",
            ),
        ),
        families=("tests/test_sandbox_probe_results.py",),
    ),
    Mutation(
        id="changes-object-without-an-id-refused",
        behaviour="refusing a change response with an object that names no id",
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                """                    if item_id is None or item_id == "":
                        # Every change Intuit documents names its object, a
                        # deletion included.
                        raise QboMcpError(CDC_MALFORMED_TEXT)
""",
                "",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="changes-metadata-not-an-object-refused",
        behaviour="refusing a change response whose object carries MetaData that isn't an object",
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                """                    if metadata is not None and not isinstance(metadata, Mapping):
                        raise QboMcpError(CDC_MALFORMED_TEXT)
                    last_updated = metadata.get("LastUpdatedTime") if metadata is not None else None
""",
                """                    last_updated = metadata.get("LastUpdatedTime") if isinstance(metadata, Mapping) else None
""",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="list-updated-since-checked-first",
        behaviour=(
            "refusing an updated_since that is no timestamp before any request, as the "
            "input alone decides it"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                """    moment = updated_since.strip()
    if _TIMESTAMP_SHAPE.fullmatch(moment):
        try:
            datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return moment
    raise QueryError(f"updated_since must be {_TIMESTAMP_HELP}")
""",
                """    moment = updated_since.strip()
    return moment
""",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="list-updated-since-takes-intuit-shapes-only",
        behaviour=(
            "taking only the timestamp shapes Intuit writes: Python reads week dates and "
            "compact forms too, and those would reach a query nothing says accepts them"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "    if _TIMESTAMP_SHAPE.fullmatch(moment):\n",
                "    if True:\n",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="attachment-port-free-for-a-loopback-literal-only",
        behaviour=(
            "keeping a pinned name on the default port against a loopback API base, "
            "where only the loopback mock's literal may use another"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                "    if request.url.port not in (None, 443) and not loopback_mock:\n",
                "    if request.url.port not in (None, 443) and not test_mode:\n",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="gate-copy-leaves-local-configuration-out",
        behaviour=(
            "leaving local configuration out of the gate's copy even when tracked: "
            "a .env in the copy is a credential in a temporary tree"
        ),
        path="scripts/mutation_gate.py",
        edits=(
            (
                "    return name in _EXAMPLES or not _LOCAL_CONFIG.fullmatch(name)\n",
                "    return True\n",
            ),
        ),
        families=(f"{GATE_TESTS}::test_the_copy_holds_what_git_lists_and_no_local_configuration",),
    ),
    Mutation(
        id="gate-control-runs-first",
        behaviour=(
            "running the control before any entry: run last, a harness that can't "
            "catch it has already printed verdicts nobody should trust"
        ),
        path="scripts/mutation_gate.py",
        edits=(
            (
                "    selected = [control] + [m for m in selected if m is not control]\n",
                "    selected = selected if control in selected else selected + [control]\n",
            ),
        ),
        families=(f"{GATE_TESTS}::test_the_control_runs_first_and_a_miss_stops_the_gate",),
    ),
    Mutation(
        id="gate-copy-leaves-the-git-directory-behind",
        behaviour=(
            "giving the copy a fresh repository: the original's git directory keeps "
            "whatever was ever staged, a .env added by mistake included"
        ),
        path="scripts/mutation_gate.py",
        edits=(
            (
                '    git("init", "-q")\n',
                '    shutil.copytree(root / ".git", copy / ".git", symlinks=True)\n',
            ),
        ),
        families=(f"{GATE_TESTS}::test_the_copy_holds_what_git_lists_and_no_local_configuration",),
    ),
    Mutation(
        id="gate-copy-adds-ignored-tracked-files",
        behaviour=(
            "forcing the copy's add: a tracked file an ignore rule matches, a lock file "
            "say, is refused by name and the gate can't start"
        ),
        path="scripts/mutation_gate.py",
        edits=(
            (
                '        "--literal-pathspecs", "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",\n',
                '        "--literal-pathspecs", "add", "--pathspec-from-file=-", "--pathspec-file-nul",\n',
            ),
        ),
        families=(f"{GATE_TESTS}::test_a_tracked_file_an_ignore_rule_matches_is_tracked_in_the_copy",),
    ),
    Mutation(
        id="gate-copy-takes-names-literally",
        behaviour=(
            "reading the copy's file names literally: ':notes.md' reads as pathspec magic "
            "and '[ab].txt' as a pattern that also takes an untracked a.txt"
        ),
        path="scripts/mutation_gate.py",
        edits=(
            (
                '        "--literal-pathspecs", "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",\n',
                '        "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",\n',
            ),
        ),
        families=(f"{GATE_TESTS}::test_a_tracked_name_is_read_as_a_name",),
    ),
    Mutation(
        id="gate-copy-strips-git-variables",
        behaviour=(
            "running the gate's git commands without GIT_* variables: GIT_DIR from a hook "
            "or a shell points the listing and the copy's add at another repository"
        ),
        path="scripts/mutation_gate.py",
        edits=(
            (
                '    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}\n',
                "    env = dict(os.environ)\n",
            ),
        ),
        families=(f"{GATE_TESTS}::test_the_copy_answers_to_no_git_variable_or_user_configuration",),
    ),
    Mutation(
        id="gate-copy-leaves-user-configuration-out",
        behaviour=(
            "keeping the user's and the system's git configuration out of the copy, so "
            "no setting there changes what it holds"
        ),
        path="scripts/mutation_gate.py",
        edits=(
            (
                '    env["GIT_CONFIG_GLOBAL"] = os.devnull\n    env["GIT_CONFIG_NOSYSTEM"] = "1"\n',
                "",
            ),
        ),
        families=(f"{GATE_TESTS}::test_the_copy_answers_to_no_git_variable_or_user_configuration",),
    ),
    Mutation(
        id="gate-copy-checks-its-index",
        behaviour=(
            "comparing the copy's index with what git tracks: git can leave a name "
            "unmatched without an error, and the hygiene tests would read another tree"
        ),
        path="scripts/mutation_gate.py",
        edits=(("    if staged != set(tracked):\n", "    if False:\n"),),
        families=(f"{GATE_TESTS}::test_a_copy_whose_index_differs_from_the_list_fails",),
    ),
    Mutation(
        id="gate-listing-keeps-user-configuration",
        behaviour=(
            "keeping the user's own git configuration when the original's files are listed: "
            "named with GIT_CONFIG_GLOBAL, its ignore rules would stop applying"
        ),
        path="scripts/mutation_gate.py",
        edits=(
            (
                '        return {name: value for name, value in os.environ.items() if name not in local}\n',
                '        return {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}\n',
            ),
        ),
        families=(f"{GATE_TESTS}::test_the_listing_keeps_the_users_own_configuration",),
    ),
    Mutation(
        id="gate-listing-ignores-repository-variables",
        behaviour=(
            "listing the original's files without variables that name a repository: a "
            "GIT_DIR from a hook lists another repository's files"
        ),
        path="scripts/mutation_gate.py",
        edits=(
            (
                '        return {name: value for name, value in os.environ.items() if name not in local}\n',
                "        return dict(os.environ)\n",
            ),
        ),
        families=(f"{GATE_TESTS}::test_the_copy_answers_to_no_git_variable_or_user_configuration",),
    ),
    Mutation(
        id="gate-listing-keeps-configuration-variables",
        behaviour=(
            "keeping configuration given through the environment when the original's files "
            "are listed: GIT_CONFIG_COUNT and git -c settings are the user's own"
        ),
        path="scripts/mutation_gate.py",
        edits=(
            (
                "    return frozenset(listed.stdout.split()) - _CONFIGURATION_VARIABLES\n",
                "    return frozenset(listed.stdout.split())\n",
            ),
        ),
        families=(f"{GATE_TESTS}::test_the_listing_keeps_the_users_own_configuration",),
    ),
    Mutation(
        id="gate-refuses-old-git",
        behaviour=(
            "refusing a git older than 2.32: earlier ones ignore GIT_CONFIG_GLOBAL, so the "
            "copy would read the user's configuration"
        ),
        path="scripts/mutation_gate.py",
        edits=(("    _require_git()\n", ""),),
        families=(f"{GATE_TESTS}::test_a_git_too_old_for_the_copy_is_refused",),
    ),
    Mutation(
        id="gate-family-runs-get-no-repository-variables",
        behaviour=(
            "running the families without variables that name a repository: the hygiene "
            "tests' git commands would read the original through a GIT_DIR from a hook"
        ),
        path="scripts/mutation_gate.py",
        edits=(("    env = _git_environment(own_config=True)\n", "    env = dict(os.environ)\n"),),
        families=(f"{GATE_TESTS}::test_family_runs_get_no_variable_naming_a_repository",),
    ),
    Mutation(
        id="suite-drops-repository-variables",
        behaviour=(
            "dropping variables that name a repository before the suite runs: run from a git "
            "hook, a test's git add would stage into the hook's index"
        ),
        path="tests/conftest.py",
        edits=(("        os.environ.pop(name, None)\n", "        pass\n"),),
        families=(f"{GATE_TESTS}::test_the_suite_drops_variables_naming_another_repository",),
    ),
    Mutation(
        id="suite-keeps-configuration-variables",
        behaviour=(
            "keeping configuration given through the environment for the suite: a CI "
            "checkout another user owns may need the safe.directory set that way"
        ),
        path="tests/conftest.py",
        edits=(
            (
                '    for name in set(listed.stdout.split()) - {"GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}:\n',
                "    for name in listed.stdout.split():\n",
            ),
        ),
        families=(f"{GATE_TESTS}::test_the_suite_drops_variables_naming_another_repository",),
    ),
    Mutation(
        id="redaction-records-masked-as-made",
        behaviour=(
            "masking each record as the factory makes it: a logger class that makes its "
            "own records, skipping the base makeRecord, would hand them over unmasked"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """    current = logging.getLogRecordFactory()
    if not getattr(current, "masks_records", False):
        logging.setLogRecordFactory(_masked_as_made(current))
""",
                "",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="redaction-make-record-backstop",
        behaviour=(
            "masking again where every logger makes its records: a library that sets its "
            "own record factory without calling ours would leave every record unmasked"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """    if not getattr(logging.Logger.makeRecord, "masks_records", False):
        logging.Logger.makeRecord = _masked_on_make(logging.Logger.makeRecord)  # type: ignore[method-assign]
""",
                "",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="redaction-bad-exc-info-never-raises",
        behaviour=(
            "masking a malformed exc_info rather than raising: the filter runs inside every "
            "logging call in the process, so a raise breaks the call that logged"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """            try:
                record.exc_text = redact(_format_exc(*record.exc_info))
            except Exception:
                # A malformed exc_info from some library: this runs inside
                # every logging call in the process, so it must never raise.
                record.exc_text = REDACTED
""",
                """            record.exc_text = redact(_format_exc(*record.exc_info))
""",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="cli-interrupt-claims-no-outcome",
        behaviour=(
            "an interrupt that doesn't claim nothing changed: revoke --all can finish one "
            "company first, and a consent can commit before its last line prints"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """            "\\ninterrupted. What was reported above as done is done, and the step "
            "under way may have finished; run 'status' before trying again",
""",
                """            "\\ninterrupted; nothing was changed",
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="revoke-builds-its-client-first",
        behaviour=(
            "building the revoke's client before the first company: built per call, a "
            "client that can't be built makes every company 'removed locally anyway' "
            "with nothing sent, and a rerun finds nothing to revoke"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """    try:
        http = oauth.token_client()
    except oauth.OAuthError as exc:
        raise StartupError(
            f"{exc}, so nothing was revoked or removed. Check SSLKEYLOGFILE and the CA "
            f"bundle, then run revoke again"
        ) from exc
    try:
        return _revoke_each(settings, store, credentials, targets, http)
    finally:
        http.close()
""",
                """    return _revoke_each(settings, store, credentials, targets, None)
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="revoke-a-pair-stored-mid-revoke",
        behaviour=(
            "revoking a pair a consent stored between a company's read and its removal: "
            "left alone it is live at Intuit with no local copy"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """    if arrived is None:
        return 0
""",
                """    return 0
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="revoke-arrived-pair-binding-checked",
        behaviour=(
            "the binding check on a pair stored mid-revoke: without it a consent from a "
            "process configured with another app is sent under this one's credentials"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                "        store.check_binding(arrived, entity_key=entity.key)\n",
                "        pass\n",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="revoke-returned-pair-binding-checked",
        behaviour=(
            "the binding check on the pair a removal hands back after the main revoke: "
            "a consent from another app's process would be posted under this app's credentials"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                "            exit_code = max(exit_code, _revoke_arrived(settings, store, credentials, entity, removed, http))\n",
                "            oauth.revoke_token(token=removed.refresh_token, client_id=credentials.client_id, "
                "client_secret=credentials.client_secret, environment=settings.environment, client=http)\n",
            ),
        ),
        families=("tests/test_oauth_cli.py::test_a_pair_another_app_stored_during_the_revoke_request_is_not_sent",),
    ),
    Mutation(
        id="timestamp-shape-ascii-digits",
        behaviour=(
            "ASCII digits only in an updated_since timestamp: a pure-Python datetime reads "
            "other digits through int(), and the shape would let them through"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                r'    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})?)?"' + "\n",
                r'    r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?)?"' + "\n",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="keepalive-rejected-credentials-stop",
        behaviour=(
            "stopping at invalid_client: the app's own credentials, so carrying on sends "
            "every other company's refresh token with credentials Intuit just refused"
        ),
        path=f"{SRC}/keepalive.py",
        edits=(
            (
                """        except AppCredentialsRejected as exc:
            out(f"{entity.key}: {redact(exc)}")
            rejected = True
            exit_code = max(exit_code, 2)
            continue
""",
                "",
            ),
        ),
        families=("tests/test_keepalive.py",),
    ),
    Mutation(
        id="keepalive-later-companies-keep-their-own-line",
        behaviour=(
            "each company after invalid_client keeps the line its own state calls for: "
            "a blanket 'not attempted' hides a malformed row or a needed consent"
        ),
        path=f"{SRC}/keepalive.py",
        edits=(
            (
                """            rejected = True
            exit_code = max(exit_code, 2)
            continue
""",
                """            keys = [each.key for each in registry]
            for later in keys[keys.index(entity.key) + 1:]:
                out(f"{later}: not attempted (Intuit rejected the app's credentials)")
            return 2
""",
            ),
        ),
        families=("tests/test_keepalive.py",),
    ),
    Mutation(
        id="keepalive-names-the-companies-it-skips",
        behaviour=(
            "a line for each company the keepalive skips after invalid_client: without it "
            "they are missing from the output, and a missing line reads as a crash"
        ),
        path=f"{SRC}/keepalive.py",
        edits=(
            (
                """        if rejected:
            out(f"{entity.key}: not attempted (Intuit rejected the app's credentials)")
            continue
""",
                """        if rejected:
            continue
""",
            ),
        ),
        families=("tests/test_keepalive.py",),
    ),
    Mutation(
        id="store-invalid-client-is-the-apps-own",
        behaviour=(
            "raising invalid_client as the app's credentials rejected, which the keepalive "
            "stops on, rather than as one company's configuration"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            raise AppCredentialsRejected(str(exc)) from exc\n",
                "            raise ConfigurationError(str(exc)) from exc\n",
            ),
        ),
        families=("tests/test_keepalive.py",),
    ),
    Mutation(
        id="revoke-all-survives-a-failed-removal",
        behaviour=(
            "carrying on when one company's row can't be removed: raised, a revoke "
            "--all leaves the remaining companies live"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """        try:
            removed = store.revoke_row(entity.realm_id)
        except QboMcpError as exc:
""",
                """        try:
            removed = store.revoke_row(entity.realm_id)
        except ZeroDivisionError as exc:
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="revoke-all-survives-an-unreadable-row",
        behaviour=(
            "carrying on when one company's row can't be read: raised, a revoke "
            "--all leaves the remaining companies live"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """            exit_code = 2
            continue
        except QboMcpError as exc:
""",
                """            exit_code = 2
            continue
        except ZeroDivisionError as exc:
""",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="yaml-scalar-escapes-what-json-leaves-raw",
        behaviour=(
            "escaping the characters JSON leaves raw: a YAML loader refuses or "
            "folds them, so the pasted name would not match"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                '    return re.sub(unsafe, lambda match: f"\\\\u{ord(match.group()):04x}", quoted)\n',
                "    return quoted\n",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    # -- live values, at any length ---------------------------------------------
    Mutation(
        id="live-values-any-length",
        behaviour=(
            "registering every value the process holds, whatever its length: "
            "with a length floor a short live token is neither known to "
            "identity nor refused at a test-only base"
        ),
        path=f"{SRC}/live_values.py",
        edits=(
            (
                "    candidates = [v for v in values if isinstance(v, str) and v]\n",
                "    candidates = [v for v in values if isinstance(v, str) and len(v) >= 16]\n",
            ),
        ),
        families=("tests/test_network_policy.py", "tests/test_live_values.py"),
    ),
    Mutation(
        id="netpolicy-identity-leg-exact",
        behaviour=(
            "the exact identity match on the values a request carries: without "
            "it only the substring scan runs, and its text floor lets a short "
            "held credential through"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                "    held_exactly = any(live_values.holds(value) for value in exact)\n",
                "    held_exactly = False\n",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    # -- the store: malformed rows, consent, locks and files ------------------
    Mutation(
        id='store-malformed-row-isolation',
        behaviour=(
            'one malformed row set aside instead of raised: without it one bad realm takes every other realm down'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('            except StoreError as exc:\n                # One bad realm must not take the other realms down: the row\n                # is set aside, and every access to *its* realm raises.\n                malformed[str(realm)] = MalformedRow(raw=raw, reason=str(exc))\n', '            except StoreError:\n                raise\n'),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='store-malformed-row-blocks-its-realm',
        behaviour=(
            'a malformed row raising on access rather than reading as absent: absent invites a connect and sends nothing to the operator'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('        if broken is not None:\n            raise MalformedRowError(malformed_row_text(realm_id, broken.reason))\n', ''),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='store-stale-consent-check',
        behaviour=(
            'the consent baseline check: without it a ceremony begun before a revoke or a newer consent commits over it'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('            if baseline is not None and not baseline.still_holds(document):\n                raise StaleConsentError(stale_consent_text(entity))\n', ''),
        ),
        families=('tests/test_store_lifecycle.py', 'tests/test_token_store.py'),
    ),
    Mutation(
        id='cli-connect-consent-baseline',
        behaviour=(
            "the connect command handing its baseline to the commit: without it the store's check has nothing to check"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            ('        return _finish_connect(\n            settings, store, entity, credentials, attempt, params, baseline=baseline\n        )\n', '        return _finish_connect(settings, store, entity, credentials, attempt, params)\n'),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='store-guard-verdict-binding',
        behaviour=(
            'binding a persisted guard verdict to the registry name it verified: without it a corrected display_name keeps the stale verdict until a data call'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('        return bool(self.registry_fingerprint) and (\n            self.registry_fingerprint == registry_fingerprint(display_name)\n        )\n', '        return True\n'),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='store-guard-newest-verdict-wins',
        behaviour=(
            'an older process never overwriting a newer guard observation'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('            if current is not None and current.checked_at > verdict.checked_at:\n                return row\n', ''),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='store-open-nofollow',
        behaviour=(
            'refusing a symlink at the lock or store path: without it a planted symlink is followed'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('        return os.open(path, flags | os.O_NOFOLLOW, mode)\n', '        return os.open(path, flags, mode)\n'),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='store-lock-inode-check',
        behaviour=(
            'the lock-replacement check: without it a lock taken on a replaced inode is held by two processes at once'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('            if _same_inode(fd, lock_path):\n                return fd\n', '            return fd\n'),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='store-dir-private',
        behaviour=(
            'the store directory created 0700: the mode on the mkdir and the chmod after it each suffice under an ordinary umask, so both are removed here and the default 0755 is what is left'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('            candidate.mkdir(mode=0o700)\n', '            candidate.mkdir()\n'),
            ('        os.chmod(candidate, 0o700)\n', '        pass\n'),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='store-tmp-cleanup-on-cancel',
        behaviour=(
            'removing the temp file when a write is cancelled: without it a cancellation mid-replace leaves an encrypted copy of the pair behind'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('    except BaseException:\n        # A cancellation mid-write must not leave a credential-bearing temp\n        # file behind.\n        tmp.unlink(missing_ok=True)\n        raise\n', ''),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='store-cleanup-failure-not-a-refresh-failure',
        behaviour=(
            'a failed journal clear after the pair is durable not failing the refresh: without it a full disk at cleanup costs the caller a token it already has'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('        self._drop_journal_after_a_definite_outcome()\n        return updated\n', '        self._clear_journal_locked()\n        return updated\n'),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='store-intent-written-before-send',
        behaviour=(
            'the intent written before the token is submitted: without it a crash in flight leaves no record that anything was sent'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('        self._write_journal_locked([intent])\n\n        try:\n            response = oauth.refresh_tokens(', '        try:\n            response = oauth.refresh_tokens('),
        ),
        families=('tests/test_token_store.py',),
    ),
    Mutation(
        id='store-competing-results-quarantine',
        behaviour=(
            'quarantining a realm whose journal holds two results for one generation: without it the row stays usable and the next refresh submits a token whose liveness is unknown'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('                quarantine[realm_id] = _competing_results_hint(document.rows[realm_id].entity)\n                continue\n', '                continue\n'),
        ),
        families=('tests/test_store_lifecycle.py', 'tests/test_token_store.py'),
    ),
    Mutation(
        id='store-verify-only-when-uncertain',
        behaviour=(
            'verify submitting nothing for a realm that is not in quarantine'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('            if not row.refresh_uncertain:\n                return VerifyOutcome(row=row, submitted=False)\n', ''),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='cli-verify-asks-first',
        behaviour=(
            "the verify command's confirmation: without it a stored token is submitted by a command that merely named an entity"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            ('    if not _confirm("Submit the stored refresh token once?"):\n        out("Nothing was submitted and nothing was changed.")\n        return 1\n', ''),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='oauth-4xx-grant-survives',
        behaviour=(
            'a 4xx from the token endpoint read as a refusal the grant survived: without it a throttled endpoint quarantines a realm that was never at risk'
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            ('        return 400 <= self.status <= 499\n', '        return False\n'),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='store-5xx-ambiguous',
        behaviour=(
            'a 5xx from the token endpoint read as ambiguous: without it the intent is dropped and the possibly-spent token is resubmitted'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('            if exc.grant_outcome_known:\n', '            if True:\n'),
        ),
        families=('tests/test_store_lifecycle.py',),
    ),
    Mutation(
        id='oauth-hard-expiry-read',
        behaviour=(
            'reading x_refresh_token_hard_expires_in when a token response carries it: without it the hard clock is discarded even when Intuit reports it'
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            ('        refresh_token_hard_expires_at=(\n            received_at + timedelta(seconds=hard_ttl) if hard_ttl > 0 else None\n        ),\n', ''),
        ),
        families=('tests/test_expiry_clocks.py',),
    ),
    Mutation(
        id='store-hard-expiry-warning',
        behaviour=(
            'warning on the hard clock: without it a token one day from its five-year limit with a full inactivity clock gets no warning'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('    if days is not None and days <= REFRESH_HARD_EXPIRY_WARN_DAYS:\n', '    if False:\n'),
        ),
        families=('tests/test_expiry_clocks.py',),
    ),
    Mutation(
        id='store-inactivity-never-called-hard',
        behaviour=(
            'the inactivity warning naming the inactivity clock: planted here, a short inactivity lifetime is called hard expiry'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('            f"inactivity expiry in {inactivity:.1f} days: the 100-day clock that "\n', '            f"hard expiry approaching: inactivity expiry in {inactivity:.1f} days: the 100-day clock that "\n'),
        ),
        families=('tests/test_expiry_clocks.py', 'tests/test_keepalive.py'),
    ),
    Mutation(
        id='store-hard-expiry-expired-state',
        behaviour=(
            'auth_state reporting expired past an observed hard expiry, not only past the inactivity clock'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('        hard_days = self.hard_expiry().days(now=now)\n        if hard_days is not None and hard_days <= 0:\n            return "expired"\n', ''),
        ),
        families=('tests/test_expiry_clocks.py',),
    ),
    Mutation(
        id='store-journal-carries-hard-expiry',
        behaviour=(
            'the journal carrying an observed hard expiry into adoption: without it a crash between journal and store loses the observation'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('        refresh_token_hard_expires_at=(\n            _parse_iso(tokens.get("refresh_token_hard_expires_at"))\n            or row.refresh_token_hard_expires_at\n        ),\n', ''),
        ),
        families=('tests/test_expiry_clocks.py',),
    ),
    Mutation(
        id='store-observed-hard-expiry-kept',
        behaviour=(
            'an observed hard expiry surviving a later refresh that omits the field: without it one response without the field erases the observation'
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('                response.refresh_token_hard_expires_at or self.refresh_token_hard_expires_at\n', '                response.refresh_token_hard_expires_at\n'),
        ),
        families=('tests/test_expiry_clocks.py',),
    ),
    Mutation(
        id='store-import-has-no-bound',
        behaviour=(
            "deriving the five-year upper bound only from a connect: an imported grant's age is unknown, and a bound from the import date would be later than the truth"
        ),
        path=f"{SRC}/store.py",
        edits=(
            ('        if self.grant_origin == "connect":\n', '        if self.grant_origin in ("connect", "import", "unknown"):\n'),
        ),
        families=('tests/test_expiry_clocks.py',),
    ),
    Mutation(
        id='ap-feature-fault-only',
        behaviour=(
            "only the feature fault (5030) meaning 'no accounts payable': without it a subscription fault, a throttle or any refused BillPayment read records the entity as having no AP"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            ('                    if exc.fault_code != ACCOUNTS_PAYABLE_FEATURE_FAULT:\n                        raise\n', ''),
        ),
        families=('tests/test_accounts_payable.py',),
    ),
    Mutation(
        id='ap-not-retried-once-known',
        behaviour=(
            'a BillPayment read refused with the feature fault not being retried for that consent'
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            ('            if accounts_payable == AP_UNAVAILABLE:\n', '            if False:\n'),
        ),
        families=('tests/test_accounts_payable.py',),
    ),
    Mutation(
        id='ap-cached-in-process',
        behaviour=(
            'the in-process accounts-payable cache, under the consent: without it a known-unavailable feature is re-read on every summary and list_entities never learns it'
        ),
        path=f"{SRC}/session.py",
        edits=(
            (
                '        self._states[entity.key] = replace(state, capabilities=updated)\n\n    def cached_capabilities(',
                '\n    def cached_capabilities(',
            ),
        ),
        families=('tests/test_accounts_payable.py',),
    ),
    Mutation(
        id='ap-unknown-until-first-use',
        behaviour=(
            'accounts payable starting as an explicit UNKNOWN: planted as available, a Simple Start company reads as having AP until its first BillPayment read'
        ),
        path=f"{SRC}/capabilities.py",
        edits=(
            ('    accounts_payable: str = AP_UNKNOWN\n', '    accounts_payable: str = AP_AVAILABLE\n'),
        ),
        families=('tests/test_accounts_payable.py',),
    ),
    Mutation(
        id='projection-line-tax-code',
        behaviour=(
            'the line-level tax code reference surviving projection'
        ),
        path=f"{SRC}/projections.py",
        edits=(
            ('_TAX_CODE_REF: Final = Nested("TaxCodeRef", "tax_code", _REF_SPEC)\n', '_TAX_CODE_REF: Final = Nested("TaxCodeRef", "tax_code", ())\n'),
        ),
        families=('tests/test_detector_canaries.py',),
    ),
    Mutation(
        id='projection-txn-tax-code',
        behaviour=(
            'the transaction-level tax code reference surviving projection'
        ),
        path=f"{SRC}/projections.py",
        edits=(
            ('        Nested("TxnTaxCodeRef", "txn_tax_code", _REF_SPEC),\n', ''),
        ),
        families=('tests/test_detector_canaries.py',),
    ),
    Mutation(
        id='server-info-credential-use',
        behaviour=(
            'server_info reporting whether credential use is enabled from the actual configuration'
        ),
        path=f"{SRC}/tools.py",
        edits=(
            ('            credential_use_enabled = settings.credentials() is not None\n', '            credential_use_enabled = True\n'),
        ),
        families=('tests/test_app_credentials_required.py',),
    ),
    Mutation(
        id="control-guard-names-match",
        behaviour=(
            "the guard's exact name comparison, whose detectors are known to "
            "be alive. If this one is not caught, the gate is broken rather "
            "than the tree being clean."
        ),
        path=f"{SRC}/guard.py",
        edits=(("    return registry_name == intuit_name", "    return True"),),
        families=("tests/test_guard.py",),
        control=True,
    ),
    Mutation(
        id="oauth-hard-expiry-opt-in",
        behaviour=(
            "sending the hard-expiry opt-in header on exchange and refresh: "
            "without it every realm's five-year clock reads unknown forever"
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            (
                "            headers[HARD_EXPIRY_OPT_IN_HEADER] = HARD_EXPIRY_OPT_IN_VALUE\n",
                "            pass\n",
            ),
        ),
        families=("tests/test_hard_expiry_opt_in.py",),
    ),
    Mutation(
        id="free-text-disclosure",
        behaviour=(
            "the data-use disclosure on a tool that returns company free text: "
            "without it a reader of get_transaction is told neither where the "
            "result goes nor that its memo is untrusted input"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "            \"Read-only; requires an explicit 'entity'.\"\n"
                "            + FREE_TEXT_DISCLOSURE\n"
                "        ),\n"
                "    )\n"
                "    def get_transaction(",
                "            \"Read-only; requires an explicit 'entity'.\"\n"
                "        ),\n"
                "    )\n"
                "    def get_transaction(",
            ),
        ),
        families=("tests/test_disclosures.py",),
    ),
    # -- the report parser on live shapes, and the four named report tools -----
    Mutation(
        id="report-leaf-flattening",
        behaviour=(
            "flattening nested column groups to leaves before pairing cells: "
            "without it ItemSales' eight-cell rows are all long rows, the row rule "
            "withholds every one, and the tool returns an empty partial report"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                '            children = item.get("Columns")\n            if children is not None:\n',
                "            children = None\n            if children is not None:\n",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="report-period-column-admission",
        behaviour=(
            "admitting a Money column with StartDate/EndDate metadata by shape: "
            "without it every 'Jan 2026' column of a summarised P&L is excluded and "
            "the report comes back as labels with no amounts"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "        elif column.leaf_key is not None and shape.period_columns and column.is_period:\n",
                "        elif False:\n",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="report-label-column-admission",
        behaviour=(
            "admitting a keyless first column of a label type on a hierarchical "
            "report: without it the aging summaries lose the vendor or "
            "customer every row is about"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "        elif column.leaf_key is None and column.synthetic_key:\n",
                "        elif False:\n",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="report-empty-report-absent-type",
        behaviour=(
            "the empty-report rule for an absent ColType: without it an empty "
            "InventoryValuationDetail is nine unrecognised-type anomalies and a "
            "PARTIAL result about nothing"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                '        if column.type == "" and row_count == 0:\n',
                "        if False:\n",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="report-quantity-type",
        behaviour=(
            "recognising Quantity as numeric-not-money: without it every "
            "InventoryValuationDetail with rows is partial for a type the live API sends"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                'QUANTITY_COLUMN_TYPES: Final[frozenset[str]] = frozenset({"Quantity"})',
                "QUANTITY_COLUMN_TYPES: Final[frozenset[str]] = frozenset()",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="client-fault-5020",
        behaviour=(
            "the fixed hint for fault 5020: without it a report the connection "
            "cannot run is a generic 400 with no code and no action"
        ),
        path=f"{SRC}/client.py",
        edits=(('    "5020": (\n', '    "5020-disabled": (\n'),),
        families=("tests/test_report_shapes.py", "tests/test_report_tools.py"),
    ),
    Mutation(
        id="client-report-endpoint-label",
        behaviour=(
            "labelling every report endpoint accounting/reports in errors: without "
            "it a new report's error says endpoint=accounting, which hides that a "
            "report was involved"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                '    if tail.startswith(REPORT_PREFIX):\n        return "accounting/reports"\n',
                "",
            ),
        ),
        families=("tests/test_report_shapes.py", "tests/test_report_tools.py"),
    ),
    Mutation(
        id="report-pnl-macro-exclusive",
        behaviour=(
            "report_profit_and_loss refusing a date_macro together with explicit "
            "dates: without it both are sent and QuickBooks picks one silently"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                "            if date_macro is not None and (start_date is not None or end_date is not None):\n"
                "                raise QueryError(_EXCLUSIVE_TEXT)\n",
                "            if False:\n                raise QueryError(_EXCLUSIVE_TEXT)\n",
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="report-bs-days-window",
        behaviour=(
            "report_balance_sheet refusing summarize_column_by=Days without a window "
            "or over 200 days: without it the excess columns fold into Others"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                '            _check_days_window(summarize_column_by, check_start, check_end, end_name="as_of")\n',
                "            pass\n",
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="report-columns-cap",
        behaviour=(
            "refusing columns lists of 25 or more on report_general_ledger and the "
            "aging details: without it the request goes out to time out upstream"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(("    if len(columns) >= MAX_COLUMNS:\n", "    if False:\n"),),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="report-aging-ap-detail-no-aging-method",
        behaviour=(
            "report_aging refusing aging_method on the payables detail (one of the "
            "four allowlists): without it a parameter the endpoint's inventory does "
            "not list is sent and silently discarded"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                '                if side == "payables" and aging_method is not None:\n'
                '                    refuse("aging_method")\n',
                "                pass\n",
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="report-aging-summary-no-accounting-method",
        behaviour=(
            "report_aging refusing accounting_method on a summary: without it the "
            "parameter is dropped rather than refused, and a caller who asked for "
            "cash basis gets accrual under a cash label"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                '                    ("num_periods", num_periods), ("columns", columns),\n'
                '                    ("accounting_method", accounting_method),\n',
                '                    ("num_periods", num_periods), ("columns", columns),\n',
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="report-inventory-backstop",
        behaviour=(
            "the send-side inventory check behind every report tool: without it a "
            "future edit can send a parameter the inventory does not list"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                "    foreign = sorted(key for key in params if key not in inventory)\n",
                "    foreign: list[str] = []\n",
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="report-class-filter-capability",
        behaviour=(
            "refusing a class filter the company cannot track: without it the "
            "filter is sent, QuickBooks ignores it, and the whole company comes "
            "back under the filter's name"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                '        _capabilities(capabilities, entity_key).require("class_tracking", entity_key)\n',
                "        pass\n",
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="report-department-filter-capability",
        behaviour="refusing a department filter the company cannot track (as above)",
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                '        _capabilities(capabilities, entity_key).require("department_tracking", entity_key)\n',
                "        pass\n",
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="report-gl-six-month-window",
        behaviour=(
            "the six-calendar-month limit on report_general_ledger: without "
            "it a year-wide ledger is requested and reported as the year"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                '            _check_window(start, end, start_name="start_date", end_name="end_date", months=MAX_WINDOW_MONTHS)\n',
                '            _check_window(start, end, start_name="start_date", end_name="end_date", months=None)\n',
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="report-aging-detail-six-month-window",
        behaviour="the six-calendar-month limit on the aging details' due-date window",
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                '                    due_start, due_end, start_name="start_duedate", end_name="end_duedate",\n'
                "                    months=MAX_WINDOW_MONTHS,\n",
                '                    due_start, due_end, start_name="start_duedate", end_name="end_duedate",\n'
                "                    months=None,\n",
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="report-aging-ap-capability",
        behaviour=(
            "report_aging(payables) refusing before any request once accounts "
            "payable is known unavailable: without it every call asks "
            "QuickBooks again for a feature it has already refused"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(("                if ap_state == AP_UNAVAILABLE:\n", "                if False:\n"),),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="netpolicy-report-tail-keys",
        behaviour=(
            "the per-report query-key allowlist at the network boundary: without "
            "it a parameter the inventory lists but no tool exposes "
            "(qzurl on the profit and loss) reaches Intuit"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                '    "reports/ProfitAndLoss": frozenset(\n'
                "        {\n"
                '            "minorversion", "start_date", "end_date", "date_macro",\n',
                '    "reports/ProfitAndLoss": frozenset(\n'
                "        {\n"
                '            "minorversion", "start_date", "end_date", "date_macro", "qzurl",\n',
            ),
        ),
        families=("tests/test_network_policy.py", "tests/test_report_tools.py"),
    ),
    # -- revoke, redaction and sanitizer edge cases ---------------------------
    Mutation(
        id="store-revoke-absent-row-epoch",
        behaviour=(
            "the epoch advance on a revoke against an ABSENT row: without it a "
            "ceremony that began with no row and was revoked mid-flight commits "
            "afterwards, because the store looks exactly as it found it"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            row = document.rows.pop(realm_id, None)\n"
                "            document.malformed.pop(realm_id, None)\n"
                "            # Present row, malformed row or no row at all: the realm's consent\n"
                "            # identity has been cancelled, and the cancellation is persisted.\n"
                "            document.advance_epoch(realm_id)\n"
                "            self._commit_locked(document)\n"
                "            self._drop_journal_records_locked(realm_id)\n"
                "            return row\n",
                "            row = document.rows.pop(realm_id, None)\n"
                "            broken = document.malformed.pop(realm_id, None)\n"
                "            if row is not None or broken is not None:\n"
                "                document.advance_epoch(realm_id)\n"
                "                self._commit_locked(document)\n"
                "                self._drop_journal_records_locked(realm_id)\n"
                "            return row\n",
            ),
        ),
        families=("tests/test_store_lifecycle.py",),
    ),
    Mutation(
        id="cli-revoke-absent-row-cancellation",
        behaviour=(
            "the CLI's store call on a revoke with nothing stored: without it "
            "'auth revoke' prints 'nothing stored' and a consent ceremony begun "
            "before it commits afterwards"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                '            out(f"{entity.key}: nothing stored")\n'
                "            try:\n"
                "                arrived = store.revoke_row(entity.realm_id)\n",
                '            out(f"{entity.key}: nothing stored")\n'
                "            try:\n"
                "                arrived = None\n",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="redaction-exact-identity",
        behaviour=(
            "the exact identity check in redact(): without it a held value "
            "under the text-scan floor is returned unchanged although holds() "
            "answers true for it (identity is exact at any length)"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                "        if live_values.holds(text):\n            return REDACTED\n",
                "",
            ),
        ),
        families=("tests/test_live_values.py",),
    ),
    Mutation(
        id="sanitizer-key-gstin",
        behaviour=(
            "one key rule of the fixture sanitizer: with GSTIN gone from "
            "REDACT_KEYS a planted value passes through"
        ),
        path="tests/fixtures/sanitize.py",
        edits=(
            (
                '        "EmployerId", "SSN", "EIN", "GSTIN", "TaxRegistrationNumber",\n',
                '        "EmployerId", "SSN", "EIN", "TaxRegistrationNumber",\n',
            ),
        ),
        families=("tests/test_fixture_sanitizer.py",),
    ),
    # -- grouping columns, the date-macro resolver ----------------------------
    Mutation(
        id="report-grouping-column-admission",
        behaviour=(
            "admitting a Money column keyed by an entity id under an entity "
            "grouping: without it a P&L by Customers comes back as labels "
            "with no amounts, every id column excluded and named"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "        elif column.leaf_key is not None and shape.period_columns and column.is_grouping:\n",
                "        elif False:\n",
            ),
        ),
        families=("tests/test_report_shapes.py", "tests/test_report_tools.py"),
    ),
    Mutation(
        id="report-grouping-repeat-is-total",
        behaviour=(
            "reading a repeated entity key under a grouping as that entity's total "
            "column: without it a Customers grouping fails as structural"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "                if entity_id is not None and (own_key,) in seen:\n",
                "                if False:\n",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="report-macro-resolved-window",
        behaviour=(
            "feeding the RESOLVED macro window to the Days check: without "
            "it Days with a macro is judged against no window at all and a "
            "year-long macro goes out to fold into Others"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                "    if window is None:\n        return start, end\n    return window.start, window.end\n",
                "    return start, end\n",
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="report-bs-as-of-or-macro",
        behaviour=(
            "report_balance_sheet requiring exactly one of as_of and date_macro: "
            "without it both are sent and QuickBooks picks one silently, "
            "or neither is and the default window is reported as the one asked for"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                "            if (as_of is None) == (date_macro is None):\n"
                "                raise QueryError(_BS_ONE_OF_TEXT if as_of is None else _EXCLUSIVE_TEXT)\n",
                "            if False:\n"
                "                raise QueryError(_BS_ONE_OF_TEXT if as_of is None else _EXCLUSIVE_TEXT)\n",
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    Mutation(
        id="capabilities-fiscal-year-start",
        behaviour=(
            "deriving the fiscal year start from Preferences: without it every "
            "fiscal macro is refused as unresolvable, or -- if a default were "
            "invented -- resolved against a January nobody stated"
        ),
        path=f"{SRC}/capabilities.py",
        edits=(
            (
                '        fiscal_year_start_month=fiscal_start_month(accounting.get("fiscal_year_start_month")),\n',
                "        fiscal_year_start_month=None,\n",
            ),
        ),
        families=("tests/test_report_tools.py",),
    ),
    # -- run_report -----------------------------------------------------------
    Mutation(
        id="run-report-parameter-allowlist",
        behaviour=(
            "run_report refusing a parameter its report's spec does not list, by "
            "name, before any request: without it accounting_method goes to "
            "CashFlow and columns to TrialBalance (neither is exempt)"
        ),
        path=f"{SRC}/tools_run_report.py",
        edits=(
            (
                "                if value is not None and name not in spec.parameters:\n"
                "                    _refuse(name, report, endpoint)\n",
                "                pass\n",
            ),
        ),
        families=("tests/test_run_report.py",),
    ),
    Mutation(
        id="run-report-detail-six-month-window",
        behaviour=(
            "the six-calendar-month limit on run_report's detail members: "
            "without it a year-wide ProfitAndLossDetail goes out and is reported "
            "as the year"
        ),
        path=f"{SRC}/tools_run_report.py",
        edits=(("            months = MAX_WINDOW_MONTHS if spec.detail else None\n", "            months = None\n"),),
        families=("tests/test_run_report.py",),
    ),
    Mutation(
        id="run-report-detail-window-required",
        behaviour=(
            "a detail member requiring a window at all: without it a detail report "
            "with no dates and no macro goes out to QuickBooks's default window, "
            "which this server never validated"
        ),
        path=f"{SRC}/tools_run_report.py",
        edits=(
            (
                "                if spec.detail and start is None and date_macro is None:\n",
                "                if False:\n",
            ),
        ),
        families=("tests/test_run_report.py",),
    ),
    Mutation(
        id="run-report-macro-six-month-check",
        behaviour=(
            "the resolved macro being held to the six-month limit on a detail "
            "member: without it 'This Fiscal Year' goes out on JournalReport while "
            "explicit dates for the same year are refused"
        ),
        path=f"{SRC}/tools_run_report.py",
        edits=(
            (
                "                if spec.detail:\n"
                '                    _check_window(check_start, check_end, start_name="start_date", end_name="end_date", months=months)\n',
                "                pass\n",
            ),
        ),
        families=("tests/test_run_report.py",),
    ),
    Mutation(
        id="run-report-per-report-macros",
        behaviour=(
            "each report admitting only the date_macro values its Intuit page "
            "lists: without it a Calendar macro goes out on a report whose page "
            "lists twenty-three"
        ),
        path=f"{SRC}/tools_run_report.py",
        edits=(
            (
                '            _check_enum(date_macro, spec.date_macros or ALL_DATE_MACROS, "date_macro")\n',
                '            _check_enum(date_macro, ALL_DATE_MACROS, "date_macro")\n',
            ),
        ),
        families=("tests/test_run_report.py",),
    ),
    Mutation(
        id="run-report-taxsummary-agency-id",
        behaviour=(
            "TaxSummary requiring agency_id, the one parameter Intuit marks "
            "Required on a report: without it the request goes out to fail "
            "upstream, or to answer for an agency nobody named"
        ),
        path=f"{SRC}/tools_run_report.py",
        edits=(
            (
                '            if "agency_id" in spec.parameters and not (agency_id or "").strip():\n',
                "            if False:\n",
            ),
        ),
        families=("tests/test_run_report.py",),
    ),
    Mutation(
        id="report-sensitive-column-gate",
        behaviour=(
            "the second gate behind the per-report allowlists: without it a "
            "sensitive column key that reaches an allowlist by mistake is "
            "published"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "        if probe in SENSITIVE_COLUMN_KEYS or probe.casefold() in SENSITIVE_COLUMN_KEYS:\n"
                "            excluded.append(column.key)\n            continue\n",
                "",
            ),
            # Plant the mistake the gate exists to catch: one sensitive key on
            # one allowlist. The gate then has to be what refuses it.
            (
                '        frozenset({"tx_date", "txn_type", "doc_num", "is_no_post", "memo", "account_name", "amount",\n',
                '        frozenset({"tx_date", "txn_type", "doc_num", "is_no_post", "memo", "account_name", "amount", "cust_msg",\n',
            ),
        ),
        families=("tests/test_run_report.py",),
    ),
    Mutation(
        id="report-titled-money-admission",
        behaviour=(
            "admitting TrialBalance's keyless Debit and Credit columns by title: "
            "without it the trial balance comes back as account names with no "
            "amounts"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                '                leaf = shape.titled_money[title or ""]\n'
                "                synthetic = True\n"
                '                admitted = "title"\n',
                '                leaf = f"column_{index}"\n',
            ),
        ),
        families=("tests/test_report_shapes.py", "tests/test_run_report.py"),
    ),
    Mutation(
        id="netpolicy-run-report-tail",
        behaviour=(
            "a run_report path on the network allowlist by exact name: without "
            "CashFlow there, run_report(CashFlow) is refused at the boundary, "
            "which is the direction the allowlist must fail in"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(('    "reports/CashFlow",\n', ""),),
        families=("tests/test_run_report.py",),
    ),
    # -- list_reference_data --------------------------------------------------
    Mutation(
        id="reference-projection-allowlist",
        behaviour=(
            "projecting every reference row through its allowlist: without it the "
            "raw QuickBooks row -- SyncToken, LastModifiedByRef, every field Intuit "
            "adds later -- reaches the model"
        ),
        path=f"{SRC}/tools_reference.py",
        edits=(
            (
                "                    rows=[block(**project_reference(entity_name, row)) for row in walk.rows],\n",
                "                    rows=[block.model_construct(**row) for row in walk.rows],\n",
            ),
        ),
        families=("tests/test_reference_data.py",),
    ),
    Mutation(
        id="changes-cdc-unsupported-types",
        behaviour=(
            "list_changes refusing TaxCode and TaxRate, which change data capture "
            "does not cover: without it the request goes out for a type Intuit "
            "will not report on"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(("        if match in CDC_UNSUPPORTED:\n", "        if False:\n"),),
        families=("tests/test_reference_data.py",),
    ),
    Mutation(
        id="reference-feature-note",
        behaviour=(
            "the note on an empty list from a company whose Preferences turn the "
            "feature off: without it 'no classes' and 'this company does not "
            "track classes' read the same"
        ),
        path=f"{SRC}/tools_reference.py",
        edits=(
            (
                "    if enabled:\n        return None\n    return (\n",
                "    if True:\n        return None\n    return (\n",
            ),
        ),
        families=("tests/test_reference_data.py",),
    ),
    # -- list_attachments and the get_attachment gate -------------------------
    Mutation(
        id="attachments-no-credential-headers",
        behaviour=(
            "the attachment class refusing a request that carries a bearer, a "
            "cookie or a proxy authorization: without it this app's credential "
            "rides to a storage host"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                "    # No bearer, no cookies, no proxy auth: attaching this app's bearer token\n"
                "    # to a storage link would hand the credential to a third party.\n"
                "    _check_no_credentials(request)\n"
                "    if not ctx.attachment_hosts:\n",
                "    if not ctx.attachment_hosts:\n",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="attachments-host-pinned",
        behaviour=(
            "the explicit host policy on the download origin and on every redirect "
            "destination: without it a TempDownloadUri -- or a Location header -- "
            "naming any host is contacted"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(("    if not _host_pinned(host, ctx.attachment_hosts):\n", "    if False:\n"),),
        families=("tests/test_attachments.py", "tests/test_network_policy.py"),
    ),
    Mutation(
        id="attachments-ip-literal-refused",
        behaviour=(
            "an IP literal never being a pinned download host outside the loopback "
            "test mode: without it a link to an address is contacted if the "
            "operator pinned that address"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(("    if literal is not None and not loopback_mock:\n", "    if False:\n"),),
        families=("tests/test_attachments.py", "tests/test_network_policy.py"),
    ),
    Mutation(
        id="attachments-destination-precheck",
        behaviour=(
            "the private-destination pre-check on a pinned host: without it a "
            "pinned name that resolves to a private, loopback or link-local "
            "address is contacted"
        ),
        path=f"{SRC}/attachments.py",
        edits=(
            (
                '            check_destination(request.url.host or "", settings, resolve)\n',
                "            pass\n",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachments-mime-allowlist",
        behaviour=(
            "refusing an unsupported recorded content type before any request: "
            "without it an archive or an executable is fetched"
        ),
        path=f"{SRC}/attachments.py",
        edits=(("    if normalised not in MIME_ALLOWLIST:\n", "    if False:\n"),),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachments-mime-signature",
        behaviour=(
            "checking the downloaded bytes against the recorded type's own "
            "signature: without it a file that says PDF and is not is returned"
        ),
        path=f"{SRC}/attachments.py",
        edits=(("            if not MIME_ALLOWLIST[content_type](content):\n", "            if False:\n"),),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachments-response-type-agrees",
        behaviour=(
            "requiring the response's content type to agree with the recorded one: "
            "without it the bytes are judged against whatever the storage host says"
        ),
        path=f"{SRC}/attachments.py",
        edits=(("            if served != content_type:\n", "            if False:\n"),),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachments-size-metadata",
        behaviour=(
            "refusing by the recorded size before the request: without it an "
            "oversized file is fetched up to the cap and then refused, spending "
            "the bandwidth"
        ),
        path=f"{SRC}/attachments.py",
        edits=(
            (
                "    if expected_size is not None and expected_size > MAX_ATTACHMENT_RAW_BYTES:\n",
                "    if False:\n",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachments-size-stream-cap",
        behaviour=(
            "the raw cap on the streamed body: without it a file whose recorded "
            "size was understated is read whole, and the base64 blob exceeds the "
            "final MCP output cap"
        ),
        path=f"{SRC}/attachments.py",
        edits=(
            (
                "                    client, request, ctx, max_bytes=MAX_ATTACHMENT_RAW_BYTES, what=\"attachment\",\n",
                "                    client, request, ctx, max_bytes=10 ** 9, what=\"attachment\",\n",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachments-redirect-hop-limit",
        behaviour=(
            "the bound on redirect hops: without it a redirect loop is followed "
            "until something else gives"
        ),
        path=f"{SRC}/attachments.py",
        edits=(("                if hops > MAX_REDIRECTS:\n", "                if False:\n"),),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachments-trust-env-off",
        behaviour=(
            "the download transport reading nothing from the environment: without it "
            "a CA file SSL_CERT_FILE names decides whom a signed download link is "
            "handed to, and SSLKEYLOGFILE records the session (the "
            "client's own trust_env=False does not reach this transport, which "
            "httpx would otherwise build with its defaults)"
        ),
        path=f"{SRC}/attachments.py",
        edits=(("    return isolated_transport()\n", "    return httpx.HTTPTransport(retries=0)\n"),),
        families=("tests/test_client_environment.py", "tests/test_attachments.py"),
    ),
    Mutation(
        id="attachments-opt-in-registration",
        behaviour=(
            "get_attachment registering only when documents are enabled: without "
            "it the opt-in is on every server's default surface"
        ),
        path=f"{SRC}/tools_attachments.py",
        edits=(("    if not settings.allow_documents:\n        return\n", ""),),
        families=("tests/test_attachments.py", "tests/test_disclosures.py"),
    ),
    Mutation(
        id="attachments-no-link-projection",
        behaviour=(
            "the Attachable projection excluding the temporary download link: "
            "without it list_attachments hands out a signed storage URL"
        ),
        path=f"{SRC}/projections.py",
        edits=(
            (
                '    Scalar("Tag", "tag", as_str),\n    ListOf("AttachableRef", "refs", (\n',
                '    Scalar("Tag", "tag", as_str),\n    Scalar("TempDownloadUri", "tag", as_str),\n    ListOf("AttachableRef", "refs", (\n',
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    # ---- percent columns declared Money (observed live)
    Mutation(
        id="report-percent-unit",
        behaviour=(
            "reading a percent column's '30.69 %' as an exact decimal with unit "
            "percent: without it every SalesByProduct result is PARTIAL with one "
            "malformed_amount per row, the shape both sandbox companies showed live"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "    if column.is_percent and text is not None:\n",
                "    if False and text is not None:\n",
            ),
        ),
        families=("tests/test_report_shapes.py", "tests/test_run_report.py"),
    ),
    Mutation(
        id="report-percent-strict",
        behaviour=(
            "refusing a bare number in a percent column: without it '30.69' with no "
            "sign is silently read as a percent it may not be, instead of disclosed"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "    if not candidate.endswith(PERCENT_TITLE_MARK):\n        return None\n",
                "    if not candidate.endswith(PERCENT_TITLE_MARK):\n        return _exact_decimal(candidate)\n",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="report-percent-identification-by-title",
        behaviour=(
            "identifying a percent column by a '%' in its title as well as by its key "
            "suffix: without it a percent column under a key this package has not "
            "seen is read as money and every cell is an anomaly"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                '        return not self.is_grouping and PERCENT_TITLE_MARK in (self.title or "")\n',
                "        return False\n",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="report-percent-no-currency",
        behaviour=(
            "a percent cell carrying no currency: without it '30.69 %' is published "
            "as 30.69 USD, a money figure it is not"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "        currency=currency if (amount is not None and column.is_money and unit is None) else None,\n",
                "        currency=currency if (amount is not None and column.is_money) else None,\n",
            ),
        ),
        families=("tests/test_report_shapes.py", "tests/test_run_report.py"),
    ),
    Mutation(
        id="report-percent-not-totalled",
        behaviour=(
            "computed_total refusing a percent column: without it percentages of a "
            "whole are summed into a figure that means nothing"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                '    if column.is_percent:\n        return unknown("the column is a percent column; percentages are not summed")\n',
                '    if False:\n        return unknown("the column is a percent column; percentages are not summed")\n',
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    # ---- "Next 4 Weeks" as QuickBooks resolves it (observed live)
    Mutation(
        id="report-macro-next-four-weeks-anchor",
        behaviour=(
            "resolving 'Next 4 Weeks' from the first day of the next week, as "
            "QuickBooks did live: without it the window is today plus 28 days, "
            "two days off the window QuickBooks reports, and the disclosed "
            "resolution misstates what was validated"
        ),
        path=f"{SRC}/date_macros.py",
        edits=(
            (
                "        next_week = _week_start(today) + timedelta(days=7)\n",
                "        next_week = today\n",
            ),
        ),
        families=("tests/test_date_macros.py",),
    ),
    # ---- the observed storage host is named, never pinned by default
    Mutation(
        id="attachments-no-default-pin",
        behaviour=(
            "no storage host pinned unless the operator pins one: without it the "
            "host observed in sandbox is contacted by default, in production too, "
            "though the operator never chose it"
        ),
        path=f"{SRC}/config.py",
        edits=(
            (
                "    attachment_hosts = tuple(dict.fromkeys(h.strip().lower() for h in hosts_raw if h.strip()))\n",
                "    attachment_hosts = tuple(dict.fromkeys(h.strip().lower() for h in hosts_raw if h.strip())) or (\n"
                '        "financialdocument.platform.intuit.com",\n'
                "    )\n",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="report-percent-not-grouping-title",
        behaviour=(
            "never treating an entity grouping's column as a percent column by its "
            "title: without it a customer, vendor or item whose NAME carries '%' "
            "turns every amount in its column into a malformed_amount, a false "
            "PARTIAL caused by user data"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                '        return not self.is_grouping and PERCENT_TITLE_MARK in (self.title or "")\n',
                '        return PERCENT_TITLE_MARK in (self.title or "")\n',
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    # ---- list_transactions' default columns; the cap counts the request
    Mutation(
        id="list-transactions-default-nine",
        behaviour=(
            "list_transactions asking for QuickBooks's own nine default columns when "
            "the caller names none: without it every default call requests all 26 "
            "allowlisted keys, over the 25-column limit, and a report 504 is never "
            "retried"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                '                columns, "TransactionList", TRANSACTION_LIST_DEFAULT_COLUMNS\n',
                '                columns, "TransactionList", reports_mod.requestable_columns("TransactionList")\n',
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="report-columns-cap-counts-request",
        behaviour=(
            "counting the routing columns toward the 25-column limit: without it 23 "
            "keys that omit tx_date and txn_type go out as a 25-column request, on "
            "list_transactions and every report tool that takes columns"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(("    if len(requested) >= MAX_COLUMNS:\n", "    if False:\n"),),
        families=("tests/test_changes_and_tools.py", "tests/test_report_tools.py"),
    ),
    # ---- list_accounts takes three filters
    Mutation(
        id="list-accounts-active-filter",
        behaviour=(
            "list_accounts' active filter: without it every call walks active and "
            "inactive accounts alike, whatever the caller asked"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                '        "Active IN (true, false)" if active is None\n'
                "        else f\"Active = {'true' if active else 'false'}\"\n",
                '        "Active IN (true, false)"\n',
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="list-accounts-account-type-filter",
        behaviour=(
            "list_accounts' account_type filter on the walk's WHERE: without it a "
            "filtered call returns the whole chart of accounts under the filter's name"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                '        clauses.append(Condition("AccountType", "=", (account_type,), True).render())\n',
                "        pass\n",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="list-accounts-classification-filter",
        behaviour=(
            "list_accounts' classification filter on the walk's WHERE: without it a "
            "filtered call returns the whole chart of accounts under the filter's name"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                '        clauses.append(Condition("Classification", "=", (classification,), True).render())\n',
                "        pass\n",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    # ---- schema rules and tool annotations
    Mutation(
        id="tool-annotations-read-only",
        behaviour=(
            "readOnlyHint on every published tool: without it a client cannot "
            "treat any tool here as safe to call"
        ),
        path=f"{SRC}/tools.py",
        edits=(("        read_only_hint=True,\n", "        read_only_hint=False,\n"),),
        families=("tests/test_tool_schema.py",),
    ),
    Mutation(
        id="schema-entity-description",
        behaviour=(
            "the entity parameter's description on the seventeen tools that take "
            "it"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                'EntityKey = Annotated[str, Field(description="The company\'s key, from list_entities.")]\n',
                "EntityKey = str\n",
            ),
        ),
        families=("tests/test_tool_schema.py",),
    ),
    Mutation(
        id="schema-date-format",
        behaviour="format: date on every YYYY-MM-DD argument",
        path=f"{SRC}/tools.py",
        edits=(('IsoDate = Annotated[str, Field(json_schema_extra={"format": "date"})]\n', "IsoDate = str\n"),),
        families=("tests/test_tool_schema.py",),
    ),
    # ---- upstream errors name their company; intuit_tid rides along
    Mutation(
        id="upstream-error-names-entity",
        behaviour=(
            "an upstream error naming the entity and environment it was bound to: "
            "without it a model holding several companies cannot tell "
            "which one failed"
        ),
        path=f"{SRC}/errors.py",
        edits=(
            (
                '        f"QuickBooks request failed: {bound_identity(entity, environment)} "\n',
                '        f"QuickBooks request failed: "\n',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="throttle-names-entity",
        behaviour=(
            "a 429 naming the entity and environment it was bound to: without it "
            "the one upstream error built outside upstream_error_text names no company"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                '            raise UpstreamThrottled(\n'
                '                f"{THROTTLED_TEXT} ("\n'
                '                f"{bound_identity(entity.key, entity.environment, intuit_tid=intuit_tid_of(response))})"\n'
                '            )\n',
                "            raise UpstreamThrottled(THROTTLED_TEXT)\n",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="upstream-error-intuit-tid",
        behaviour=(
            "carrying Intuit's intuit_tid on a failed response: without it the "
            "support id Intuit asks integrators to log is dropped"
        ),
        path=f"{SRC}/client.py",
        edits=(('            "intuit_tid": intuit_tid_of(response),\n', '            "intuit_tid": None,\n'),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="throttle-intuit-tid",
        behaviour="carrying intuit_tid on a 429, which is a non-2xx response too",
        path=f"{SRC}/client.py",
        edits=(
            (
                "bound_identity(entity.key, entity.environment, intuit_tid=intuit_tid_of(response))",
                "bound_identity(entity.key, entity.environment, intuit_tid=None)",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="intuit-tid-shape",
        behaviour=(
            "carrying intuit_tid only in its documented shape: without it an "
            "upstream header value is quoted into a fixed-text error"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                "    return value if _INTUIT_TID_SHAPE.fullmatch(value) else None\n",
                "    return value or None\n",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="throttle-block-names-entity",
        behaviour=(
            "a recorded 429 block, refused before any request, naming its entity "
            "and environment like the 429 it records"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                "            raise UpstreamThrottled(\n"
                '                f"{THROTTLED_TEXT} ({bound_identity(entity.key, entity.environment)})"\n'
                "            ) from None\n",
                "            raise\n",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    # ---- token requests carry only the enumerated headers
    Mutation(
        id="token-headers-enumerated",
        behaviour=(
            'a token request carrying only the headers the OAuth builders send: without it a cookie, a method override or a proxy credential rides along'
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(('    if set(names) - allowed:\n        raise NetworkPolicyViolation("token request carries a header outside the enumerated set")\n', ''),),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="token-headers-opt-in-not-on-revoke",
        behaviour=(
            'the hard-expiry opt-in admitted only on the two calls that issue a token'
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(('    _check_token_headers(request, opt_in=False)\n', '    _check_token_headers(request, opt_in=True)\n'),),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="token-headers-fixed-values",
        behaviour=(
            "the builders' own values for Accept, User-Agent and the opt-in"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(('        if name in headers and headers[name] != value:\n', '        if False:\n'),),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="token-headers-basic-only",
        behaviour=(
            "a token request's credential being the app's Basic one, never a bearer"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(('    if "authorization" in headers and not headers["authorization"].lower().startswith("basic "):\n', '    if False:\n'),),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="token-headers-no-duplicates",
        behaviour=(
            'refusing a duplicated token-request header, as a duplicated body field is refused'
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(('    if len(names) != len(set(names)):\n', '    if False:\n'),),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="token-headers-host",
        behaviour=(
            "a token request's Host header matching its URL"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(('    if "host" in headers and headers["host"] != request.url.netloc.decode("ascii"):\n', '    if False:\n'),),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="token-headers-content-length",
        behaviour=(
            "a token request's Content-Length matching its body"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(('    if "content-length" in headers and headers["content-length"] != str(len(request.content)):\n', '    if False:\n'),),
        families=("tests/test_network_policy.py",),
    ),
    # ---- a store mutation that calls another does not wait on its own lock
    Mutation(
        id="store-lock-reentrant-mutex",
        behaviour=(
            "the reentrant in-process mutex: without it a store mutation that calls "
            "another waits on its own lock until the timeout"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            existing = _ProcessLock(mutex=threading.RLock())\n",
                "            existing = _ProcessLock(mutex=threading.Lock())\n",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    Mutation(
        id="store-lock-flock-once-per-thread",
        behaviour=(
            "taking the file lock once per thread's outermost mutation: without it "
            "a nested mutation's second flock on a second descriptor conflicts "
            "with the first, in the same process"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "        if state.depth == 0:\n"
                "            state.fd = _flock_exclusive(lock_path, max(0.0, expires - time.monotonic()))\n",
                "        state.fd = _flock_exclusive(lock_path, max(0.0, expires - time.monotonic()))\n",
            ),
        ),
        families=("tests/test_token_store.py",),
    ),
    # ---- an Intuit host only for its own environment
    Mutation(
        id="api-base-own-environment-startup",
        behaviour=(
            "refusing the other environment's Intuit host at startup: without it "
            "--api-base https://sandbox-quickbooks.api.intuit.com starts a "
            "production server that sends production tokens to the sandbox host"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                "        if candidate != API_BASES.get(environment):\n",
                "        if False:\n",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    Mutation(
        id="api-base-own-environment-send",
        behaviour=(
            "refusing the other environment's Intuit host at send time: without it "
            "a context carrying the crossed pair sends the bearer token, because "
            "both origins count as real"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                "    _refuse_other_environments_host(base, ctx.environment)\n",
                "",
            ),
        ),
        families=("tests/test_network_policy.py",),
    ),
    # ---- no caller value in any error text
    Mutation(
        id="sdk-argument-error-rebuilt",
        behaviour=(
            "rebuilding the SDK's argument refusal at the call boundary: without it "
            "pydantic's text reaches the client with input_value, a configured "
            "client secret included"
        ),
        path=f"{SRC}/server.py",
        edits=(
            (
                "                raise ToolError(\n"
                "                    argument_error_text(name, cause.errors(include_input=False), declared)\n"
                "                ) from None\n",
                "                raise\n",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="unknown-tool-name-not-repeated",
        behaviour="an unknown tool's name, caller-supplied, not repeated in the refusal",
        path=f"{SRC}/server.py",
        edits=(("                raise ToolError(UNKNOWN_TOOL_TEXT) from None\n", "                raise\n"),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="argument-error-declared-parameters-only",
        behaviour=(
            "naming a location only through a parameter the tool declares: without "
            "it a location outside them, which a caller chose, is repeated"
        ),
        path=f"{SRC}/errors.py",
        edits=(("        if isinstance(head, str) and head in declared:\n", "        if isinstance(head, str):\n"),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="argument-error-no-dict-keys",
        behaviour=(
            "keeping only list positions after the parameter: without it a dict key "
            "in a location, the caller's own text, is repeated"
        ),
        path=f"{SRC}/errors.py",
        edits=(("                if isinstance(part, int) and not isinstance(part, bool)\n", "                if True\n"),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="argument-error-vetted-messages",
        behaviour=(
            "reusing pydantic's message only for the vetted schema-derived types: "
            "without it a validator's own message, which may quote the value, is repeated"
        ),
        path=f"{SRC}/errors.py",
        edits=(
            (
                '        reason = str(error.get("msg", "")) if error.get("type") in _VETTED_MESSAGE_TYPES else _UNVETTED_REASON\n',
                '        reason = str(error.get("msg", ""))\n',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="schema-enums-published",
        behaviour=(
            "every enum published through json_schema_extra: without "
            "it tools/list loses the vocabularies a client reads"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                '    return Annotated[str, Field(json_schema_extra={"enum": list(values)})]\n',
                "    return Annotated[str, Field()]\n",
            ),
        ),
        families=("tests/test_tool_schema.py",),
    ),
    Mutation(
        id="enum-refused-in-the-tool",
        behaviour=(
            "the tool-side enum refusal, the only check on it: without it a value "
            "outside an enum reaches QuickBooks"
        ),
        path=f"{SRC}/tools.py",
        edits=(("    if value is not None and value not in allowed:\n", "    if False:\n"),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="list-accounts-account-type-refused",
        behaviour="list_accounts refusing an account_type outside its enum before any request",
        path=f"{SRC}/tools.py",
        edits=(('            check_enum(account_type, ACCOUNT_TYPES, "account_type")\n', ""),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="list-accounts-classification-refused",
        behaviour="list_accounts refusing a classification outside its enum before any request",
        path=f"{SRC}/tools.py",
        edits=(('            check_enum(classification, ACCOUNT_CLASSIFICATIONS, "classification")\n', ""),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="list-transactions-transaction-type-refused",
        behaviour="list_transactions refusing a transaction_type outside its enum before any request",
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "            if transaction_type is not None and (\n"
                "                transaction_type not in reports_mod.REPORT_TRANSACTION_TYPES\n"
                "            ):\n",
                "            if False:\n",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="list-transactions-source-account-type-refused",
        behaviour="list_transactions refusing a source_account_type outside its enum before any request",
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "            if source_account_type is not None and (\n"
                "                source_account_type not in reports_mod.REPORT_SOURCE_ACCOUNT_TYPES\n"
                "            ):\n",
                "            if False:\n",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="run-report-report-refused",
        behaviour="run_report refusing a report outside its enum with fixed text",
        path=f"{SRC}/tools_run_report.py",
        edits=(("    if report in CATALOG:\n        return\n", "    if True:\n        return\n"),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="reference-kind-refused",
        behaviour="list_reference_data refusing a kind outside its enum instead of walking a default",
        path=f"{SRC}/tools_reference.py",
        edits=(("        return KINDS[kind]\n", '        return KINDS.get(kind, KINDS["items"])\n'),),
        families=("tests/test_error_boundary.py",),
    ),
    # ---- quantity columns are quantities
    Mutation(
        id="report-quantity-by-key",
        behaviour=(
            "a column keyed Quantity being a quantity whatever its declared type: "
            "without it InventoryValuationSummary and SalesByProduct publish ten "
            "units as ten dollars"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "        return self.type in QUANTITY_COLUMN_TYPES or (self.leaf_key or self.key) in QUANTITY_COLUMN_KEYS\n",
                "        return self.type in QUANTITY_COLUMN_TYPES\n",
            ),
        ),
        families=("tests/test_report_shapes.py", "tests/test_run_report.py"),
    ),
    Mutation(
        id="report-quantity-not-money",
        behaviour=(
            "a quantity column not being money: without it a Money-typed quantity "
            "carries a currency and enters a computed money total"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "        return self.type in MONEY_COLUMN_TYPES and not self.is_quantity\n",
                "        return self.type in MONEY_COLUMN_TYPES\n",
            ),
        ),
        families=("tests/test_report_shapes.py", "tests/test_run_report.py"),
    ),
    # ---- a local trim is known loss
    Mutation(
        id="list-transactions-trim-partial",
        behaviour=(
            "list_transactions calling a locally trimmed result partial: without it "
            "client_truncated is true while completeness says unknown"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                '            completeness = "partial" if (flat.anomalies or flat.client_truncated) else "unknown"\n',
                '            completeness = "partial" if flat.anomalies else "unknown"\n',
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="report-summary-names-trim",
        behaviour=(
            "the first sentence saying how many rows max_rows withheld: without it a "
            "trimmed result's summary reads like the whole report"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "    withheld = report.upstream_row_count - rows if report.client_truncated else 0\n",
                "    withheld = 0\n",
            ),
        ),
        families=("tests/test_report_flattener.py", "tests/test_changes_and_tools.py"),
    ),
    # ---- a download must be the recorded size
    Mutation(
        id="attachments-size-exact",
        behaviour=(
            "refusing a body whose length differs from the recorded Size: without it "
            "a truncated or altered file with a valid signature is returned as the "
            "recorded one"
        ),
        path=f"{SRC}/attachments.py",
        edits=(("            if expected_size is not None and len(content) != expected_size:\n", "            if False:\n"),),
        families=("tests/test_attachments.py",),
    ),
    # ---- keepalive --require-all
    Mutation(
        id="keepalive-require-all",
        behaviour=(
            "--require-all counting a configured company with no credential as "
            "needing attention: without it the run exits 0 while that company can "
            "answer nothing"
        ),
        path=f"{SRC}/keepalive.py",
        edits=(("            if require_all:\n", "            if False:\n"),),
        families=("tests/test_keepalive.py",),
    ),
    # ---- report id filters take lists
    Mutation(
        id="report-id-filters-are-lists",
        behaviour=(
            "every report id filter taking a list of ids: without it "
            "the filters are unvalidated strings and a caller's list is refused"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                "IdList = Annotated[list[Annotated[str, Field(pattern=ID_PATTERN)]], Field(min_length=1)]\n",
                "IdList = str\n",
            ),
        ),
        families=("tests/test_tool_schema.py", "tests/test_report_tools.py"),
    ),
    Mutation(
        id="report-id-filter-each-validated",
        behaviour=(
            "each element of an id filter validated as an id: without it a comma or "
            "a space inside one element turns one id into two once the list is joined"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                "IdList = Annotated[list[Annotated[str, Field(pattern=ID_PATTERN)]], Field(min_length=1)]\n",
                "IdList = Annotated[list[str], Field(min_length=1)]\n",
            ),
        ),
        families=("tests/test_tool_schema.py", "tests/test_report_tools.py"),
    ),
    Mutation(
        id="report-id-filter-not-empty",
        behaviour="an id filter listing at least one id: without it an empty list is sent as an empty filter",
        path=f"{SRC}/tools.py",
        edits=(
            (
                "IdList = Annotated[list[Annotated[str, Field(pattern=ID_PATTERN)]], Field(min_length=1)]\n",
                "IdList = Annotated[list[Annotated[str, Field(pattern=ID_PATTERN)]], Field()]\n",
            ),
        ),
        families=("tests/test_tool_schema.py", "tests/test_report_tools.py"),
    ),
    Mutation(
        id="report-id-filter-joined",
        behaviour=(
            "a list of ids sent as the one comma-separated value the pages document: "
            "without it the list's text representation goes out"
        ),
        path=f"{SRC}/tools.py",
        edits=(('    return ",".join(ids) if ids is not None else None\n', "    return str(ids) if ids is not None else None\n"),),
        families=("tests/test_report_tools.py", "tests/test_run_report.py", "tests/test_changes_and_tools.py"),
    ),
    # ---- a report's request carries only its own columns
    Mutation(
        id="report-routing-columns-per-endpoint",
        behaviour=(
            "adding the routing keys only where the report's own vocabulary has "
            "them: without it AccountListDetail's request carries tx_date and "
            "txn_type, outside its contract"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                "        routing = tuple(key for key in ROUTING_COLUMNS if key in allowed)\n",
                "        routing = ROUTING_COLUMNS\n",
            ),
        ),
        families=("tests/test_run_report.py", "tests/test_report_tools.py"),
    ),
    Mutation(
        id="report-columns-final-list-checked",
        behaviour=(
            "checking the list that would go out against the report's own "
            "vocabulary: without it a key this package adds, a default's "
            "included, reaches the request unchecked"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(("    if outside:\n", "    if False:\n"),),
        families=("tests/test_report_tools.py",),
    ),
    # ---- the attachment cap counts the whole result
    Mutation(
        id="attachments-whole-result-cap",
        behaviour=(
            "counting the complete attachment result against the output cap: "
            "without it a file at the raw cap returns a result over the limit"
        ),
        path=f"{SRC}/tools_attachments.py",
        edits=(("    if size > attachments_mod.MAX_ATTACHMENT_OUTPUT_BYTES:\n", "    if False:\n"),),
        families=("tests/test_attachments.py",),
    ),
    # ---- attachment errors name the company
    Mutation(
        id="get-attachment-binds-entity",
        behaviour=(
            "get_attachment binding its resolved company: without it a failed "
            "download reads 'status 404' with no company, alike for every entity"
        ),
        path=f"{SRC}/tools_attachments.py",
        edits=(("            bound.bind(target)\n", ""),),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="tool-errors-name-the-binding",
        behaviour="a bound call's failure text naming its entity and environment",
        path=f"{SRC}/tools.py",
        edits=(('        return f"{text} ({self.identity})"\n', "        return text\n"),),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="tool-errors-unexpected-names-the-binding",
        behaviour="an unexpected failure in a bound call naming its company too",
        path=f"{SRC}/tools.py",
        edits=(
            (
                '        log.exception("unexpected tool failure")\n'
                "        raise ToolError(binding.name(UNEXPECTED_ERROR_TEXT)) from None\n",
                '        log.exception("unexpected tool failure")\n'
                "        raise ToolError(UNEXPECTED_ERROR_TEXT) from None\n",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    # ---- an envelope failure logs no value
    Mutation(
        id="envelope-failure-log-no-value",
        behaviour=(
            "logging an envelope failure by location and type only: without it the "
            "traceback carries pydantic's text, with the accounting value, to the "
            "stderr the client keeps on disk"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                '        log.error("tool result failed envelope validation: %s", envelope_failure_summary(exc))\n',
                '        log.exception("tool result failed envelope validation")\n',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="envelope-failure-log-masks-keys",
        behaviour="writing a location part that is not a field name, which can be data, as <key>",
        path=f"{SRC}/tools.py",
        edits=(
            (
                '            str(part) if isinstance(part, int) or part in declared else "<key>"\n',
                "            str(part)\n",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    # ---- the clients read nothing from the environment
    Mutation(
        id="client-env-accounting",
        behaviour=(
            "the accounting client built by the network policy: without it httpx's "
            "defaults hand the bearer to a proxy the environment names, or to an "
            "interceptor whose CA SSL_CERT_FILE names"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                "            self._http = isolated_client(timeout=ACCOUNTING_TIMEOUT_SECONDS)\n",
                "            self._http = httpx.Client(timeout=ACCOUNTING_TIMEOUT_SECONDS, follow_redirects=False)\n",
            ),
        ),
        families=("tests/test_client_environment.py",),
    ),
    Mutation(
        id="client-env-token-post",
        behaviour=(
            "the token POSTs' client built by the network policy: without it the "
            "client secret and the grant go to a proxy or CA the environment names"
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            (
                "        client = isolated_client(timeout=OAUTH_POST_TIMEOUT_SECONDS)\n",
                "        client = httpx.Client(timeout=OAUTH_POST_TIMEOUT_SECONDS, follow_redirects=False)\n",
            ),
        ),
        families=("tests/test_client_environment.py",),
    ),
    Mutation(
        id="token-client-keeps-no-cookie",
        behaviour=(
            "a token client that keeps no cookie: revoke --all shares one, a cookie the "
            "first answer set rides on the next request, and the policy refuses it"
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            (
                "    client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=()))\n",
                "",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="oauth-unusable-token-is-lost",
        behaviour=(
            "reading a 200 whose tokens can't be sent as a lost response: otherwise a blank "
            "token replaces the stored pair and the refresh reads as a success"
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            (
                "    if not _usable(access_token) or not _usable(refresh_token):\n",
                "    if not isinstance(access_token, str) or not isinstance(refresh_token, str):\n",
            ),
        ),
        families=("tests/test_token_store.py::test_a_success_with_unusable_tokens_is_a_lost_response",),
    ),
    Mutation(
        id="client-env-discovery",
        behaviour=(
            "the discovery client built by the network policy: without it the "
            "document that confirms the pinned endpoints can come through a proxy "
            "or CA the environment names"
        ),
        path=f"{SRC}/oauth.py",
        edits=(
            (
                "    http = client or isolated_client(timeout=DISCOVERY_TIMEOUT_SECONDS)\n",
                "    http = client or httpx.Client(timeout=DISCOVERY_TIMEOUT_SECONDS, follow_redirects=False)\n",
            ),
        ),
        families=("tests/test_client_environment.py",),
    ),
    Mutation(
        id="client-env-no-proxy",
        behaviour=(
            "the builder's trust_env=False: without it every client it builds takes "
            "a proxy from the environment (or macOS's system settings)"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(("        trust_env=False,\n", "        trust_env=True,\n"),),
        families=("tests/test_client_environment.py", "tests/test_attachments.py"),
    ),
    Mutation(
        id="client-env-own-tls",
        behaviour=(
            "the builder's own TLS context: without it httpx builds one through the "
            "standard library, which writes the session's secrets to SSLKEYLOGFILE"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(("        verify=isolated_tls(),\n", ""),),
        families=("tests/test_client_environment.py",),
    ),
    Mutation(
        id="client-env-no-keylog",
        behaviour=(
            "switching the TLS key log off: without it every session's secrets, which "
            "decrypt the bearer and the client secret, go to a file the environment names"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(("    context.keylog_filename = None\n", ""),),
        families=("tests/test_client_environment.py",),
    ),
    # ---- refusals repeat no caller value
    Mutation(
        id="window-refusal-no-dates-list-transactions",
        behaviour=(
            "list_transactions' six-month refusal naming its parameters, not the "
            "caller's dates: without it both dates come back in the error"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                '                raise QueryError(window_refusal_text("start_date", "end_date", MAX_WINDOW_MONTHS))\n',
                "                raise QueryError(window_refusal_text(start.isoformat(), end.isoformat(), MAX_WINDOW_MONTHS))\n",
            ),
        ),
        families=("tests/test_no_caller_value_in_errors.py",),
    ),
    Mutation(
        id="window-refusal-no-dates-reports",
        behaviour=(
            "the report tools' shared window refusal naming its parameters, not the "
            "dates: without it every detail report's refusal repeats the caller's dates"
        ),
        path=f"{SRC}/tools_reports.py",
        edits=(
            (
                "        raise QueryError(window_refusal_text(start_name, end_name, months))\n",
                "        raise QueryError(window_refusal_text(start.isoformat(), end.isoformat(), months))\n",
            ),
        ),
        families=("tests/test_no_caller_value_in_errors.py",),
    ),
    # ---- a recorded Size must be readable
    Mutation(
        id="attachments-size-numeric-string",
        behaviour=(
            "reading a Size written as text as its number: without it \"100\" is "
            "unreadable and the download is refused"
        ),
        path=f"{SRC}/projections.py",
        edits=(("        if not _SIZE_TEXT.fullmatch(value):\n", "        if True:\n"),),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachments-size-not-whole-refused",
        behaviour="a fractional Size is not a whole number: without it 671.5 is read as 671",
        path=f"{SRC}/projections.py",
        edits=(("        if not value.is_integer():\n", "        if False:\n"),),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachments-size-negative-refused",
        behaviour="a negative Size is refused: without it -1 is taken as a recorded length",
        path=f"{SRC}/projections.py",
        edits=(
            (
                "    if isinstance(value, int):\n        if value < 0:\n",
                "    if isinstance(value, int):\n        if False:\n",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachments-size-unreadable-refused",
        behaviour=(
            "get_attachment reading the Size strictly and refusing one it cannot read: "
            "without it an unreadable Size downloads the file unsized"
        ),
        path=f"{SRC}/tools_attachments.py",
        edits=(
            (
                '                size = recorded_size(raw.get("Size"))\n',
                '                size = metadata.get("size")\n',
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    # ---- the envelope-failure log names only declared fields
    Mutation(
        id="envelope-failure-log-declared-fields",
        behaviour=(
            "naming a location part only when the envelope model declares it: "
            "without it any identifier-shaped dict key -- data -- is logged"
        ),
        path=f"{SRC}/tools.py",
        edits=(
            (
                '            str(part) if isinstance(part, int) or part in declared else "<key>"\n',
                '            str(part) if isinstance(part, int) or str(part).isidentifier() else "<key>"\n',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="envelope-failure-log-nested-fields",
        behaviour=(
            "the declared fields of the models an envelope nests: without them a "
            "nested field's name is logged as <key> and the log loses where it failed"
        ),
        path=f"{SRC}/tools.py",
        edits=(("            pending.extend(_nested_models(info.annotation))\n", ""),),
        families=("tests/test_error_boundary.py",),
    ),
    # ---- a trim is known loss in flatten() too
    Mutation(
        id="flatten-known-loss-before-cap",
        behaviour=(
            "flatten() calling a trimmed or anomalous report partial whatever the "
            "upstream cap says, as the tools do: without it the library says "
            "unknown where the tools say partial"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                '    if client_truncated or anomalous:\n        return "partial"\n'
                '    if upstream_partial:\n        return "unknown"\n',
                '    if upstream_partial:\n        return "unknown"\n'
                '    if client_truncated or anomalous:\n        return "partial"\n',
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    # ---- openWorldHint false where nothing leaves
    Mutation(
        id="open-world-false-list-entities",
        behaviour="list_entities saying it reaches nothing outside: it makes no request",
        path=f"{SRC}/tools.py",
        edits=(
            (
                '        annotations=read_only("List QuickBooks companies", open_world=False),\n',
                '        annotations=read_only("List QuickBooks companies"),\n',
            ),
        ),
        families=("tests/test_tool_schema.py",),
    ),
    Mutation(
        id="open-world-false-server-info",
        behaviour="server_info saying it reaches nothing outside: it makes no request",
        path=f"{SRC}/tools.py",
        edits=(
            (
                '        annotations=read_only("Server information", open_world=False),\n',
                '        annotations=read_only("Server information"),\n',
            ),
        ),
        families=("tests/test_tool_schema.py",),
    ),
    # ---- the rate-limit file names no company id
    Mutation(
        id="ratelimit-filed-by-entity-key",
        behaviour=(
            "filing each call under the entity key: without it the rate-limit "
            "file names every company's full realm id, in plaintext"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                "                entity.key, timeout=call_deadline.bounded(DEFAULT_ACQUIRE_TIMEOUT_SECONDS)\n",
                "                entity.realm_id, timeout=call_deadline.bounded(DEFAULT_ACQUIRE_TIMEOUT_SECONDS)\n",
            ),
        ),
        families=("tests/test_rate_limit_file.py",),
    ),
    Mutation(
        id="ratelimit-block-filed-by-entity-key",
        behaviour=(
            "filing a 429 block under the entity key: without it the block names "
            "the realm id and sits where no call looks, so each call spends a "
            "request to rediscover the 429"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                '                entity.key, response.headers.get("Retry-After")\n',
                '                entity.realm_id, response.headers.get("Retry-After")\n',
            ),
        ),
        families=("tests/test_rate_limit_file.py",),
    ),
    Mutation(
        id="ratelimit-file-private-mode",
        behaviour="the rate-limit file's 0600: without it the umask decides, 0644 by default",
        path=f"{SRC}/limits.py",
        edits=(("STATE_FILE_MODE: Final = 0o600\n", "STATE_FILE_MODE: Final = 0o666\n"),),
        families=("tests/test_rate_limit_file.py",),
    ),
    Mutation(
        id="ratelimit-file-written-like-the-store",
        behaviour=(
            "writing the rate-limit file the way the store writes its document: "
            "without it the file is written in place, under the umask, unsynced, "
            "and through a link planted at its path"
        ),
        path=f"{SRC}/limits.py",
        edits=(
            (
                "            write_atomic(self.state_path, payload, mode=STATE_FILE_MODE)\n",
                "            self.state_path.write_bytes(payload)\n",
            ),
        ),
        families=("tests/test_rate_limit_file.py",),
    ),
    Mutation(
        id="ratelimit-realm-keyed-not-carried",
        behaviour=(
            "discarding a realm-keyed file: without it its "
            "entries are read as current and every realm id is carried forward"
        ),
        path=f"{SRC}/limits.py",
        edits=(
            (
                '    if not isinstance(document, dict) or document.get("format") != STATE_FORMAT:\n'
                "        return {}\n"
                '    section = document.get("entities")\n',
                "    if not isinstance(document, dict):\n"
                "        return {}\n"
                '    section = document.get("entities", document)\n',
            ),
        ),
        families=("tests/test_rate_limit_file.py",),
    ),
    Mutation(
        id="ratelimit-other-format-discarded",
        behaviour=(
            "reading only the current format: without it a file of any other "
            "format is trusted as if this build had written it"
        ),
        path=f"{SRC}/limits.py",
        edits=(
            (
                '    if not isinstance(document, dict) or document.get("format") != STATE_FORMAT:\n',
                "    if not isinstance(document, dict):\n",
            ),
        ),
        families=("tests/test_rate_limit_file.py",),
    ),
    Mutation(
        id="ratelimit-rewritten-at-first-contact",
        behaviour=(
            "rewriting a file not in the current format at first contact: without "
            "it a realm-keyed entry survives every contact that records nothing"
        ),
        path=f"{SRC}/limits.py",
        edits=(("        if document != _document_of(entities):\n            self._write(entities)\n", ""),),
        families=("tests/test_rate_limit_file.py",),
    ),
    Mutation(
        id="registry-key-carries-no-realm-id",
        behaviour=(
            "refusing an entity key that contains a company's realm id: without "
            "it the key carries the id into every result and the rate-limit file"
        ),
        path=f"{SRC}/registry.py",
        edits=(("    carrying = _key_carrying_a_realm_id(section)\n", "    carrying = None\n"),),
        families=("tests/test_rate_limit_file.py",),
    ),
    # ---- a rate-limit file that cannot be read or written stops the call
    Mutation(
        id="ratelimit-unreadable-refused",
        behaviour=(
            "refusing a rate-limit file that cannot be read: without it the file "
            "reads as empty, every window as open, and the next write replaces "
            "the history in it"
        ),
        path=f"{SRC}/limits.py",
        edits=(
            (
                "        except OSError:\n            raise RateLimited(STATE_UNAVAILABLE_TEXT) from None\n",
                "        except OSError:\n            return {}\n",
            ),
        ),
        families=("tests/test_rate_limit_file.py",),
    ),
    Mutation(
        id="ratelimit-unwritable-refused",
        behaviour=(
            "not sending a call the rate-limit file cannot record: without it every "
            "call goes uncounted, the windows never fill, and no other process sees "
            "a 429 block"
        ),
        path=f"{SRC}/limits.py",
        edits=(
            (
                "            # fails once tends to fail for every call after it.\n"
                "            raise RateLimited(STATE_UNAVAILABLE_TEXT) from None\n",
                "            # fails once tends to fail for every call after it.\n"
                "            return\n",
            ),
        ),
        families=("tests/test_rate_limit_file.py",),
    ),
    # ---- a change response that is not the documented shape is refused whole
    Mutation(
        id="changes-envelope-required",
        behaviour=(
            "refusing a change response with no block: without it a missing or "
            "empty envelope reads as no changes, complete, and a safe checkpoint"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "    if not isinstance(blocks, list) or not blocks:\n"
                "        raise QboMcpError(CDC_MALFORMED_TEXT)\n",
                "    if not isinstance(blocks, list) or not blocks:\n"
                "        return [], 0\n",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="changes-entries-required",
        behaviour=(
            "refusing a block with no list of entries: without it the block is "
            "skipped and the count that is left looks complete"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "        if not isinstance(responses, list) or not responses:\n"
                "            raise QboMcpError(CDC_MALFORMED_TEXT)\n",
                "        if not isinstance(responses, list) or not responses:\n"
                "            continue\n",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="changes-entry-is-an-object",
        behaviour=(
            "refusing an entry that is not an object: without it the entry is "
            "skipped and its changes are missing from a result called complete"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "            if not isinstance(response, Mapping):\n"
                "                raise QboMcpError(CDC_MALFORMED_TEXT)\n",
                "            if not isinstance(response, Mapping):\n"
                "                continue\n",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="changes-objects-are-a-list",
        behaviour=(
            "refusing a requested type whose objects are not a list: without it "
            "they are skipped and the type reads as unchanged"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "                if not isinstance(value, list):\n"
                "                    raise QboMcpError(CDC_MALFORMED_TEXT)\n",
                "                if not isinstance(value, list):\n"
                "                    continue\n",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="changes-object-is-an-object",
        behaviour=(
            "refusing an object that is not one: without it the object is skipped, "
            "uncounted, and the count that is left looks complete"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "                    if not isinstance(item, Mapping):\n"
                "                        raise QboMcpError(CDC_MALFORMED_TEXT)\n",
                "                    if not isinstance(item, Mapping):\n"
                "                        continue\n",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="changes-every-requested-type-accounted-for",
        behaviour=(
            "refusing a change response whose entries don't number the requested "
            "types: a type no entry accounts for reads as unchanged, and the result "
            "as a safe checkpoint"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "    if entries != len(requested):\n"
                "        raise QboMcpError(CDC_MALFORMED_TEXT)\n",
                "",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="changes-one-type-per-entry",
        behaviour=(
            "refusing an entry holding two types, or a type held twice: counted as "
            "accounted for, another requested type goes missing unnoticed"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "            if len(types_here) > 1 or named.intersection(types_here):\n"
                "                raise QboMcpError(CDC_MALFORMED_TEXT)\n",
                "",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="changes-unknown-member-refused",
        behaviour=(
            "refusing an entry member that is neither a requested type nor a paging "
            "field: passed over, an entry holding anything reads as a type with no changes"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "                    # Anything else could change what the entry means.\n"
                "                    raise QboMcpError(CDC_MALFORMED_TEXT)\n",
                "                    continue\n",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="changes-paging-fields-are-single-values",
        behaviour=(
            "refusing a paging field that holds more than one value: passed over, "
            "what it holds goes unread"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "                    if not is_scalar(value):\n"
                "                        raise QboMcpError(CDC_MALFORMED_TEXT)\n",
                "",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    # ---- a diagnostic reports the store's own refusal
    Mutation(
        id="diagnostics-report-the-refusal-itself",
        behaviour=(
            "the diagnostic tools reporting why the store was refused: without it "
            "a store refused for its location is reported as the plaintext "
            "refusal, with advice that would not fix it"
        ),
        path=f"{SRC}/session.py",
        edits=(
            (
                "            return None, redact(str(exc))\n",
                "            from .errors import PLAINTEXT_REFUSED_TEXT\n"
                "            return None, PLAINTEXT_REFUSED_TEXT\n",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    # ---- an object where one value belongs never becomes text
    Mutation(
        id="changes-object-for-a-value-refused",
        behaviour=(
            "refusing a change whose id, status or timestamp is an object: without "
            "it the object's text, and every field nested in it, is returned as "
            "that value"
        ),
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                "                        part is not None and not is_scalar(part)\n",
                "                        False\n",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="vendor-payments-reference-never-text",
        behaviour=(
            "a payee reference read one value at a time: without it an object as "
            "a payee's id, name or type becomes text in the rows, the totals or "
            "the reasons a payment was excluded"
        ),
        path=f"{SRC}/vendor_payments.py",
        edits=(
            (
                '    return as_str(ref.get("value")), as_str(ref.get("name")), as_str(ref.get("type"))\n',
                '    return tuple(None if ref.get(k) is None else str(ref.get(k)) for k in ("value", "name", "type"))\n',
            ),
        ),
        families=("tests/test_vendor_payments.py",),
    ),
    Mutation(
        id="vendor-payments-id-never-text",
        behaviour=(
            "a payment's id read as one value: without it an object there becomes "
            "the transaction id in every row and review list"
        ),
        path=f"{SRC}/vendor_payments.py",
        edits=(('    return as_str(row.get("Id")) or ""\n', '    return str(row.get("Id", ""))\n'),),
        families=("tests/test_vendor_payments.py",),
    ),
    Mutation(
        id="vendor-payments-fields-never-text",
        behaviour=(
            "a payment's date, document number and line id read as one value: "
            "without it an object there is returned as its text"
        ),
        path=f"{SRC}/vendor_payments.py",
        edits=(
            (
                '    return None if value in (None, "") else as_str(value)\n',
                '    return None if value in (None, "") else str(value)\n',
            ),
        ),
        families=("tests/test_vendor_payments.py",),
    ),
    Mutation(
        id="vendor-payments-currency-never-text",
        behaviour=(
            "a payment's currency read as one value: without it an object there "
            "becomes the currency a row reports"
        ),
        path=f"{SRC}/vendor_payments.py",
        edits=(('        return as_str(ref["value"])\n', '        return str(ref["value"])\n'),),
        families=("tests/test_vendor_payments.py",),
    ),
    Mutation(
        id="report-field-object-refused",
        behaviour=(
            "refusing a report whose header field or row group is an object: "
            "without it the object's text passes the column and cell checks "
            "into the result"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "    if not is_scalar(value):\n"
                '        raise _structural(f"{where} is not a single value")\n',
                "",
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-currency-checked",
        behaviour=(
            "the report currency read through the header check: without it an "
            "object there becomes the currency of every money cell"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                '    currency = _header_text(header, "Currency")\n',
                '    currency = str(header.get("Currency")) if header.get("Currency") else None\n',
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    Mutation(
        id="report-row-group-checked",
        behaviour=(
            "a row's group read through the field check: without it an object "
            "there becomes the group the row reports"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                "            group = _text_or_none(raw.get(\"group\"), f\"row {row_id}'s group\")\n",
                '            group = str(raw.get("group")) if raw.get("group") else None\n',
            ),
        ),
        families=("tests/test_report_flattener.py",),
    ),
    # ---- secrets the environment holds are redacted from the first line
    Mutation(
        id="startup-secrets-registered-first-server",
        behaviour=(
            "the server registering the environment's secrets first: without it "
            "a secret pasted into the registry by mistake is printed by the "
            "registry's refusal, before credentials are built"
        ),
        path=f"{SRC}/server.py",
        edits=(("    register_environment_secrets()\n", ""),),
        families=("tests/test_registry_and_startup.py",),
    ),
    Mutation(
        id="startup-secrets-registered-first-auth",
        behaviour=(
            "the auth CLI registering the environment's secrets first: without it "
            "a secret pasted into the registry by mistake is printed by the "
            "registry's refusal"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(("    register_environment_secrets()\n", ""),),
        families=("tests/test_registry_and_startup.py",),
    ),
    Mutation(
        id="startup-secrets-registered-first-keepalive",
        behaviour=(
            "the keepalive registering the environment's secrets first: without "
            "it a secret pasted into the registry by mistake is printed by the "
            "registry's refusal"
        ),
        path=f"{SRC}/keepalive.py",
        edits=(("    register_environment_secrets()\n", ""),),
        families=("tests/test_registry_and_startup.py",),
    ),
    Mutation(
        id="startup-secrets-registered-after-the-env-file",
        behaviour=(
            "registering the secrets an env file brings: without it a secret "
            "loaded from --env-file is printed by the registry error after it"
        ),
        path=f"{SRC}/config.py",
        edits=(("        register_environment_secrets()\n", ""),),
        families=("tests/test_registry_and_startup.py",),
    ),
    Mutation(
        id="startup-secrets-cover-the-store-key",
        behaviour=(
            "the token store key among the environment's secrets: without it the "
            "key pasted into the registry by mistake is printed"
        ),
        path=f"{SRC}/config.py",
        edits=(
            (
                "SECRET_ENVIRONMENT_VARIABLES: Final = (ENV_CLIENT_SECRET, ENV_KEY_VAR, ENV_IMPORT_REFRESH_TOKEN)\n",
                "SECRET_ENVIRONMENT_VARIABLES: Final = (ENV_CLIENT_SECRET, ENV_IMPORT_REFRESH_TOKEN)\n",
            ),
        ),
        families=("tests/test_registry_and_startup.py",),
    ),
    Mutation(
        id="startup-secrets-cover-the-import-token",
        behaviour=(
            "the refresh token being imported among the environment's secrets: "
            "without it that token pasted into the registry by mistake is printed"
        ),
        path=f"{SRC}/config.py",
        edits=(
            (
                "SECRET_ENVIRONMENT_VARIABLES: Final = (ENV_CLIENT_SECRET, ENV_KEY_VAR, ENV_IMPORT_REFRESH_TOKEN)\n",
                "SECRET_ENVIRONMENT_VARIABLES: Final = (ENV_CLIENT_SECRET, ENV_KEY_VAR)\n",
            ),
        ),
        families=("tests/test_registry_and_startup.py",),
    ),
    # ---- something else on the IPv6 loopback stops consent
    Mutation(
        id="callback-ipv6-conflict-fatal",
        behaviour=(
            "skipping only an IPv6 loopback that doesn't exist: without it a port "
            "something else holds on [::1] is skipped too, and a browser that "
            "resolves localhost to IPv6 hands that process the callback"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(
            (
                "                if host == CALLBACK_HOST_V6 and exc.errno in IPV6_UNAVAILABLE_ERRNOS:\n",
                "                if host == CALLBACK_HOST_V6:\n",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="callback-partial-bind-released",
        behaviour=(
            "releasing the IPv4 listener when the IPv6 bind fails: without it the "
            "refused listener keeps the port it bound"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(
            (
                "                for bound in self._servers:\n"
                "                    # Never started, so closing is enough: shutdown() would\n"
                "                    # wait for a serve loop that never ran.\n"
                "                    bound.server_close()\n"
                "                self._servers.clear()\n",
                "",
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    # ---- one timeout covers every wait for a lock
    Mutation(
        id="store-lock-waits-share-one-timeout",
        behaviour=(
            "the file lock getting what the mutex left of the timeout: without it "
            "each wait gets all of it, and a contended lock keeps a call twice "
            "as long as its budget"
        ),
        path=f"{SRC}/store.py",
        edits=(
            (
                "            state.fd = _flock_exclusive(lock_path, max(0.0, expires - time.monotonic()))\n",
                "            state.fd = _flock_exclusive(lock_path, timeout)\n",
            ),
        ),
        families=("tests/test_call_limits.py",),
    ),
    Mutation(
        id="ratelimit-lock-waits-share-one-timeout",
        behaviour=(
            "each look at the rate-limit file waiting only for what the timeout "
            "has left: without it every pass waits for the whole timeout again"
        ),
        path=f"{SRC}/limits.py",
        edits=(
            (
                "            with self._locked(timeout=max(0.0, deadline - self.clock())):\n",
                "            with self._locked(timeout=max(1.0, timeout)):\n",
            ),
        ),
        families=("tests/test_pagination_and_limits.py",),
    ),
    # ---- a fake name can't be checked against a guess
    Mutation(
        id="sanitizer-pseudonym-keyed",
        behaviour=(
            "keying the sanitizer's fake names with a per-run key: without it "
            "anyone can hash a guessed real name and compare it with a committed "
            "fake one"
        ),
        path="tests/fixtures/sanitize.py",
        edits=(
            (
                '    digest = hmac.new(_RUN_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()[:6].upper()\n',
                '    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:6].upper()\n',
            ),
        ),
        families=("tests/test_fixture_sanitizer.py",),
    ),
    # ---- the store folder is private whoever creates it
    Mutation(
        id="ratelimit-folder-created-private",
        behaviour=(
            "the rate limiter creating the store's folder through the store's own "
            "routine: without it a limiter that reaches the folder first makes it "
            "0755 under the default umask, and the store never tightens a folder "
            "that exists"
        ),
        path=f"{SRC}/limits.py",
        edits=(
            (
                "        _mkdir_private(self.directory)\n",
                "        self.directory.mkdir(parents=True, exist_ok=True)\n",
            ),
        ),
        families=("tests/test_store_lifecycle.py",),
    ),
    # ---- the listener's cache header
    Mutation(
        id="listener-no-cache",
        behaviour=(
            "no-cache beside no-store on the listener's answers, Intuit's wording: "
            "without it the page for the URL that carries the code says no-store only"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(
            (
                '        self.send_header("Cache-Control", "no-cache, no-store")\n',
                '        self.send_header("Cache-Control", "no-store")\n',
            ),
        ),
        families=("tests/test_oauth_cli.py",),
    ),
    Mutation(
        id="listener-cache-control-sent",
        behaviour=(
            "the listener's Cache-Control header at all: without it the page for "
            "the URL that carries the code may be cached"
        ),
        path=f"{SRC}/callback_server.py",
        edits=(('        self.send_header("Cache-Control", "no-cache, no-store")\n', ""),),
        families=("tests/test_oauth_cli.py",),
    ),
    # ---- TransactionListByCustomer is sent no columns
    Mutation(
        id="run-report-by-customer-sends-no-columns",
        behaviour=(
            "sending TransactionListByCustomer no columns: without it the "
            "default set is requested by name, and in production Intuit "
            "answers every such request with fault 10000, so the report never answers"
        ),
        path=f"{SRC}/report_catalog.py",
        edits=(
            (
                '"cleared", "docnum", "memo", "payment_method", "department", "sort_order"},\n'
                '            date_macros=_D33, window=WINDOW_DATE, detail=True,\n'
                '            columns_refusal=BY_CUSTOMER_COLUMNS_REFUSAL,\n',
                '"cleared", "docnum", "memo", "payment_method", "department", "sort_order", "columns"},\n'
                '            date_macros=_D33, window=WINDOW_DATE, detail=True,\n'
                '            default_columns=("tx_date", "txn_type", "doc_num", "is_no_post", "memo", '
                '"account_name", "amount"),\n',
            ),
        ),
        families=("tests/test_run_report.py",),
    ),
    Mutation(
        id="netpolicy-by-customer-no-columns",
        behaviour=(
            "the network policy refusing columns on reports/TransactionListByCustomer: "
            "without it a later edit that sends them there reaches Intuit, which "
            "faults on any value in production"
        ),
        path=f"{SRC}/netpolicy.py",
        edits=(
            (
                '"source_account_type", "cleared", "docnum", "memo", "payment_method", "department", "sort_order"}\n',
                '"source_account_type", "cleared", "docnum", "memo", "payment_method", "department", "sort_order",\n'
                '         "columns"}\n',
            ),
        ),
        families=("tests/test_network_policy.py", "tests/test_run_report.py"),
    ),
    Mutation(
        id="run-report-by-customer-columns-refusal-reason",
        behaviour=(
            "refusing a caller's columns for TransactionListByCustomer with the "
            "reason: without it the refusal says only that the report does not "
            "take them, never that Intuit faults on them in production"
        ),
        path=f"{SRC}/tools_run_report.py",
        edits=(
            (
                "            if columns is not None and spec.columns_refusal:\n"
                "                raise QueryError(spec.columns_refusal)\n",
                "",
            ),
        ),
        families=("tests/test_run_report.py",),
    ),
    Mutation(
        id="run-report-by-customer-default-set-admitted",
        behaviour=(
            "the response allowlist admitting TransactionListByCustomer's own "
            "default set: without amount there, the report sent no columns comes "
            "back with its amounts withheld"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                'frozenset({"tx_date", "txn_type", "doc_num", "is_no_post", "memo", "account_name", "amount",\n',
                'frozenset({"tx_date", "txn_type", "doc_num", "is_no_post", "memo", "account_name",\n',
            ),
        ),
        families=("tests/test_run_report.py",),
    ),
    # ---- an unknown fault is named by its code
    Mutation(
        id="fault-unknown-code-named",
        behaviour=(
            "naming a fault the table has no hint for by its codes: without it the "
            "error says only that a fault arrived, as it did for fault 10000 in "
            "production, and names nothing to diagnose"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                "            named = known if known is not None else named_fault_codes(codes)\n",
                "            named = known\n",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="fault-code-named-digits-only",
        behaviour=(
            "naming digits only: without it a short code that is not digits -- "
            "upstream text -- is repeated in the error"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                '_NAMEABLE_FAULT_CODE: Final = re.compile(r"[0-9]{1,7}")\n',
                '_NAMEABLE_FAULT_CODE: Final = re.compile(r".{1,7}")\n',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="fault-code-named-seven-digits-at-most",
        behaviour=(
            "naming a code of at most seven digits: without the bound a digit run "
            "of identifier length in a code field is repeated in the error"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                '_NAMEABLE_FAULT_CODE: Final = re.compile(r"[0-9]{1,7}")\n',
                '_NAMEABLE_FAULT_CODE: Final = re.compile(r"[0-9]+")\n',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="fault-codes-named-five-at-most",
        behaviour=(
            "naming at most five codes and counting the rest: without it a fault "
            "carrying thousands of errors names every code in one error text"
        ),
        path=f"{SRC}/client.py",
        edits=(
            (
                '    shown = ",".join(nameable[:FAULT_CODES_NAMED])\n',
                '    shown = ",".join(nameable)\n',
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="fault-codes-named-once",
        behaviour="naming each of a fault's codes once, however many of its errors carry it",
        path=f"{SRC}/client.py",
        edits=(
            (
                "    nameable = list(dict.fromkeys(code for code in codes if _NAMEABLE_FAULT_CODE.fullmatch(code)))\n",
                "    nameable = [code for code in codes if _NAMEABLE_FAULT_CODE.fullmatch(code)]\n",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="fault-unknown-says-no-hint",
        behaviour=(
            "saying that this server has no hint for a fault it names: without it a "
            "named code reads as if the text beside it explained it"
        ),
        path=f"{SRC}/client.py",
        edits=(('                hint += " This server has no hint for this fault."\n', "                pass\n"),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="fault-10000-hint",
        behaviour=(
            "the hint for fault 10000, an internal error on Intuit's side that "
            "repeated for the same request: without it the table has no entry and "
            "the error only names the code"
        ),
        path=f"{SRC}/client.py",
        edits=(('    "10000": (\n', '    "10000-not-in-the-table": (\n'),),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="redact-digit-run-anywhere",
        behaviour=(
            "masking a long digit run joined to a letter or an underscore: with word "
            "boundaries, a company id in a file name survives every error"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """    r"|\\d{8,}"\n""",
                """    r"|\\b\\d{8,}\\b"\n""",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="redact-long-value-anywhere",
        behaviour=(
            "masking a long opaque value after an underscore: `_` is a word character, "
            "so a word boundary spares it"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """    r"|[A-Za-z0-9]{40,}"\n""",
                """    r"|\\b[A-Za-z0-9]{40,}\\b"\n""",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="parser-refusal-cuts-at-the-first-quote",
        behaviour=(
            "dropping what argparse quotes in a refusal: a bad choice or a value a flag "
            "can't take reaches stderr, past every hook"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """        reason = re.split("['\\"]", message, maxsplit=1)[0].rstrip(": ")\n""",
                """        reason = message\n""",
            ),
        ),
        families=("tests/test_refused_command_lines.py",),
    ),
    Mutation(
        id="parser-refusal-redacted",
        behaviour=(
            "the redactor behind the parser's refusal: a value shaped like an option "
            "keeps its name, company id and all"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """{self.prog}: error: {redact(reason)}\\n""",
                """{self.prog}: error: {reason}\\n""",
            ),
        ),
        families=("tests/test_refused_command_lines.py",),
    ),
    Mutation(
        id="parser-stray-words-reduced",
        behaviour="reducing a stray argument to an option name or a placeholder: it's printed as typed",
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """' '.join(_option_name(word) for word in stray)""",
                """' '.join(stray)""",
            ),
        ),
        families=("tests/test_refused_command_lines.py",),
    ),
    Mutation(
        id="parser-option-name-kept",
        behaviour=(
            "keeping the name of a stray option while dropping its value: a refusal "
            "stops saying which option it refused"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """    name = word.partition("=")[0]\n""",
                """    name = word\n""",
            ),
        ),
        families=("tests/test_refused_command_lines.py",),
    ),
    Mutation(
        id="parser-server-value-free",
        behaviour=(
            "the server's command line refusing an abbreviated option, and refusing "
            "without repeating what it refused: without it --allow-doc is --allow-documents"
        ),
        path=f"{SRC}/server.py",
        edits=(
            (
                """    parser = ValueFreeParser(\n""",
                """    parser = argparse.ArgumentParser(\n""",
            ),
        ),
        families=("tests/test_no_abbreviated_options.py", "tests/test_refused_command_lines.py"),
    ),
    Mutation(
        id="parser-keepalive-value-free",
        behaviour=(
            "the keepalive's command line refusing an abbreviated option, and refusing "
            "without repeating what it refused: without it --token-st is --token-store"
        ),
        path=f"{SRC}/keepalive.py",
        edits=(
            (
                """    parser = ValueFreeParser(\n""",
                """    parser = argparse.ArgumentParser(\n""",
            ),
        ),
        families=("tests/test_no_abbreviated_options.py", "tests/test_refused_command_lines.py"),
    ),
    Mutation(
        id="parser-auth-value-free",
        behaviour=(
            "the auth CLI's command line, and through it each command's own parser, "
            "refusing an abbreviated option and refusing without repeating what it "
            "refused: without it revoke --al is revoke --all, every company"
        ),
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """    parser = ValueFreeParser(\n""",
                """    parser = argparse.ArgumentParser(\n""",
            ),
        ),
        families=("tests/test_no_abbreviated_options.py", "tests/test_refused_command_lines.py"),
    ),
    Mutation(
        id="report-period-needs-real-dates",
        behaviour=(
            "requiring real dates on a period column: any two nonempty strings admit an "
            "unreviewed Money column, cells and all"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """        start, end = _period_date(self.period_start), _period_date(self.period_end)\n""",
                """        return bool(self.period_start) and bool(self.period_end)\n"""
                """        start, end = _period_date(self.period_start), _period_date(self.period_end)\n""",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="report-period-dates-in-order",
        behaviour="requiring a period's start on or before its end: a backwards period is admitted",
        path=f"{SRC}/reports.py",
        edits=(
            (
                """        return start is not None and end is not None and start <= end\n""",
                """        return start is not None and end is not None\n""",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="report-period-one-date-form",
        behaviour="taking only the form Intuit sends: compact and week dates are admitted",
        path=f"{SRC}/reports.py",
        edits=(
            (
                """    if not (isinstance(text, str) and _PERIOD_DATE.fullmatch(text)):\n""",
                """    if not isinstance(text, str):\n""",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="report-period-impossible-date",
        behaviour=(
            "treating a date that doesn't exist as no date: 2026-02-31 stops the whole "
            "report instead of excluding one column"
        ),
        path=f"{SRC}/reports.py",
        edits=(
            (
                """        return date.fromisoformat(text)\n    except ValueError:\n""",
                """        return date.fromisoformat(text)\n    except KeyError:\n""",
            ),
        ),
        families=("tests/test_report_shapes.py",),
    ),
    Mutation(
        id="attachment-budget-before-the-lookup",
        behaviour="checking the budget before a name lookup: a spent call still asks the resolver",
        path=f"{SRC}/attachments.py",
        edits=(
            (
                """            if deadline is not None:\n                deadline.checkpoint()\n"""
                """            check_destination(request.url.host or "", settings, resolve)\n""",
                """            check_destination(request.url.host or "", settings, resolve)\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachment-hop-timeout-bounded",
        behaviour=(
            "bounding each download hop by what's left of the budget: a slow host or a "
            "redirect waits the full timeout whatever the call has left"
        ),
        path=f"{SRC}/attachments.py",
        edits=(
            (
                """                call_deadline.bounded(ACCOUNTING_TIMEOUT_SECONDS)\n""",
                """                ACCOUNTING_TIMEOUT_SECONDS\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="scrub-failure-prints-no-value",
        behaviour="a failed scan naming files, not values: the log becomes a second copy of the finding",
        path="scripts/scrub_check.py",
        edits=(
            (
                """    for path, count in sorted(collections.Counter(masked(path) for path, _ in found).items()):\n"""
                """        print(f"  {path}: {count} long digit run(s)")\n""",
                """    for path, value in found:\n"""
                """        print(f"  {path}: {value}")\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-unreadable-file-fails",
        behaviour="failing on a tracked file it can't read: the scan passes on what it never saw",
        path="scripts/scrub_check.py",
        edits=(
            (
                """            raise ScanIncomplete(f"could not read {masked(path)}: {exc.strerror}") from None\n""",
                """            continue\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-deleted-file-left-to-history",
        behaviour=(
            "telling a deleted file from an unreadable one: an uncommitted deletion stops "
            "the scan, though its blob is still read"
        ),
        path="scripts/scrub_check.py",
        edits=(
            (
                """        except (FileNotFoundError, IsADirectoryError):\n""",
                """        except IsADirectoryError:\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-missing-object-fails",
        behaviour="failing on an object git can't supply: the history scan stops there and passes",
        path="scripts/scrub_check.py",
        edits=(
            (
                """            raise ScanIncomplete(f"git could not supply object {header[0][:8] if header else '?'}")\n""",
                """            break\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-short-listing-fails",
        behaviour="failing on a listing that ends early: the history scan passes on part of it",
        path="scripts/scrub_check.py",
        edits=(
            (
                """        if newline == -1:\n            raise ScanIncomplete("git's object listing ended early")\n""",
                """        if newline == -1:\n            break\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-incomplete-exits-2",
        behaviour="an incomplete scan exiting nonzero: a caller reads it as a pass",
        path="scripts/scrub_check.py",
        edits=(
            (
                """        print(f"scrub-check: INCOMPLETE -- {exc}", file=sys.stderr)\n        return 2\n""",
                """        print(f"scrub-check: INCOMPLETE -- {exc}", file=sys.stderr)\n        return 0\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="attachment-timeout-names-the-deadline",
        behaviour=(
            "telling the budget's timeout from the request's: a call the budget ended "
            "reports a slow host instead of the deadline"
        ),
        path=f"{SRC}/attachments.py",
        edits=(
            (
                """                if deadline is not None and deadline.usable() <= 0.5:\n""",
                """                if False:\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachment-timeout-refused-by-name",
        behaviour="naming a timed-out download: it reaches the model as an unexpected error",
        path=f"{SRC}/attachments.py",
        edits=(
            (
                """                raise AttachmentRefused("the download timed out, so nothing is returned") from None\n""",
                """                raise\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachment-network-failure-refused-by-name",
        behaviour="naming a download that failed on the network: it reaches the model as an unexpected error",
        path=f"{SRC}/attachments.py",
        edits=(
            (
                """                raise AttachmentRefused("the download failed, so nothing is returned") from None\n""",
                """                raise\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="redact-keeps-the-support-id",
        behaviour=(
            "keeping Intuit's support id whole: one error in six carries a cut id, "
            "useless to quote to Intuit"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """    r"(?P<kept>(?<= intuit_tid=)[A-Za-z0-9][A-Za-z0-9-]{0,79}|(?<=the download host ')[A-Za-z0-9._-]+)"\n""",
                """    r"(?P<kept>(?<=the download host ')[A-Za-z0-9._-]+)"\n""",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="redact-keeps-the-pinned-host",
        behaviour="keeping the host an operator has to pin whole: the refusal stops saying what to pin",
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """    r"(?P<kept>(?<= intuit_tid=)[A-Za-z0-9][A-Za-z0-9-]{0,79}|(?<=the download host ')[A-Za-z0-9._-]+)"\n""",
                """    r"(?P<kept>(?<= intuit_tid=)[A-Za-z0-9][A-Za-z0-9-]{0,79})"\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="redact-token-shape-anywhere",
        behaviour="masking a token shape after an underscore: a word boundary spares it",
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """(re.compile(r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{8,}""",
                """(re.compile(r"\\beyJ[A-Za-z0-9_-]{8,}""",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="redact-encrypted-token-whole",
        behaviour="masking an encrypted token whole: only its header goes, and the rest stands",
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """[A-Za-z0-9_-]{8,}+(?:\\.[A-Za-z0-9_-]*){2,4}"), REDACTED),""",
                """[A-Za-z0-9_-]{8,}+\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]+"), REDACTED),""",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="parser-abbreviation-off-by-default",
        behaviour=(
            "the parser class turning abbreviation off for every parser built from it: "
            "without it --enti is --entities, revoke --al is revoke --all, and an "
            "ambiguous prefix is printed, value and all"
        ),
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """        kwargs.setdefault("allow_abbrev", False)\n""",
                "",
            ),
        ),
        families=("tests/test_no_abbreviated_options.py", "tests/test_refused_command_lines.py"),
    ),
    Mutation(
        id="auth-unknown-entity-not-repeated",
        behaviour="the auth CLI refusing an unknown company key without repeating the word",
        path=f"{SRC}/auth_cli.py",
        edits=(
            (
                """raise StartupError(f"unknown entity. Configured entities: {known}")""",
                """raise StartupError(f"unknown entity '{key}'. Configured entities: {known}")""",
            ),
        ),
        families=("tests/test_refused_command_lines.py", "tests/test_oauth_cli.py"),
    ),
    Mutation(
        id="registry-writes-value-not-repeated",
        behaviour="refusing a writes value without repeating it",
        path=f"{SRC}/registry.py",
        edits=(
            (
                """sets writes to something other than false; this \"\n""",
                """sets writes: {value['writes']!r}; this \"\n""",
            ),
        ),
        families=("tests/test_registry_and_startup.py",),
    ),
    Mutation(
        id="registry-key-the-redactor-would-cut",
        behaviour=(
            "refusing a key with eight digits in a row: every error prints a command "
            "nobody can paste, and two such companies look the same"
        ),
        path=f"{SRC}/registry.py",
        edits=(
            (
                """    if any(redact(str(key)) != str(key) for key in section):\n""",
                """    if False:\n""",
            ),
        ),
        families=("tests/test_registry_and_startup.py",),
    ),
    Mutation(
        id="transaction-columns-say-how-they-got-in",
        behaviour="list_transactions saying how each column got in: admitted_by is null there",
        path=f"{SRC}/tools_data.py",
        edits=(
            (
                """                            admitted_by=c.admitted_by,\n""",
                "",
            ),
        ),
        families=("tests/test_changes_and_tools.py",),
    ),
    Mutation(
        id="attachment-malformed-link-refused",
        behaviour="naming a malformed download link: it reaches the model as an unexpected error",
        path=f"{SRC}/attachments.py",
        edits=(
            (
                """    except httpx.InvalidURL:\n""",
                """    except KeyError:\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachment-unencodable-host-unresolved",
        behaviour="treating a host no DNS name can have as unresolved: Python's codec error escapes",
        path=f"{SRC}/attachments.py",
        edits=(
            (
                """    except (OSError, UnicodeError):  # UnicodeError: a label no DNS name can have.\n""",
                """    except OSError:\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachment-deadline-names-the-download",
        behaviour="a download's own deadline text: a model is told to try a shorter date window",
        path=f"{SRC}/attachments.py",
        edits=(
            (
                """        raise DeadlineExceeded(download_deadline_text(deadline.budget)) from exc\n""",
                """        raise\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="scrub-commits-and-tags-scanned",
        behaviour="reading commit and tag messages: an identifier in one passes",
        path="scripts/scrub_check.py",
        edits=(
            (
                """        elif kind in ("commit", "tag"):\n""",
                """        elif False:\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-object-hashes-skipped",
        behaviour=(
            "skipping full hashes in commits and tags: one revert in 25 quotes a hash that "
            "fails a clean repository, and only a history rewrite clears it"
        ),
        path="scripts/scrub_check.py",
        edits=(
            (
                """            contents[sha] = OBJECT_HASH.sub("", text)\n""",
                """            contents[sha] = text\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-names-scanned-in-the-tree",
        behaviour="scanning tracked file names: a file named for an identifier passes",
        path="scripts/scrub_check.py",
        edits=(
            (
                """            continue\n        found.extend((path, value) for value in identifiers_in(path))\n"""
                """        try:\n""",
                """            continue\n        try:\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-names-scanned-in-history",
        behaviour="scanning every path in history: a file deleted since, named for an identifier, passes",
        path="scripts/scrub_check.py",
        edits=(
            (
                """    for path in history_paths().union(paths.values()):\n""",
                """    for path in ():\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-printed-names-masked",
        behaviour="masking a printed file name: a file named for an identifier puts it in the log",
        path="scripts/scrub_check.py",
        edits=(
            (
                """    return LONG_DIGITS.sub("<digits>", path).encode("ascii", "backslashreplace").decode("ascii")\n""",
                """    return path.encode("ascii", "backslashreplace").decode("ascii")\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-short-object-fails",
        behaviour="failing on an object shorter than its header says: the scan passes on part of it",
        path="scripts/scrub_check.py",
        edits=(
            (
                """        if len(body) != size:\n            raise ScanIncomplete("git's object listing cut an object short")\n""",
                "",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-object-count-checked",
        behaviour="counting the objects git supplied: a listing that stops cleanly passes on what it held",
        path="scripts/scrub_check.py",
        edits=(
            (
                """    if read != len(shas):\n        raise ScanIncomplete(f"git supplied {read} of {len(shas)} objects")\n""",
                "",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-git-failure-is-incomplete",
        behaviour="a git that fails exiting 2, with the reason, instead of a traceback",
        path="scripts/scrub_check.py",
        edits=(
            (
                """    except (ScanIncomplete, subprocess.CalledProcessError, OSError) as exc:\n""",
                """    except ScanIncomplete as exc:\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-link-to-a-folder-is-skipped",
        behaviour="skipping a tracked link to a folder: a clean repository fails the scan",
        path="scripts/scrub_check.py",
        edits=(
            (
                """        except (FileNotFoundError, IsADirectoryError):\n""",
                """        except FileNotFoundError:\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-listing-keeps-names-exact",
        behaviour="taking file names unquoted: a non-ASCII name reads as deleted and its copy is skipped",
        path="scripts/scrub_check.py",
        edits=(
            (
                """        ["git", "ls-files", "-z"], capture_output=True, check=True,\n"""
                """    ).stdout.decode("utf-8", "surrogateescape").split("\\0")\n""",
                """        ["git", "ls-files"], capture_output=True, check=True,\n"""
                """    ).stdout.decode("utf-8", "surrogateescape").split("\\n")\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="redact-support-id-only-as-errors-print-it",
        behaviour="keeping a support id only after the exact text errors print: my_intuit_tid=<id> prints the id",
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """(?P<kept>(?<= intuit_tid=)""",
                """(?P<kept>(?<=intuit_tid=)""",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="redact-host-only-as-the-refusal-prints-it",
        behaviour="keeping a host only in the refusal that names it: any text saying download host keeps an id",
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """|(?<=the download host ')""",
                """|(?<=download host ')""",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="redact-host-keeps-its-underscores",
        behaviour="a kept host running past an underscore: the rest of the name is cut",
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """[A-Za-z0-9._-]+)"\n""",
                """[A-Za-z0-9.-]+)"\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="redact-token-rule-possessive",
        behaviour="the token rule staying linear: backtracking on a long dashed line takes half a minute",
        path=f"{SRC}/redaction.py",
        edits=(
            (
                """eyJ[A-Za-z0-9_-]{8,}+(?:""",
                """eyJ[A-Za-z0-9_-]{8,}(?:""",
            ),
        ),
        families=("tests/test_error_boundary.py",),
    ),
    Mutation(
        id="attachment-unbuildable-redirect-refused",
        behaviour="naming a redirect httpx can't build: it reaches the model as an unexpected error",
        path=f"{SRC}/attachments.py",
        edits=(
            (
                """            except (httpx.HTTPError, httpx.InvalidURL):\n""",
                """            except httpx.HTTPError:\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="attachment-unbuildable-client-refused",
        behaviour="naming a download client that can't be built: it reaches the model as an unexpected error",
        path=f"{SRC}/attachments.py",
        edits=(
            (
                """        client = http if http is not None else _build_client()\n    except OSError:\n""",
                """        client = http if http is not None else _build_client()\n    except ValueError:\n""",
            ),
        ),
        families=("tests/test_attachments.py",),
    ),
    Mutation(
        id="scrub-any-length-of-run",
        behaviour="catching a run past twenty digits: a longer id passes, and company ids run to 32",
        path="scripts/scrub_check.py",
        edits=(
            (
                """[0-9]{12,}(?![0-9])""",
                """[0-9]{12,20}(?![0-9])""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-history-names-read-as-bytes",
        behaviour="reading history's names as bytes: a name that isn't UTF-8 ends the scan in a traceback",
        path="scripts/scrub_check.py",
        edits=(
            (
                """        capture_output=True, check=True,\n    ).stdout.decode("utf-8", "surrogateescape")\n    mapping""",
                """        capture_output=True, text=True, check=True,\n    ).stdout\n    mapping""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-printed-names-are-text",
        behaviour="printing a name that isn't text: the report itself raises",
        path="scripts/scrub_check.py",
        edits=(
            (
                """.encode("ascii", "backslashreplace").decode("ascii")\n""",
                """\n""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
    Mutation(
        id="scrub-merge-names-seen",
        behaviour="reading what a merge added: a file added only in a merge goes unseen",
        path="scripts/scrub_check.py",
        edits=(
            (
                """["git", "log", "--all", "-m", "--format=", "--name-only", "-z"],""",
                """["git", "log", "--all", "--format=", "--name-only", "-z"],""",
            ),
        ),
        families=("tests/test_repo_hygiene.py",),
    ),
)


# --------------------------------------------------------------------------
# Mechanics
# --------------------------------------------------------------------------


class StaleEntry(Exception):
    """An edit no longer matches the source exactly once."""


def mutate(text: str, mutation: Mutation) -> str:
    """Apply every edit, or raise. A miss is never a skip.

    A mutated Python file must still compile. One that does not fails its
    family at import, which reads as "caught" and proves nothing about the
    behaviour: such an entry reports "1 error" where it should report a failing
    test.
    """
    for old, new in mutation.edits:
        occurrences = text.count(old)
        if occurrences != 1:
            raise StaleEntry(
                f"{mutation.id}: the edit matched {occurrences} times in "
                f"{mutation.path}, expected exactly 1. The gate no longer "
                f"describes this behaviour and would pass without testing it."
            )
        text = text.replace(old, new)
    if mutation.path.endswith(".py"):
        try:
            compile(text, mutation.path, "exec")
        except SyntaxError as exc:
            raise StaleEntry(
                f"{mutation.id}: the edit leaves {mutation.path} unparseable "
                f"({exc.msg}). Its family would fail on import, which is not "
                f"evidence that the behaviour is protected."
            ) from None
    return text


def check_every_entry_applies(root: Path = REPO_ROOT) -> list[str]:
    """Return one message per stale entry. Empty means the table is current."""
    problems: list[str] = []
    for mutation in MUTATIONS:
        target = root / mutation.path
        if not target.exists():
            problems.append(f"{mutation.id}: {mutation.path} does not exist")
            continue
        try:
            mutate(target.read_text(encoding="utf-8"), mutation)
        except StaleEntry as exc:
            problems.append(str(exc))
    return problems


def _child_env(copy: Path) -> dict[str, str]:
    # Nothing naming a repository: the hygiene tests run git in the copy, and
    # a GIT_DIR from a hook would point them back at the original.
    env = _git_environment(own_config=True)
    # Ahead of site-packages, so the copy wins over any installed or editable
    # build of the same package. The import control below proves it did.
    env["PYTHONPATH"] = str(copy / "src")
    return env


def import_control_passes(copy: Path) -> tuple[bool, str]:
    """A subprocess must import the package from the copy, not from anywhere else."""
    result = subprocess.run(
        [sys.executable, "-c", "import qbo_multicompany_mcp as m; print(m.__file__)"],
        cwd=copy, env=_child_env(copy), capture_output=True, text=True, check=False,
    )
    where = result.stdout.strip()
    return (result.returncode == 0 and where.startswith(str(copy)), where or result.stderr)


def run_families(copy: Path, families: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    # A mutation can make a test print bytes that aren't UTF-8. That's a
    # verdict to read, not a reason for the gate itself to stop.
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *families],
        cwd=copy, env=_child_env(copy), capture_output=True, text=True, errors="replace", check=False,
    )


def last_line(result: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else result.stderr.strip().splitlines()[-1:][0] if result.stderr else ""


_SUMMARY_FAILED = re.compile(r"(?<![\w-])(\d+) failed\b")
_SUMMARY_ERRORS = re.compile(r"(?<![\w-])(\d+) errors?\b")

CAUGHT = "caught"
SURVIVED = "survived"
INFRASTRUCTURE = "infrastructure"


def classify(result: subprocess.CompletedProcess[str]) -> str:
    """``caught``, ``survived`` or ``infrastructure``.

    Caught means the family ran and at least one test FAILED, with no error:
    pytest exits 1 for that and only that, and its summary line says how
    many. Exit 0 with no failure is a survivor. Everything else -- exit 2
    (interrupted, which is what a collection error is), 3 (internal error),
    4 (usage), 5 (nothing collected), a negative code (a signal), or a
    summary that names an error -- tested nothing, so it is neither.
    """
    summary = last_line(result)
    failed_match = _SUMMARY_FAILED.search(summary)
    errors_match = _SUMMARY_ERRORS.search(summary)
    failed = int(failed_match.group(1)) if failed_match else 0
    errors = int(errors_match.group(1)) if errors_match else 0
    if result.returncode == 0 and failed == 0 and errors == 0:
        return SURVIVED
    if result.returncode == 1 and failed >= 1 and errors == 0:
        return CAUGHT
    return INFRASTRUCTURE


def collection_control_passes(copy: Path) -> tuple[bool, str]:
    """A planted collection error must read as infrastructure, never as caught.

    The file is written into the copy's tests directory, run on its own, and
    removed. Its import fails before any test is collected, which is exactly
    the outcome a verdict of ``returncode != 0`` would count as detection.
    """
    planted = copy / "tests" / "test_zz_planted_collection_error.py"
    planted.write_text(
        "import qbo_mutation_gate_module_that_does_not_exist  # noqa: F401\n"
        "\n"
        "def test_never_runs() -> None:\n"
        "    raise AssertionError('collected, which it must not be')\n",
        encoding="utf-8",
    )
    try:
        result = run_families(copy, (str(planted.relative_to(copy)),))
    finally:
        planted.unlink()
    verdict = classify(result)
    return verdict == INFRASTRUCTURE, f"{verdict}: {last_line(result)}"


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", help="run one entry by id (repeatable)")
    parser.add_argument("--list", action="store_true", help="print the table and stop")
    args = parser.parse_args()

    if args.list:
        for mutation in MUTATIONS:
            marker = "CONTROL " if mutation.control else ""
            print(f"{mutation.id:28} {marker}{mutation.behaviour}")
        return 0

    stale = check_every_entry_applies()
    if stale:
        print("mutation-gate: THE GATE IS STALE -- it would pass without testing anything")
        for problem in stale:
            print(f"  {problem}")
        return 2

    selected = [m for m in MUTATIONS if not args.only or m.id in args.only]
    if not selected:
        print(f"mutation-gate: no entry matches {args.only}", file=sys.stderr)
        return 2
    # The control runs first, whatever was selected: a harness that can't catch
    # it can't vouch for any entry after it.
    control = next(m for m in MUTATIONS if m.control)
    selected = [control] + [m for m in selected if m is not control]

    with tempfile.TemporaryDirectory(prefix="qbo-mutation-gate-") as workspace:
        copy = Path(workspace) / "tree"
        print(f"mutation-gate: copying the tree to {copy}")
        try:
            copy_tree(REPO_ROOT, copy)
        except (OSError, subprocess.CalledProcessError) as exc:
            said = getattr(exc, "stderr", None)
            detail = said.decode("utf-8", "replace") if isinstance(said, bytes) and said else str(exc)
            # git puts warnings before its errors and advice after them: the
            # cause is the first fatal or error line, else the first that isn't
            # a hint.
            lines = detail.strip().splitlines()
            cause = next((line for line in lines if line.startswith(("fatal:", "error:"))), None) or next(
                (line for line in lines if not line.startswith("hint:")), "no detail"
            )
            print("mutation-gate: COULD NOT COPY THE TREE -- the gate copies the files git lists into a")
            print("  fresh repository, so it needs a git checkout and git 2.32 or later on PATH.")
            print(f"  {type(exc).__name__}: {cause}")
            return 2

        reaches, where = import_control_passes(copy)
        if not reaches:
            print("mutation-gate: IMPORT CONTROL FAILED -- the subprocess imports")
            print(f"  {where}")
            print("  which is not inside the copy, so no mutation would take effect.")
            return 2
        print(f"mutation-gate: import control ok ({where})")

        fires, detail = collection_control_passes(copy)
        if not fires:
            print("mutation-gate: THE COLLECTION CONTROL DID NOT FIRE -- a planted collection")
            print(f"  error was classified as {detail}")
            print("  so an entry that broke the tree would be reported as caught.")
            return 2
        print(f"mutation-gate: collection control ok ({detail})")

        families = sorted({family for m in selected for family in m.families})
        baseline = run_families(copy, tuple(families))
        if baseline.returncode != 0:
            print("mutation-gate: BASELINE FAILED -- these families do not pass unmutated,")
            print("  so a failure under mutation would prove nothing.")
            print(baseline.stdout[-4000:])
            return 2
        print(f"mutation-gate: baseline ok ({last_line(baseline)})")

        survivors: list[Mutation] = []
        broken: list[tuple[Mutation, str]] = []

        for mutation in selected:
            target = copy / mutation.path
            pristine = target.read_text(encoding="utf-8")
            target.write_text(mutate(pristine, mutation), encoding="utf-8")
            try:
                result = run_families(copy, mutation.families)
            finally:
                target.write_text(pristine, encoding="utf-8")

            verdict = classify(result)
            shown = {CAUGHT: "caught", SURVIVED: "SURVIVED", INFRASTRUCTURE: "BROKEN"}[verdict]
            print(f"  {mutation.id:28} {shown:9} {last_line(result)}")
            if mutation.control:
                if verdict != CAUGHT:
                    print()
                    print("mutation-gate: THE CONTROL DID NOT FIRE -- the gate proves nothing.")
                    print("  A behaviour whose tests are known to be alive was deleted and its")
                    print("  family reported no test failure, so the failure is in this harness.")
                    return 2
                continue
            if verdict == INFRASTRUCTURE:
                broken.append((mutation, last_line(result)))
            elif verdict == SURVIVED:
                survivors.append(mutation)

    if broken:
        print()
        print(f"mutation-gate: {len(broken)} run(s) tested nothing -- the family did not")
        print("  finish with a test failure, so the edit is neither caught nor survived.")
        for mutation, summary in broken:
            print(f"  {mutation.id}: {summary}")
        return 2

    if survivors:
        print()
        print(f"mutation-gate: FAIL -- {len(survivors)} behaviour(s) can be deleted unnoticed")
        for mutation in survivors:
            print(f"  {mutation.id}: {mutation.behaviour}")
            print(f"    families run: {', '.join(mutation.families)}")
        return 1

    print()
    print(
        f"mutation-gate: PASS -- control fired, "
        f"{len(selected) - 1} behaviour(s) protected"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
