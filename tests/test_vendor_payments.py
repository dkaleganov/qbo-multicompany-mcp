"""Proves the candidate-payment scan classifies each documented case, and keeps
its language to what it can actually support.

One fixture per classification row, plus the cases where the honest answer is
"this needs a human".
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from qbo_multicompany_mcp.capabilities import Capabilities
from qbo_multicompany_mcp.errors import CapabilityUnavailable
from qbo_multicompany_mcp.vendor_payments import (
    DEDUPE_NOTE,
    NOT_A_CLASSIFIER_NOTE,
    Classification,
    MixedCurrency,
    aggregate,
    assert_single_currency,
    cents,
    classify_bill_payment,
    classify_journal_entry,
    classify_purchase,
    require_single_currency,
)


def vendor_ref(id_: str = "7", name: str = "Acme Supplies") -> dict:
    return {"value": id_, "name": name, "type": "Vendor"}


def purchase(**overrides) -> dict:
    base = {
        "Id": "1",
        "TxnDate": "2026-02-01",
        "EntityRef": vendor_ref(),
        "PaymentType": "Check",
        "TotalAmt": "100.00",
    }
    base.update(overrides)
    return base


def run(*rows_and_kinds) -> Classification:
    out = Classification()
    for kind, row in rows_and_kinds:
        {"purchase": classify_purchase,
         "billpayment": classify_bill_payment,
         "journal": classify_journal_entry}[kind](row, out)
    return out


# --------------------------------------------------------------------------
# One fixture per classification row
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payment_type", ["Cash", "Check"])
def test_non_card_purchase_types_land_in_non_card(payment_type: str) -> None:
    out = run(("purchase", purchase(PaymentType=payment_type)))
    assert len(out.rows) == 1
    assert out.rows[0].is_card is False
    assert out.rows[0].amount == Decimal("100.00")


def test_a_card_purchase_lands_in_card() -> None:
    out = run(("purchase", purchase(PaymentType="CreditCard")))
    assert out.rows[0].is_card is True


def test_a_card_refund_is_negative() -> None:
    """Credit is documented for CreditCard refunds."""
    out = run(("purchase", purchase(PaymentType="CreditCard", Credit=True)))
    assert out.rows[0].amount == Decimal("-100.00")


def test_the_credit_flag_is_ignored_on_cash_and_cheque() -> None:
    """It is documented ONLY for card refunds. Treating it as a general
    negative-amount flag would silently negate cash and cheque spend."""
    for payment_type in ("Cash", "Check"):
        out = run(("purchase", purchase(PaymentType=payment_type, Credit=True)))
        assert out.rows[0].amount == Decimal("100.00"), payment_type


@pytest.mark.parametrize("pay_type", ["Check", "CreditCard"])
def test_documented_bill_payment_types(pay_type: str) -> None:
    out = run(("billpayment", {"Id": "2", "VendorRef": vendor_ref(), "PayType": pay_type,
                               "TotalAmt": "50.00"}))
    assert len(out.rows) == 1
    assert out.rows[0].is_card is (pay_type == "CreditCard")


def test_there_is_no_cash_bill_payment() -> None:
    out = run(("billpayment", {"Id": "2", "VendorRef": vendor_ref(), "PayType": "Cash",
                               "TotalAmt": "50.00"}))
    assert out.rows == []
    assert out.unclassified[0].reason == "unrecognised_pay_type"


def test_a_negative_bill_payment_is_surfaced_not_absorbed() -> None:
    """Documented as impossible, so a negative means the record is not what the
    documentation describes."""
    out = run(("billpayment", {"Id": "2", "VendorRef": vendor_ref(), "PayType": "Check",
                               "TotalAmt": "-5.00"}))
    assert out.rows == []
    assert out.unclassified[0].reason == "negative_total_not_documented"


# --------------------------------------------------------------------------
# Payee typing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payee_type", ["Customer", "Employee"])
def test_non_vendor_payees_are_excluded_not_folded_in(payee_type: str) -> None:
    """Purchase.EntityRef may identify a Customer, a Vendor or an Employee."""
    out = run(("purchase", purchase(EntityRef={"value": "9", "name": "X", "type": payee_type})))
    assert out.rows == []
    assert out.excluded[f"payee_is_{payee_type.lower()}"] == 1


def test_a_payee_with_no_type_is_unclassified_not_assumed_a_vendor() -> None:
    out = run(("purchase", purchase(EntityRef={"value": "9", "name": "X"})))
    assert out.rows == []
    assert out.unclassified[0].reason == "payee_type_absent"


def test_a_missing_payee_goes_to_review_and_no_total() -> None:
    out = run(("purchase", purchase(EntityRef=None)))
    assert out.rows == []
    assert out.no_payee == ["1"]


# --------------------------------------------------------------------------
# Ambiguity and missing data
# --------------------------------------------------------------------------


def test_a_missing_amount_is_unclassified_never_zero() -> None:
    """Counting an unknown amount as zero would understate a total while
    looking complete."""
    out = run(("purchase", purchase(TotalAmt=None)))
    assert out.rows == []
    assert out.unclassified[0].reason == "amount_missing_or_unparseable"


def test_an_unparseable_amount_is_unclassified() -> None:
    out = run(("purchase", purchase(TotalAmt="about a hundred")))
    assert out.unclassified[0].reason == "amount_missing_or_unparseable"


def test_an_unrecognised_payment_type_is_unclassified_not_bucketed() -> None:
    """A 200 proves a value is accepted, not that the documented set is closed,
    so an unlisted value is surfaced rather than guessed into non-card."""
    out = run(("purchase", purchase(PaymentType="Crypto")))
    assert out.rows == []
    assert out.unclassified[0].reason == "unrecognised_payment_type"


# --------------------------------------------------------------------------
# Journal entries
# --------------------------------------------------------------------------


JOURNAL = {
    "Id": "30", "TxnDate": "2026-03-01", "TotalAmt": 0, "HomeTotalAmt": 0,
    "Line": [
        {"Id": "1", "Amount": "75.00",
         "JournalEntryLineDetail": {"PostingType": "Debit",
                                    "Entity": {"Type": "Vendor", "EntityRef": vendor_ref()}}},
        {"Id": "2", "Amount": "75.00",
         "JournalEntryLineDetail": {"PostingType": "Credit",
                                    "Entity": {"Type": "Vendor", "EntityRef": vendor_ref()}}},
        {"Id": "3", "Amount": "10.00",
         "JournalEntryLineDetail": {"PostingType": "Debit",
                                    "Entity": {"Type": "Customer",
                                               "EntityRef": {"value": "5"}}}},
    ],
}


def test_journal_entries_are_never_summed() -> None:
    out = run(("journal", JOURNAL))
    assert out.rows == [], "journal lines must not become payment rows"


def test_journal_review_lines_carry_direction_and_sign() -> None:
    """Header totals are documented as zero, so amounts come from signed lines."""
    out = run(("journal", JOURNAL))
    assert len(out.review_lines) == 2
    debit, credit = out.review_lines
    assert (debit.direction, debit.signed_amount) == ("Debit", Decimal("75.00"))
    assert (credit.direction, credit.signed_amount) == ("Credit", Decimal("-75.00"))
    assert debit.transaction_id == "30" and debit.line_id == "1"


def test_only_vendor_lines_are_reviewed() -> None:
    out = run(("journal", JOURNAL))
    assert all(line.vendor_id == "7" for line in out.review_lines)


def test_a_journal_header_total_is_never_read() -> None:
    """If the header were used, this 999 would appear somewhere."""
    out = run(("journal", {**JOURNAL, "TotalAmt": 999, "HomeTotalAmt": 999}))
    assert all(line.signed_amount != Decimal("999.00") for line in out.review_lines)


def test_an_unknown_posting_type_is_unclassified() -> None:
    out = run(("journal", {"Id": "31", "Line": [
        {"Id": "1", "Amount": "5.00",
         "JournalEntryLineDetail": {"PostingType": "Sideways",
                                    "Entity": {"Type": "Vendor", "EntityRef": vendor_ref()}}}]}))
    assert out.review_lines == []
    assert out.unclassified[0].reason == "line_posting_type_unknown"


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def test_inactive_vendors_are_matched_and_named() -> None:
    """A vendor deactivated in March was still paid in February."""
    out = run(("purchase", purchase()))
    totals = aggregate(out.rows, vendor_index={"7": {"display_name": "Retired Co", "active": False}})
    assert totals[0].display_name == "Retired Co"
    assert totals[0].active is False


def test_card_and_non_card_never_mix() -> None:
    out = run(
        ("purchase", purchase(Id="1", PaymentType="Check", TotalAmt="100.00")),
        ("purchase", purchase(Id="2", PaymentType="CreditCard", TotalAmt="40.00")),
    )
    total = aggregate(out.rows, vendor_index={})[0]
    assert total.non_card_amount == Decimal("100.00")
    assert total.card_amount == Decimal("40.00")


def test_include_card_false_withholds_detail_but_keeps_the_total() -> None:
    """include_card governs display of card detail, never whether card
    payments are counted."""
    out = run(("purchase", purchase(PaymentType="CreditCard", TotalAmt="40.00")))
    total = aggregate(out.rows, vendor_index={})[0]
    shown = total.to_json(include_card=True)
    withheld = total.to_json(include_card=False)
    assert shown["candidate_card_amount"] == "40.00"
    assert withheld["candidate_card_amount"] == "40.00"
    assert withheld["card_detail_withheld"] is True


def test_amounts_are_exact_cents_not_floats() -> None:
    out = run(
        ("purchase", purchase(Id="1", TotalAmt="0.10")),
        ("purchase", purchase(Id="2", TotalAmt="0.20")),
    )
    total = aggregate(out.rows, vendor_index={})[0]
    assert total.non_card_amount == Decimal("0.30")
    assert str(total.non_card_amount) == "0.30"  # 0.1 + 0.2 in float is not this


def test_year_boundary_rows_are_whatever_the_caller_asked_for() -> None:
    """Windowing is the query's job; classification does not silently re-filter
    by date, which would hide a window bug."""
    out = run(("purchase", purchase(TxnDate="2025-12-31")))
    assert out.rows[0].txn_date == "2025-12-31"


# --------------------------------------------------------------------------
# Currency
# --------------------------------------------------------------------------


def test_a_multi_currency_company_is_refused_before_any_request() -> None:
    caps = Capabilities(False, False, False, True, "Accrual")
    with pytest.raises(CapabilityUnavailable) as caught:
        require_single_currency(caps)
    text = str(caught.value)
    assert "multi-currency" in text
    assert "HomeTotalAmt" in text, "explains why a generic fallback is unsupported"


def test_a_single_currency_company_passes() -> None:
    require_single_currency(Capabilities(False, False, False, False, "Accrual"))
    require_single_currency(None)


def test_mixed_currency_rows_are_never_added() -> None:
    out = run(
        ("purchase", purchase(Id="1", CurrencyRef={"value": "USD"})),
        ("purchase", purchase(Id="2", CurrencyRef={"value": "EUR"})),
    )
    with pytest.raises(MixedCurrency):
        assert_single_currency(out.rows)


def test_a_single_currency_is_reported() -> None:
    out = run(("purchase", purchase(CurrencyRef={"value": "USD"})))
    assert assert_single_currency(out.rows) == "USD"


# --------------------------------------------------------------------------
# What this tool refuses to claim
# --------------------------------------------------------------------------


def test_economic_duplicates_are_out_of_scope_and_said_so() -> None:
    """One real payment recorded as both a Purchase and a BillPayment is two
    records. This cannot tell they are the same money."""
    assert "two different ids" in DEDUPE_NOTE
    assert "would be counted twice" in DEDUPE_NOTE


def test_the_envelope_language_stays_candidate_only() -> None:
    assert "not evidence" in NOT_A_CLASSIFIER_NOTE
    assert "No threshold" in NOT_A_CLASSIFIER_NOTE
    assert "no upper bound" in NOT_A_CLASSIFIER_NOTE


def test_no_threshold_or_bound_field_exists_on_the_envelope() -> None:
    """A canary: if someone adds one, this fails."""
    from qbo_multicompany_mcp.envelopes import CandidateVendorRow, VendorPaymentsEnvelope

    fields = set(VendorPaymentsEnvelope.model_fields) | set(CandidateVendorRow.model_fields)
    for banned in ("threshold", "meets_threshold", "upper_bound", "max_possible",
                   "reportable", "form_1099_required", "requires_1099"):
        assert banned not in fields


def test_cents_never_silently_becomes_a_float() -> None:
    assert cents("0.1") + cents("0.2") == Decimal("0.30")
    assert isinstance(cents("1"), Decimal)


# --------------------------------------------------------------------------
# An amount that parses but cannot be cents
# --------------------------------------------------------------------------


def _bill_payment(**overrides) -> dict:
    base = {"Id": "b1", "TxnDate": "2026-02-01", "VendorRef": vendor_ref(), "PayType": "Check",
            "TotalAmt": "50.00"}
    base.update(overrides)
    return base


def _journal(amount) -> dict:
    return {
        "Id": "j1", "TxnDate": "2026-02-01",
        "Line": [
            {
                "Id": "1", "Amount": amount,
                "JournalEntryLineDetail": {
                    "PostingType": "Debit",
                    "Entity": {"Type": "Vendor", "EntityRef": vendor_ref()},
                },
            }
        ],
    }


NOT_CENTS = ["Infinity", "-Infinity", "NaN", "sNaN", "1e400", "1" + "0" * 30, float("inf"), True]


@pytest.mark.parametrize("amount", NOT_CENTS, ids=[repr(a) for a in NOT_CENTS])
def test_a_non_finite_or_unrepresentable_purchase_amount_is_unclassified_never_zero(amount) -> None:
    """Decimal("Infinity") constructs, so a parse check alone accepts it. A
    cents conversion that then answered 0.00 would classify the row as a
    payment of nothing, with no review item, and the vendor total would say
    0.00 with a count of 1. An amount that is not a finite, representable
    number of cents is unknown, and unknown goes to review."""
    out = run(("purchase", purchase(TotalAmt=amount)))
    assert out.rows == []
    assert [u.reason for u in out.unclassified] == ["amount_missing_or_unparseable"]


@pytest.mark.parametrize("amount", ["Infinity", "NaN", "1" + "0" * 30], ids=["inf", "nan", "overflow"])
def test_a_non_finite_bill_payment_amount_is_unclassified(amount) -> None:
    out = run(("billpayment", _bill_payment(TotalAmt=amount)))
    assert out.rows == []
    assert [u.reason for u in out.unclassified] == ["amount_missing_or_unparseable"]


@pytest.mark.parametrize("amount", ["Infinity", "NaN", "1" + "0" * 30], ids=["inf", "nan", "overflow"])
def test_a_non_finite_journal_line_amount_is_unclassified(amount) -> None:
    out = run(("journal", _journal(amount)))
    assert out.review_lines == []
    assert [u.reason for u in out.unclassified] == ["line_amount_unparseable"]


def test_cents_refuses_what_it_cannot_represent() -> None:
    """The conversion itself never answers zero for a value it could not
    convert: the callers check first, and a caller that forgets gets an
    error rather than a silent 0.00."""
    for value in ("Infinity", "NaN", None, "about a hundred", "1" + "0" * 30):
        with pytest.raises(ValueError):
            cents(value)


def test_the_largest_representable_amount_is_still_cents() -> None:
    """The control: the boundary refuses the unrepresentable, not the large."""
    assert cents("9" * 20 + ".99") == Decimal("9" * 20 + ".99")


# ==========================================================================
# An object where one value belongs never becomes text
# ==========================================================================

#: What an object's text would carry into a row: a field nobody reviewed.
OBJECT_FIELD = {"PrimaryEmailAddr": {"Address": "nested-canary"}}


def _journal_line_id(line_id) -> dict:
    row = _journal("75.00")
    row["Line"][0]["Id"] = line_id
    return row


@pytest.mark.parametrize(
    ("kind", "row"),
    [
        pytest.param("purchase", purchase(Id=OBJECT_FIELD), id="purchase id"),
        pytest.param("purchase", purchase(TxnDate=OBJECT_FIELD), id="purchase date"),
        pytest.param("purchase", purchase(DocNumber=OBJECT_FIELD), id="document number"),
        pytest.param("purchase", purchase(CurrencyRef={"value": OBJECT_FIELD}), id="currency"),
        pytest.param("purchase", purchase(EntityRef=vendor_ref(name=OBJECT_FIELD)), id="payee name"),
        pytest.param("purchase", purchase(EntityRef=vendor_ref(id_=OBJECT_FIELD)), id="payee id"),
        pytest.param("purchase", purchase(EntityRef={**vendor_ref(), "type": OBJECT_FIELD}), id="payee type"),
        pytest.param("billpayment", _bill_payment(VendorRef=vendor_ref(name=OBJECT_FIELD)), id="bill payee name"),
        pytest.param("journal", _journal_line_id(OBJECT_FIELD), id="journal line id"),
    ],
)
def test_an_object_where_one_value_belongs_never_becomes_text(kind: str, row: dict) -> None:
    """Every part of a classification a result shows, the reasons a payment
    was excluded included: a payee type becomes part of that reason's name."""
    import json

    out = run((kind, row))
    shown = json.dumps({
        "rows": [r.to_json() for r in out.rows],
        "review": [line.to_json() for line in out.review_lines],
        "unclassified": [u.to_json() for u in out.unclassified],
        "no_payee": out.no_payee,
        "excluded": out.excluded,
        "vendors": [v.to_json(include_card=True) for v in aggregate(out.rows, vendor_index={})],
    })
    assert "nested-canary" not in shown


def test_the_fields_beside_it_still_come_through() -> None:
    """The control: an object is left out as one field, not the whole row."""
    out = run(("purchase", purchase(DocNumber=OBJECT_FIELD)))
    (row,) = out.rows
    assert row.doc_number is None
    assert (row.transaction_id, row.vendor_name, row.amount) == ("1", "Acme Supplies", Decimal("100.00"))


# ==========================================================================
# The two value sets say what the evidence supports
# ==========================================================================


def test_the_two_value_sets_claim_confirmation_of_the_documented_values_and_nothing_wider() -> None:
    """A 200 proves a value is accepted, not that the set is closed. The two
    value-set comments say exactly that, and never say 'complete' or 'against
    live sandbox' as if the set were a contract."""
    from qbo_multicompany_mcp import vendor_payments as VP

    raw = open(VP.__file__, encoding="utf-8").read()
    source = " ".join(raw.replace("#:", " ").split()).lower()
    assert source.count("every documented value confirmed; an unlisted value is unclassified, never guessed") == 2
    assert "confirmed complete" not in source
    assert "confirmed against live sandbox" not in source
