"""Candidate vendor payment totals for a calendar year.

**This is a cross-check against a bank ledger. It is not a filing classifier**,
and the language here stays that careful on purpose.

What that rules out, deliberately:

*No threshold flagging.* Nothing here says a vendor crossed a reporting
threshold. Payment type is an accounting signal, not evidence that money
settled or that any tax rule applies, and a tool that flagged thresholds would
be read as advice it is not entitled to give.

*No upper-bound field.* A bound cannot be proven while any amount is unknown.
A "maximum possible" that quietly excluded the rows it could not classify would
be a bound over the wrong set, stated with unearned confidence.

*Bills are never added to their payments.* A bill and the payment that settles
it are the same money recorded twice. Only payments are summed.

Three sources are walked:

``Purchase``
    Cash, Check and CreditCard spend. ``EntityRef`` may identify a Customer, a
    **Vendor** or an **Employee**, so the reference type is checked rather than
    the payee being assumed to be a vendor. It may also be absent entirely:
    2 of 35 purchases in the sandbox company have no payee at all, so the
    ``no_payee`` review list is an ordinary outcome rather than an edge case,
    and a non-empty one does not indicate a fault.
``BillPayment``
    Check and CreditCard only -- there is no cash bill payment -- and the total
    cannot be negative.
``JournalEntry``
    **For review only, never summed.** Its ``TotalAmt`` and ``HomeTotalAmt``
    are documented as always zero (Intuit's JournalEntry page), so any amount
    must come from validated signed lines. Review rows carry the transaction
    id, the line id, the debit/credit direction and the signed amount, and a
    human decides what they mean.

What deduplication does and does not cover: rows are deduplicated by source and
id, so the same record read twice across a page boundary is counted once.
**The same economic payment recorded under two different ids is outside that**
-- a Purchase and a BillPayment for one real payment are two distinct records
and this cannot tell they are the same money. The envelope says so rather than
leaving a reader to assume otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from .capabilities import Capabilities
from .errors import CapabilityUnavailable, QboMcpError
from .projections import as_str

CENTS: Final = Decimal("0.01")

DATE_BASIS: Final = "TxnDate"

SOURCE_PURCHASE: Final = "Purchase"
SOURCE_BILL_PAYMENT: Final = "BillPayment"
SOURCE_JOURNAL_ENTRY: Final = "JournalEntry"

#: ``Purchase.PaymentType``. **Every documented value confirmed; an unlisted
#: value is unclassified, never guessed.** Confirmed by the 2026-09-21 probe
#: against the sandbox company: a 200 proves a value is accepted, not that the
#: set is closed.
#:
#: An unlisted value is reported as unclassified rather than guessed into a
#: bucket. ``ORDERBY Id`` shows why: sandbox honours it, and Intuit's change log
#: says production stopped on 2026-01-27. Sandbox behaviour is evidence about
#: sandbox. A value set confirmed there is a good default, not a contract with
#: production.
PURCHASE_PAYMENT_TYPES: Final[frozenset[str]] = frozenset({"Cash", "Check", "CreditCard"})

#: ``BillPayment.PayType``. **Every documented value confirmed; an unlisted
#: value is unclassified, never guessed.** The same probe; there is no
#: documented cash bill payment.
BILL_PAYMENT_PAY_TYPES: Final[frozenset[str]] = frozenset({"Check", "CreditCard"})

CARD_TYPES: Final[frozenset[str]] = frozenset({"CreditCard"})

MULTI_CURRENCY_REFUSAL: Final = (
    "This company has multi-currency enabled, so candidate vendor payment "
    "totals are not produced for it. Amounts in different currencies cannot be "
    "added, and there is no validated currency-specific implementation here yet "
    "-- HomeTotalAmt exists only on some entity types, so a generic fallback to "
    "it is unsupported. Use list_transactions and get_transaction to read the "
    "underlying records instead."
)

DEDUPE_NOTE: Final = (
    "Rows are deduplicated by source and id, so one record read twice across a "
    "page boundary is counted once. The same economic payment recorded under "
    "two different ids -- for example a Purchase and a BillPayment for one real "
    "payment -- is NOT detected here and would be counted twice."
)

NOT_A_CLASSIFIER_NOTE: Final = (
    "Candidate totals for cross-checking against a bank ledger. Payment type is "
    "an accounting signal, not evidence that money settled or that any tax rule "
    "applies. No threshold is applied and no upper bound is claimed."
)


def parse_cents(value: Any) -> Decimal | None:
    """``value`` as an exact number of cents, or ``None`` when it is not one.

    Not one: absent, a boolean, text that is not a number, a number that is
    not finite (``Infinity``, ``NaN``), or one the cents quantization cannot
    represent within the decimal context. ``Decimal("Infinity")`` constructs
    happily, so "does it construct" is the wrong question: a check that stops
    there lets a conversion fail at quantizing, and one that swallows that
    failure and answers ``0.00`` classifies a payment of unknown size as a
    payment of nothing, with no review item. The one question that matters is
    whether the value can be the cents it will be summed as, and this asks
    exactly that.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite():
        return None
    try:
        return parsed.quantize(CENTS)
    except InvalidOperation:
        return None


def cents(value: Any) -> Decimal:
    """Exact cents. Never a float: a float total of money is wrong by design.

    Never zero for a value that could not be converted either: callers ask
    :func:`is_amount_parseable` first, and one that forgets gets an error
    rather than a silent ``0.00``.
    """
    parsed = parse_cents(value)
    if parsed is None:
        raise ValueError("amount is not a finite, representable number of cents")
    return parsed


def is_amount_parseable(value: Any) -> bool:
    return parse_cents(value) is not None


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@dataclass(slots=True)
class PaymentRow:
    source: str
    transaction_id: str
    txn_date: str | None
    vendor_id: str | None
    vendor_name: str | None
    payment_type: str | None
    is_card: bool
    amount: Decimal
    doc_number: str | None = None
    currency: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "transaction_id": self.transaction_id,
            "txn_date": self.txn_date,
            "vendor_id": self.vendor_id,
            "vendor_name": self.vendor_name,
            "payment_type": self.payment_type,
            "is_card": self.is_card,
            "amount": str(self.amount),
            "doc_number": self.doc_number,
            "currency": self.currency,
        }


@dataclass(slots=True)
class ReviewLine:
    """A journal-entry line touching a vendor. Never summed."""

    transaction_id: str
    line_id: str | None
    txn_date: str | None
    vendor_id: str | None
    vendor_name: str | None
    direction: str  # Debit | Credit
    signed_amount: Decimal

    def to_json(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "line_id": self.line_id,
            "txn_date": self.txn_date,
            "vendor_id": self.vendor_id,
            "vendor_name": self.vendor_name,
            "direction": self.direction,
            "signed_amount": str(self.signed_amount),
        }


@dataclass(slots=True)
class Unclassified:
    source: str
    transaction_id: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "transaction_id": self.transaction_id,
            "reason": self.reason,
        }


@dataclass(slots=True)
class Classification:
    rows: list[PaymentRow] = field(default_factory=list)
    review_lines: list[ReviewLine] = field(default_factory=list)
    unclassified: list[Unclassified] = field(default_factory=list)
    no_payee: list[str] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)

    def exclude(self, reason: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1


def _ref(row: Mapping[str, Any], key: str) -> tuple[str | None, str | None, str | None]:
    # Every value a row or total shows goes through as_str, as a projected
    # leaf does: an object where one value belongs becomes None, never its
    # text, which would carry whatever is nested in it into the result.
    ref = row.get(key)
    if not isinstance(ref, Mapping):
        return None, None, None
    return as_str(ref.get("value")), as_str(ref.get("name")), as_str(ref.get("type"))


def _id_of(row: Mapping[str, Any]) -> str:
    return as_str(row.get("Id")) or ""


def classify_purchase(row: Mapping[str, Any], out: Classification) -> None:
    """Classify one Purchase.

    ``EntityRef`` may point at a Customer, a Vendor or an Employee. The type is
    read rather than assumed, because assuming would silently fold employee
    reimbursements and customer refunds into vendor totals.
    """
    transaction_id = _id_of(row)
    vendor_id, vendor_name, entity_type = _ref(row, "EntityRef")

    if vendor_id is None:
        out.no_payee.append(transaction_id)
        return
    if entity_type is None:
        out.unclassified.append(
            Unclassified(SOURCE_PURCHASE, transaction_id, "payee_type_absent")
        )
        return
    if entity_type != "Vendor":
        out.exclude(f"payee_is_{entity_type.lower()}")
        return

    payment_type = row.get("PaymentType")
    payment_type = str(payment_type) if payment_type is not None else None
    if payment_type not in PURCHASE_PAYMENT_TYPES:
        out.unclassified.append(
            Unclassified(SOURCE_PURCHASE, transaction_id, "unrecognised_payment_type")
        )
        return

    if not is_amount_parseable(row.get("TotalAmt")):
        out.unclassified.append(
            Unclassified(SOURCE_PURCHASE, transaction_id, "amount_missing_or_unparseable")
        )
        return

    amount = cents(row.get("TotalAmt"))
    is_card = payment_type in CARD_TYPES

    # `Credit` is documented ONLY for CreditCard refunds (Intuit's Purchase
    # page). It is not a general negative-amount flag, so it is applied to card
    # rows and nowhere else -- treating it as general would silently negate
    # cash and cheque spend.
    if is_card and row.get("Credit") is True:
        amount = -amount

    currency = _currency_of(row)
    out.rows.append(
        PaymentRow(
            source=SOURCE_PURCHASE,
            transaction_id=transaction_id,
            txn_date=_text(row.get("TxnDate")),
            vendor_id=vendor_id,
            vendor_name=vendor_name,
            payment_type=payment_type,
            is_card=is_card,
            amount=amount,
            doc_number=_text(row.get("DocNumber")),
            currency=currency,
        )
    )


def classify_bill_payment(row: Mapping[str, Any], out: Classification) -> None:
    """Classify one BillPayment. The bill itself is never added to this."""
    transaction_id = _id_of(row)
    vendor_id, vendor_name, _ = _ref(row, "VendorRef")

    if vendor_id is None:
        out.no_payee.append(transaction_id)
        return

    pay_type = row.get("PayType")
    pay_type = str(pay_type) if pay_type is not None else None
    if pay_type not in BILL_PAYMENT_PAY_TYPES:
        out.unclassified.append(
            Unclassified(SOURCE_BILL_PAYMENT, transaction_id, "unrecognised_pay_type")
        )
        return

    if not is_amount_parseable(row.get("TotalAmt")):
        out.unclassified.append(
            Unclassified(
                SOURCE_BILL_PAYMENT, transaction_id, "amount_missing_or_unparseable"
            )
        )
        return

    amount = cents(row.get("TotalAmt"))
    if amount < 0:
        # The documented meaning is a sum of payment lines (Intuit's
        # BillPayment page), so a negative here means the record is not what
        # the documentation describes; it is surfaced rather than folded into
        # a total.
        out.unclassified.append(
            Unclassified(
                SOURCE_BILL_PAYMENT, transaction_id, "negative_total_not_documented"
            )
        )
        return

    out.rows.append(
        PaymentRow(
            source=SOURCE_BILL_PAYMENT,
            transaction_id=transaction_id,
            txn_date=_text(row.get("TxnDate")),
            vendor_id=vendor_id,
            vendor_name=vendor_name,
            payment_type=pay_type,
            is_card=pay_type in CARD_TYPES,
            amount=amount,
            doc_number=_text(row.get("DocNumber")),
            currency=_currency_of(row),
        )
    )


def classify_journal_entry(row: Mapping[str, Any], out: Classification) -> None:
    """Collect vendor-touching journal lines for review. Never sums.

    ``TotalAmt`` and ``HomeTotalAmt`` are documented as always zero on a
    JournalEntry (Intuit's JournalEntry page), so the header carries no usable
    amount at all. Everything here comes from signed lines.
    """
    transaction_id = _id_of(row)
    lines = row.get("Line")
    if not isinstance(lines, Sequence):
        return

    for line in lines:
        if not isinstance(line, Mapping):
            continue
        detail = line.get("JournalEntryLineDetail")
        if not isinstance(detail, Mapping):
            continue
        entity = detail.get("Entity")
        if not isinstance(entity, Mapping):
            continue
        if str(entity.get("Type") or "") != "Vendor":
            continue

        vendor_id, vendor_name, _ = _ref(entity, "EntityRef")
        posting = str(detail.get("PostingType") or "")
        if posting not in ("Debit", "Credit"):
            out.unclassified.append(
                Unclassified(
                    SOURCE_JOURNAL_ENTRY, transaction_id, "line_posting_type_unknown"
                )
            )
            continue
        if not is_amount_parseable(line.get("Amount")):
            out.unclassified.append(
                Unclassified(
                    SOURCE_JOURNAL_ENTRY, transaction_id, "line_amount_unparseable"
                )
            )
            continue

        magnitude = cents(line.get("Amount"))
        signed = magnitude if posting == "Debit" else -magnitude
        out.review_lines.append(
            ReviewLine(
                transaction_id=transaction_id,
                line_id=_text(line.get("Id")),
                txn_date=_text(row.get("TxnDate")),
                vendor_id=vendor_id,
                vendor_name=vendor_name,
                direction=posting,
                signed_amount=signed,
            )
        )


def _currency_of(row: Mapping[str, Any]) -> str | None:
    ref = row.get("CurrencyRef")
    if isinstance(ref, Mapping) and ref.get("value"):
        return as_str(ref["value"])
    return None


def _text(value: Any) -> str | None:
    return None if value in (None, "") else as_str(value)


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


@dataclass(slots=True)
class VendorTotal:
    vendor_id: str
    display_name: str | None
    active: bool | None
    non_card_amount: Decimal
    card_amount: Decimal
    payment_count: int
    sources: set[str] = field(default_factory=set)

    def to_json(self, *, include_card: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "vendor_id": self.vendor_id,
            "display_name": self.display_name,
            "active": self.active,
            "candidate_non_card_amount": str(self.non_card_amount),
            "payment_count": self.payment_count,
            "sources": sorted(self.sources),
        }
        # The card TOTAL is always reported: `include_card` governs whether
        # card DETAIL is displayed, never whether card payments are counted.
        out["candidate_card_amount"] = str(self.card_amount)
        if not include_card:
            out["card_detail_withheld"] = True
        return out


def aggregate(
    rows: Sequence[PaymentRow],
    *,
    vendor_index: Mapping[str, Mapping[str, Any]],
) -> list[VendorTotal]:
    """Total per vendor, keeping card and non-card apart.

    ``vendor_index`` must include **inactive** vendors. A vendor deactivated in
    March was still paid in February, and matching only active vendors would
    drop those payments or leave them nameless.
    """
    totals: dict[str, VendorTotal] = {}
    for row in rows:
        if row.vendor_id is None:
            continue
        entry = totals.get(row.vendor_id)
        if entry is None:
            record = vendor_index.get(row.vendor_id, {})
            entry = VendorTotal(
                vendor_id=row.vendor_id,
                display_name=record.get("display_name") or row.vendor_name,
                active=record.get("active"),
                non_card_amount=Decimal("0.00"),
                card_amount=Decimal("0.00"),
                payment_count=0,
            )
            totals[row.vendor_id] = entry
        if row.is_card:
            entry.card_amount += row.amount
        else:
            entry.non_card_amount += row.amount
        entry.payment_count += 1
        entry.sources.add(row.source)

    return sorted(
        totals.values(),
        key=lambda v: (-(v.non_card_amount + v.card_amount), v.vendor_id),
    )


def require_single_currency(capabilities: Capabilities | None) -> None:
    """Refuse aggregates for a multi-currency company.

    Amounts in different currencies are never added. ``HomeTotalAmt`` exists
    only on some entity types, so a generic fallback to it is unsupported --
    and silently summing mixed currencies would produce a number that looks
    like money and is not.
    """
    if capabilities is not None and capabilities.multi_currency:
        raise CapabilityUnavailable(MULTI_CURRENCY_REFUSAL)


class MixedCurrency(QboMcpError):
    """Rows carried more than one currency despite a single-currency company."""


def assert_single_currency(rows: Sequence[PaymentRow]) -> str | None:
    """Confirm every row shares one currency, and return it."""
    seen = {row.currency for row in rows if row.currency}
    if len(seen) > 1:
        raise MixedCurrency(
            "these records carry more than one currency, so they cannot be "
            "totalled. " + MULTI_CURRENCY_REFUSAL
        )
    return next(iter(seen)) if seen else None
