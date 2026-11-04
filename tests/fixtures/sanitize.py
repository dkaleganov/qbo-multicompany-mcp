"""Sanitize a captured QuickBooks payload before it enters the repository.

A projection fixture taken from a live sandbox response has to be sanitized in
an isolated step before entering the tree. This is that step. It is a separate
module with its own tests, so the sanitizer can be reviewed on its own rather
than trusted because it happens to be called somewhere.

It removes what it knows to look for, and nothing else. Keys in
``REDACT_KEYS`` are replaced at any depth, keys in ``PSEUDONYM_KEYS`` get a
fake name, and every other string has its long digit runs and separated ids
masked, keeping the last four digits. Amounts, dates and free text under a key
on neither list pass through unchanged. Its output is a draft: read every value
before committing it, and replace any a real company could be recognised by.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping, Sequence
from typing import Any, Final

#: Realm-shaped digit runs become the placeholder shape the repository's own
#: hygiene tests enforce: a run of zeros with a short tail.
_LONG_DIGITS: Final = re.compile(r"(?<!\d)\d{9,20}(?!\d)")

#: Separated identifiers, which the digit-run rule above cannot see: an EIN
#: written with a hyphen after the second digit is a run of two digits and a
#: run of seven, and neither reaches nine. A shape like that is easy to miss,
#: which is why key-based redaction is the primary defence and this is the
#: backstop.
_SEPARATED_IDS: Final = re.compile(
    r"(?<!\d)(?:\d{2}-\d{7}|\d{3}-\d{2}-\d{4}|\d{3}-\d{3}-\d{3})(?!\d)"
)

#: What a redacted value becomes.
PLACEHOLDER: Final = "[redacted-by-fixture-sanitizer]"

#: Keys whose values are replaced wherever they appear, at any depth.
REDACT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "TaxIdentifier", "PrimaryTaxIdentifier", "SecondaryTaxIdentifier",
        "EmployerId", "SSN", "EIN", "GSTIN", "TaxRegistrationNumber",
        "AcctNum", "BankAccountNumber", "RoutingNumber",
        "PrimaryEmailAddr", "Email", "WebAddr",
        "PrimaryPhone", "AlternatePhone", "Mobile", "Fax",
        "BillAddr", "ShipAddr", "LegalAddr", "CompanyAddr", "RemitToAddr",
        "CustomerCommunicationAddr", "OtherAddr", "VendorAddr",
        "CreditChargeInfo", "CustomerMemo", "Notes", "PrintOnCheckName",
        "NameValue",
    }
)

#: Keys whose values are pseudonymised rather than dropped, because a fixture
#: with no names at all stops exercising the name-handling code.
PSEUDONYM_KEYS: Final[frozenset[str]] = frozenset(
    {"DisplayName", "CompanyName", "LegalName", "GivenName", "FamilyName",
     "FullyQualifiedName", "Name", "PrintName", "Title", "Suffix"}
)

#: A random key for this process alone. Unkeyed, a fake name could be checked
#: against a guessed real one by hashing the guess.
_RUN_KEY: Final = secrets.token_bytes(32)


def pseudonym(value: str, *, prefix: str = "Example") -> str:
    """An obviously-fake name for ``value``, the same one for the whole run.

    The same within a run, so a name repeated across a capture stays one name;
    different for two different names, which keeps a test about distinct
    vendors meaningful. Keyed with :data:`_RUN_KEY`, so a re-capture gets new
    fake names and a committed one can't be tested against a guess.
    """
    digest = hmac.new(_RUN_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()[:6].upper()
    return f"{prefix} {digest}"


def _mask_digits(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        original = match.group()
        tail = original[-4:]
        return "0" * max(8, len(original) - 4) + tail

    text = _SEPARATED_IDS.sub(PLACEHOLDER, text)
    return _LONG_DIGITS.sub(replace, text)


def sanitize(value: Any, *, _key: str | None = None) -> Any:
    """Recursively sanitize a captured payload.

    Returns a new structure; the input is never modified, so a caller that
    still holds the raw capture cannot be surprised by it having changed.
    """
    if _key in REDACT_KEYS:
        return PLACEHOLDER

    if isinstance(value, Mapping):
        return {key: sanitize(item, _key=str(key)) for key, item in value.items()}

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [sanitize(item, _key=_key) for item in value]

    if isinstance(value, str):
        if _key in PSEUDONYM_KEYS:
            return pseudonym(value)
        return _mask_digits(value)

    return value
