#!/usr/bin/env python3
"""Scan tracked files and the history reachable from refs for identifiers that
should never have been committed.

The script carries its own control and **fails if the control does not fire**,
so a detector that breaks can't report clean.

Two exclusions, both narrow and both justified:

``uv.lock``
    Dependency hashes. A SHA-256 rendered as hex contains long runs of digits
    by chance. They are not identifiers.
Tree objects
    ``git cat-file -p`` renders a tree's binary entries as text, so raw SHA
    bytes surface as digit runs. Also not identifiers, and neither is a full
    object hash in a commit or tag: the header names them, and ``git revert``
    quotes one in the message. Failing on those would leave only a history
    rewrite as the way out.

Everything else is judged against the placeholder shape the repository's own
hygiene tests enforce: a long run of zeros with a short tail. That covers file
names, commit messages and tag messages as well as file contents.

A failure names the file and how many values it holds, never the values: a scan
log must not become a second copy of what it found. And a scan that couldn't
read something it set out to read exits 2 rather than passing on what it did.
"""

from __future__ import annotations

import collections
import pathlib
import re
import subprocess
import sys

#: The shape a synthetic identifier must have to be allowed in the tree.
PLACEHOLDER = re.compile(r"^0{8,}[0-9]{0,4}$")

#: A digit run long enough to be an identifier rather than a quantity.
LONG_DIGITS = re.compile(r"(?<![0-9])[0-9]{12,}(?![0-9])")

#: A full object hash, which can hold a long digit run by chance.
OBJECT_HASH = re.compile(r"(?<![0-9a-f])[0-9a-f]{40,64}(?![0-9a-f])")

EXCLUDED_PATHS = frozenset({"uv.lock"})

#: There is no tolerance list, not even an empty one: an empty list accepts a
#: first entry far more easily than absent machinery does. Adding a tolerance
#: should be a deliberate change, not filling in a blank.


class ScanIncomplete(Exception):
    """Something the scan set out to read could not be read."""


def object_paths() -> dict[str, str]:
    """Map object sha to the path it was last seen at."""
    out = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8", "surrogateescape")
    mapping: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, path = line.partition(" ")
        if sha:
            mapping[sha] = path
    return mapping


def history_paths() -> set[str]:
    """Every path any commit touched. ``rev-list`` names each object once, so
    two files with the same contents share one name there."""
    out = subprocess.run(
        ["git", "log", "--all", "-m", "--format=", "--name-only", "-z"],
        capture_output=True, check=True,
    ).stdout
    return {name.strip("\n") for name in out.decode("utf-8", "surrogateescape").split("\0")} - {""}


def object_contents(shas: list[str]) -> dict[str, str]:
    """One batch pass, rather than a subprocess per object."""
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        input="\n".join(shas).encode("ascii"), capture_output=True, check=True,
    )
    raw = proc.stdout
    contents: dict[str, str] = {}
    read = index = 0
    while index < len(raw):
        newline = raw.find(b"\n", index)
        if newline == -1:
            raise ScanIncomplete("git's object listing ended early")
        header = raw[index:newline].decode("ascii", "replace").split()
        if len(header) != 3:
            raise ScanIncomplete(f"git could not supply object {header[0][:8] if header else '?'}")
        sha, kind, size = header[0], header[1], int(header[2])
        start = newline + 1
        body = raw[start : start + size]
        if len(body) != size:
            raise ScanIncomplete("git's object listing cut an object short")
        read += 1
        text = body.decode("utf-8", "replace")
        if kind == "blob":
            contents[sha] = text
        elif kind in ("commit", "tag"):
            contents[sha] = OBJECT_HASH.sub("", text)
        index = start + size + 1
    if read != len(shas):
        raise ScanIncomplete(f"git supplied {read} of {len(shas)} objects")
    return contents


def identifiers_in(text: str) -> list[str]:
    return [value for value in LONG_DIGITS.findall(text) if not PLACEHOLDER.match(value)]


def masked(path: str) -> str:
    """``path`` fit to print: a file can be named after what it holds, and a
    name needn't be text."""
    return LONG_DIGITS.sub("<digits>", path).encode("ascii", "backslashreplace").decode("ascii")


def scan_objects() -> list[tuple[str, str]]:
    """Every blob, commit and tag reachable from a ref, and every path."""
    paths = object_paths()
    contents = object_contents(list(paths))
    found: list[tuple[str, str]] = []
    for sha, text in contents.items():
        path = paths.get(sha, "")
        if path in EXCLUDED_PATHS:
            continue
        found.extend((path or f"<object {sha[:8]}>", value) for value in identifiers_in(text))
    for path in history_paths().union(paths.values()):
        found.extend((path, value) for value in identifiers_in(path))
    return sorted(set(found))


def scan_worktree() -> list[tuple[str, str]]:
    """Tracked files as they stand now."""
    # -z: the plain listing quotes a non-ASCII name, which then reads as deleted.
    listing = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, check=True,
    ).stdout.decode("utf-8", "surrogateescape").split("\0")
    found: list[tuple[str, str]] = []
    for path in filter(None, listing):
        if path in EXCLUDED_PATHS:
            continue
        found.extend((path, value) for value in identifiers_in(path))
        try:
            text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError):
            continue  # Deleted, or a link or submodule: no file here to read.
        except OSError as exc:
            raise ScanIncomplete(f"could not read {masked(path)}: {exc.strerror}") from None
        found.extend((path, value) for value in identifiers_in(text))
    return sorted(set(found))


def control_fires() -> bool:
    """Prove the detector works before trusting a clean result."""
    # Assembled from parts: spelled out, this sample would itself be a long
    # digit run in a tracked file, and this script scans tracked files.
    sample = "realm " + "1234567890" + "123456" + " here"
    found = LONG_DIGITS.search(sample)
    return bool(found) and not PLACEHOLDER.match(found.group())


def report(found: list[tuple[str, str]], where: str) -> None:
    print(f"scrub-check: FAIL -- {len(found)} identifier(s) {where}")
    for path, count in sorted(collections.Counter(masked(path) for path, _ in found).items()):
        print(f"  {path}: {count} long digit run(s)")


def main() -> int:
    if not control_fires():
        print("scrub-check: DETECTOR IS DEAD -- control did not fire", file=sys.stderr)
        return 2

    try:
        # Any hit in the tree fails.
        in_tree = scan_worktree()
        if in_tree:
            report(in_tree, "in the working tree")
            return 1

        # So does any hit in history.
        offenders = scan_objects()
        if offenders:
            report(offenders, "in history")
            return 1
    except (ScanIncomplete, subprocess.CalledProcessError, OSError) as exc:
        print(f"scrub-check: INCOMPLETE -- {exc}", file=sys.stderr)
        return 2

    print("scrub-check: PASS -- control fired, tree and history clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
