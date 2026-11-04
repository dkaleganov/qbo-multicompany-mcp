"""The live-value registry: the credentials this process actually holds.

The redactor in :mod:`qbo_multicompany_mcp.redaction` matches *shapes*: a
bearer header, a JWT, a long opaque run, ``name=value``. A credential with none
of those shapes walks straight past it -- a short punctuated client secret in
an exception message, a token Intuit starts issuing in a format nobody here
anticipated. So alongside the shapes, this module keeps the exact values:

* the client secret, when app credentials are constructed;
* every access and refresh token received from Intuit's token endpoint;
* every access and refresh token decoded from the store or its journal.

Two consumers read it. Redaction removes each value whatever it looks like: a
whole message that is one, at any length, and one inside longer text from
:data:`TEXT_SCAN_MIN_LENGTH` characters up (the floor below). The network
policy refuses to let one reach a test-only API base, matched by identity
rather than by shape.

**Memory only.** Nothing here is written anywhere. The registry dies with the
process and holds nothing the process did not already hold -- a test asserts
this module cannot so much as import a way to write a file.

**Test fixtures are the one exemption, and it is explicit.** A token pair
marked ``synthetic`` is not registered, so the suite's loopback mock can
receive the fixture tokens it was built around. Nothing in the package ever
marks a pair synthetic; a test asserts that too. Every pair Intuit issues, and
every pair of unknown provenance on disk, is live by default.

**Identity is exact and does not depend on length.** Every value this process
holds is registered, however short, and :func:`holds` answers for any of
them: the refusal at a test-only base covers any value the store actually
holds. A length floor here would rest on a claim that Intuit issues no short
values, and no such claim is made.

**Scanning TEXT for a value is a different act, and it has a floor.** Redaction
and the request-body scan look for each value as a substring of arbitrary
text, and a two-letter test secret found that way would redact every ordinary
occurrence of those two letters in everything this process writes. So
:func:`snapshot` and :func:`found_in` skip values shorter than
:data:`TEXT_SCAN_MIN_LENGTH`. That floor is a false-positive policy for
substring scanning and nothing else -- never a statement about what Intuit
issues, and never applied to identity.
"""

from __future__ import annotations

import threading
from typing import Final

#: Values shorter than this are not searched for as substrings of text (see
#: the module docstring: a false-positive policy for scanning, not a claim
#: about credentials). Identity checks ignore it.
TEXT_SCAN_MIN_LENGTH: Final = 16

_VALUES: set[str] = set()
_LOCK = threading.Lock()


def register(*values: str | None) -> None:
    """Record each value as a live credential. Idempotent; never raises.

    Every non-empty string, whatever its length: identity must be able to
    answer for anything the store holds.
    """
    candidates = [v for v in values if isinstance(v, str) and v]
    if not candidates:
        return
    with _LOCK:
        _VALUES.update(candidates)


def holds(value: str) -> bool:
    """Whether ``value`` is exactly a registered credential, of any length."""
    with _LOCK:
        return value in _VALUES


def snapshot() -> tuple[str, ...]:
    """Every registered value long enough to search text for, longest first.

    Longest first so that when one value contains another, the whole of the
    longer one is removed rather than a fragment of it. Values under
    :data:`TEXT_SCAN_MIN_LENGTH` are left out of substring scanning only;
    :func:`holds` still knows them.
    """
    with _LOCK:
        long_enough = [v for v in _VALUES if len(v) >= TEXT_SCAN_MIN_LENGTH]
    return tuple(sorted(long_enough, key=len, reverse=True))


def found_in(text: str) -> bool:
    """Whether any registered value long enough to scan for occurs in ``text``."""
    return any(value in text for value in snapshot())
