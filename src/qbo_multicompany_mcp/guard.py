"""The company-name guard.

``realm_id`` is the identity. ``CompanyName`` is *verified*, never routed on.

The distinction matters because company names are not unique and not stable.
Two QuickBooks companies may carry byte-identical names -- an accountant running
"Holdings LLC" for two unrelated clients is ordinary -- and anyone with access
can rename a company at any time. Routing on a name would mean a rename
silently redirects every subsequent call.

So the guard runs the other way round: the realm decides where the request
goes, and the name that comes back is checked against what the registry claims.
A mismatch means the registry and reality have diverged, and this package
refuses to serve that entity until a human resolves it -- while every other
entity keeps working.

Verification happens **once per entity per process**, on first use, and again
after a re-consent (which may have pointed the entity at a different company
file entirely). Not on every call: that would double the request count for
every tool, permanently, to re-answer a question whose answer changes about
once a year.

The comparison is exact. The difference class below exists so that when it
fails, a human reading the CLI can see *why* in one word rather than staring at
two strings that look identical because the difference is a trailing space.
"""

from __future__ import annotations

import unicodedata
from typing import Final

_WHITESPACE_TRANSLATION: Final = {ord(c): " " for c in "\t\n\r\v\f   "}


def names_match(registry_name: str, intuit_name: str) -> bool:
    """Exact equality. Nothing is normalised away.

    A looser comparison would quietly accept the case where an operator
    believes they registered one company and in fact registered another whose
    name differs only in punctuation.
    """
    return registry_name == intuit_name


def difference_class(registry_name: str, intuit_name: str) -> str:
    """One word describing how two names differ: the first that explains it.

    ``whitespace`` -> ``case`` -> ``punctuation`` -> ``other``. Ordered from
    most-likely-a-typo to least, so the word points at the likeliest fix.
    """
    if registry_name == intuit_name:
        return "none"

    left = registry_name.translate(_WHITESPACE_TRANSLATION)
    right = intuit_name.translate(_WHITESPACE_TRANSLATION)
    if " ".join(left.split()) == " ".join(right.split()):
        return "whitespace"

    if left.casefold() == right.casefold():
        return "case"

    if _strip_punctuation(left) == _strip_punctuation(right):
        return "punctuation"

    return "other"


def _strip_punctuation(value: str) -> str:
    return " ".join(
        "".join(
            ch
            for ch in value
            if not unicodedata.category(ch).startswith(("P", "S"))
        ).split()
    ).casefold()


def describe_mismatch(registry_name: str, intuit_name: str) -> str:
    """The CLI's mismatch report.

    Both values are printed with ``repr()`` so an invisible difference -- a
    trailing space, a non-breaking space, a curly apostrophe -- is visible on
    the page. This text is for an operator at a terminal; it never goes to a
    model, and :func:`qbo_multicompany_mcp.errors.guard_mismatch_text` (which
    echoes neither name) is what a tool returns.
    """
    return (
        f"  registry: {registry_name!r}\n"
        f"  intuit:   {intuit_name!r}\n"
        f"  differs by: {difference_class(registry_name, intuit_name)}"
    )
