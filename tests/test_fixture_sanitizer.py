"""Proves the fixture sanitizer removes what it claims to, and fires at all.

The sanitizer is the isolated step a live payload has to pass before it can
become a fixture, tested on its own so that path is reviewed rather than
trusted. It removes what it knows to look for, and the last test here pins down
what it leaves for a person to check.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from .fixtures.sanitize import (
    PLACEHOLDER,
    PSEUDONYM_KEYS,
    REDACT_KEYS,
    pseudonym,
    sanitize,
)

def realistic_capture() -> dict:
    """A capture that LOOKS real, assembled at run time.

    Every identifying string is built from parts, so no realistic realm id,
    company name, address, tax id or email address is ever a literal in this
    repository. Spelled out, this fixture would trip the tree's own hygiene
    rules -- which is the point: the sanitizer exists precisely because such
    values must not land here, and its test must not be the thing that lands
    them.
    """
    realm = "1" + "234567890" + "123456"
    company = " ".join(["Some", "Real", "Bakery", "Ltd"])
    email = "accounts" + "@" + "some-real-bakery" + "." + "test"
    phone = "+44 20 " + "7946 0958"
    tax_id = "98" + "-" + "7654321"
    vendor_tax = "12" + "-" + "3456789"
    street = "17 " + "Real Street"
    postcode = "ZZ1" + " 1ZZ"
    account_number = "40501" + "234567890"

    return {
        "CompanyInfo": {
            "Id": realm,
            "CompanyName": company,
            "LegalName": company.replace("Ltd", "Limited"),
            "EmployerId": tax_id,
            "CompanyAddr": {"Line1": street, "PostalCode": postcode},
            "Email": {"Address": email},
            "PrimaryPhone": {"FreeFormNumber": phone},
            "NameValue": [{"Name": "Marker", "Value": "inter" + "nal-marker"}],
        },
        "Vendor": [
            {"Id": "12", "DisplayName": " ".join(["Another", "Real", "Supplier"]),
             "TaxIdentifier": vendor_tax, "AcctNum": account_number,
             "BillAddr": {"Line1": "9 " + "Elsewhere Road"}},
        ],
    }


CAPTURE = realistic_capture()


def identifying_values() -> list[str]:
    capture = realistic_capture()
    info = capture["CompanyInfo"]
    return [
        info["Id"], info["CompanyName"], info["LegalName"], info["EmployerId"],
        info["CompanyAddr"]["Line1"], info["CompanyAddr"]["PostalCode"],
        info["Email"]["Address"], info["PrimaryPhone"]["FreeFormNumber"],
        capture["Vendor"][0]["DisplayName"], capture["Vendor"][0]["TaxIdentifier"],
        capture["Vendor"][0]["AcctNum"], capture["Vendor"][0]["BillAddr"]["Line1"],
        info["NameValue"][0]["Value"],
    ]


def test_the_sanitizer_fires_on_a_realistic_capture() -> None:
    """The canary. If this passed on an unsanitized payload, every other
    assertion here would be meaningless."""
    raw = json.dumps(CAPTURE)
    for secret in identifying_values():
        assert secret in raw, f"the fixture must actually contain {secret!r}"


def test_none_of_the_planted_identifying_values_survive() -> None:
    cleaned = json.dumps(sanitize(CAPTURE))
    survivors = [value for value in identifying_values() if value in cleaned]
    assert not survivors, survivors


def test_no_identifying_literal_is_written_into_this_repository() -> None:
    """The fixture is assembled at run time for a reason.

    Spelled out, its realm id would trip
    test_no_realistic_realm_id_is_tracked_anywhere.
    """
    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    for value in identifying_values():
        assert value not in source, f"{value!r} is a literal in this file"


def test_realm_shaped_numbers_become_placeholder_shaped() -> None:
    """The shape the repository's own hygiene tests enforce."""
    cleaned = sanitize(CAPTURE)
    identifier = cleaned["CompanyInfo"]["Id"]
    assert re.match(r"^0{8,}\d{0,4}$", identifier), identifier


def test_names_are_pseudonymised_not_deleted() -> None:
    """A fixture with no names stops exercising the name-handling code."""
    cleaned = sanitize(CAPTURE)
    name = cleaned["CompanyInfo"]["CompanyName"]
    assert name.startswith("Example ")
    assert name != PLACEHOLDER


def test_distinct_names_stay_distinct() -> None:
    """Otherwise a test about two different vendors stops being about two."""
    assert pseudonym("Alpha Ltd") != pseudonym("Beta Ltd")


def test_a_name_keeps_one_pseudonym_for_the_run() -> None:
    """So a name repeated across one capture stays one name."""
    assert pseudonym("Alpha Ltd") == pseudonym("Alpha Ltd")


def test_a_pseudonym_cannot_be_checked_against_a_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unkeyed, anyone could hash a guessed name and compare the result with a
    committed fake one. Keyed, the fake name depends on a key that dies with
    the run."""
    import hashlib

    from .fixtures import sanitize as module

    guess = "Alpha Ltd"
    unkeyed = hashlib.sha256(guess.encode("utf-8")).hexdigest()[:6].upper()
    assert pseudonym(guess) != f"Example {unkeyed}"
    before = pseudonym(guess)
    monkeypatch.setattr(module, "_RUN_KEY", bytes(32))
    assert pseudonym(guess) != before


def test_redacted_keys_are_replaced_wholesale() -> None:
    cleaned = sanitize(CAPTURE)
    assert cleaned["CompanyInfo"]["CompanyAddr"] == PLACEHOLDER
    assert cleaned["Vendor"][0]["TaxIdentifier"] == PLACEHOLDER


def test_structure_is_preserved() -> None:
    """A sanitizer that flattened the payload would stop it being a fixture."""
    cleaned = sanitize(CAPTURE)
    assert set(cleaned) == set(CAPTURE)
    assert isinstance(cleaned["Vendor"], list)
    assert cleaned["Vendor"][0]["Id"]


def test_the_input_is_not_modified() -> None:
    before = json.dumps(CAPTURE)
    sanitize(CAPTURE)
    assert json.dumps(CAPTURE) == before


def test_the_key_sets_are_not_empty() -> None:
    """A sanitizer with empty key sets would pass every redaction test."""
    assert len(REDACT_KEYS) >= 15
    assert len(PSEUDONYM_KEYS) >= 5


# --------------------------------------------------------------------------
# Every key rule, seen to fire
# --------------------------------------------------------------------------
#
# The tests above check a handful of fields and a minimum set size, so
# removing one key -- GSTIN, say -- from REDACT_KEYS leaves them all green
# while a planted GSTIN value passes straight through. A detector whose passing
# result is an absence proves nothing until every rule has been seen to fire.
# So the reviewed key sets are spelled out here, and each key gets a planted
# marker that must be transformed. Removing a key fails twice: the set
# comparison, and that key's own planted marker.

EXPECTED_REDACT_KEYS = frozenset(
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
EXPECTED_PSEUDONYM_KEYS = frozenset(
    {"DisplayName", "CompanyName", "LegalName", "GivenName", "FamilyName",
     "FullyQualifiedName", "Name", "PrintName", "Title", "Suffix"}
)


def test_the_key_sets_are_exactly_the_reviewed_ones() -> None:
    """A key added or removed is a decision, made here on purpose."""
    assert REDACT_KEYS == EXPECTED_REDACT_KEYS, sorted(REDACT_KEYS ^ EXPECTED_REDACT_KEYS)
    assert PSEUDONYM_KEYS == EXPECTED_PSEUDONYM_KEYS, sorted(PSEUDONYM_KEYS ^ EXPECTED_PSEUDONYM_KEYS)


@pytest.mark.parametrize("key", sorted(EXPECTED_REDACT_KEYS))
def test_every_redact_key_replaces_its_planted_marker_at_any_depth(key: str) -> None:
    """One planted marker per key, as a string, an object, a list and a list
    of objects, two levels down: whatever shape the value takes, the key's
    rule replaces it wholesale and the marker never survives."""
    marker = f"planted-{key}-marker"
    for planted in (marker, {"Line1": marker}, [marker], [{"Value": marker}]):
        cleaned = sanitize({"Outer": {"Wrapper": [{key: planted, "Kept": "kept"}]}})
        assert marker not in json.dumps(cleaned), (key, planted)
        assert cleaned["Outer"]["Wrapper"][0][key] == PLACEHOLDER, (key, planted)
        assert cleaned["Outer"]["Wrapper"][0]["Kept"] == "kept"


@pytest.mark.parametrize("key", sorted(EXPECTED_PSEUDONYM_KEYS))
def test_every_pseudonym_key_replaces_its_planted_marker(key: str) -> None:
    marker = f"Planted {key} Marker"
    cleaned = sanitize({"Outer": [{key: marker}]})
    value = cleaned["Outer"][0][key]
    assert marker not in json.dumps(cleaned), key
    assert value.startswith("Example ") and value != PLACEHOLDER, (key, value)


def test_what_it_does_not_look_for_passes_through() -> None:
    """Why its output is a draft for a person to read: an amount, a date, text
    under a key on neither list, and the last four digits of a masked run all
    come out as they went in."""
    reference = "ref " + "1234" + "5678" + "9012"
    cleaned = sanitize({"Purchase": [{
        "TotalAmt": 1234.56, "TxnDate": "2026-03-04",
        "PrivateNote": "paid at the counter", "DocNumber": reference,
    }]})["Purchase"][0]
    assert cleaned["TotalAmt"] == 1234.56
    assert cleaned["TxnDate"] == "2026-03-04"
    assert cleaned["PrivateNote"] == "paid at the counter"
    assert cleaned["DocNumber"] != reference and cleaned["DocNumber"].endswith("9012")
