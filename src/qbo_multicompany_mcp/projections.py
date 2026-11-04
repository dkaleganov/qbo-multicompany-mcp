"""Projections: allowlists applied at every depth.

Every QuickBooks payload is projected through an explicit spec before any part
of it reaches a model. The projector walks the **spec**, never the payload.
That direction is the whole point: a denylist has to anticipate every field
Intuit might add, and Intuit adds fields. Walking the spec means a field nobody
here has heard of cannot appear in a result, today or after the next API
revision.

Two fields are deliberately truncated rather than dropped, because the whole
value is sensitive but the tail is useful for telling records apart:

``EmployerId``
    The company's federal tax id, returned when one is set (Intuit's
    CompanyInfo page, whose sample bodies show it unmasked). Only the last four
    digits leave this process.
``AcctNum``
    A user-defined chart-of-accounts code (Intuit's Account page). On Bank and
    Credit Card accounts only the last four leave this process; on every other
    account type the value passes through in full. That is a product policy,
    not a property of the field: users type real account numbers into the bank
    and card ones, and nothing here can say what a user typed into the others.

Addresses, phone numbers, email addresses and web addresses are not in any
spec. They are not projected, not truncated -- absent.

**A scalar leaf takes a scalar.** A ``Scalar`` rule names a leaf that holds
a string, a number or a boolean. When upstream puts an object or a list
there instead, the value is omitted -- at every depth, and before any cast
runs. Casting a container to text would publish its whole Python
representation, unreviewed child fields included, under an allowlisted key:
``PaymentMethodRef.value`` holding an object would land entire in
``payment_method.id``. Omission rather than an anomaly field, because the
entity envelopes carry no anomaly list and the spec's contract is that a
result holds only what the spec names, in the shape the spec names.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

#: Account types whose ``AcctNum`` is truncated to its last four.
_MASKED_ACCTNUM_TYPES: Final[frozenset[str]] = frozenset({"Bank", "Credit Card"})


def is_scalar(value: Any) -> bool:
    """A string, a number or a boolean: what a scalar leaf may hold.

    A mapping, a sequence, a set or anything else structured is not, and is
    never coerced to text. ``bytes`` is not either: no QuickBooks JSON leaf
    decodes to bytes, and a value that did would not be a reviewed field.
    """
    return isinstance(value, (str, int, float, bool))


# --------------------------------------------------------------------------
# The projector
# --------------------------------------------------------------------------


class Rule:
    """One allowlisted path. Subclasses define how the value is carried over."""

    source: str
    target: str

    def apply(self, payload: Mapping[str, Any], out: dict[str, Any]) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Scalar(Rule):
    source: str
    target: str
    cast: Callable[[Any], Any] | None = None

    def apply(self, payload: Mapping[str, Any], out: dict[str, Any]) -> None:
        if self.source not in payload:
            return
        value = payload[self.source]
        if value is None or not is_scalar(value):
            # A container at a scalar leaf is omitted, never stringified.
            return
        out[self.target] = self.cast(value) if self.cast else value


@dataclass(frozen=True, slots=True)
class Nested(Rule):
    source: str
    target: str
    spec: tuple[Rule, ...]

    def apply(self, payload: Mapping[str, Any], out: dict[str, Any]) -> None:
        value = payload.get(self.source)
        if not isinstance(value, Mapping):
            return
        projected = project(value, self.spec)
        if projected:
            out[self.target] = projected


@dataclass(frozen=True, slots=True)
class ListOf(Rule):
    source: str
    target: str
    spec: tuple[Rule, ...]

    def apply(self, payload: Mapping[str, Any], out: dict[str, Any]) -> None:
        value = payload.get(self.source)
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return
        out[self.target] = [
            project(item, self.spec) for item in value if isinstance(item, Mapping)
        ]


@dataclass(frozen=True, slots=True)
class Derived(Rule):
    """A value computed from the whole payload, not copied from one key.

    Used where the safe form of a field depends on another field -- masking
    ``AcctNum`` only for bank and credit-card accounts, for instance.

    ``emit_none`` matters for fields where ``None`` is itself the answer.
    ``tax_id_on_file`` is true, false, or "cannot be determined", and the third
    has to arrive as an explicit ``null`` rather than as an absent key -- an
    absent key reads as "no opinion offered", which is a different and weaker
    statement than "this could not be determined, here is why".
    """

    source: str
    target: str
    compute: Callable[[Mapping[str, Any]], Any]
    emit_none: bool = False

    def apply(self, payload: Mapping[str, Any], out: dict[str, Any]) -> None:
        value = self.compute(payload)
        if value is not None or self.emit_none:
            out[self.target] = value


def project(payload: Mapping[str, Any], spec: tuple[Rule, ...]) -> dict[str, Any]:
    """Apply ``spec`` to ``payload``, producing only what ``spec`` names."""
    out: dict[str, Any] = {}
    if not isinstance(payload, Mapping):
        return out
    for rule in spec:
        rule.apply(payload, out)
    return out


# --------------------------------------------------------------------------
# Casts
# --------------------------------------------------------------------------


def as_str(value: Any) -> str | None:
    """Text of a scalar; ``None`` for a container, which is never coerced."""
    return str(value) if is_scalar(value) else None


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def as_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def last4(value: Any) -> str | None:
    if not is_scalar(value):
        return None
    text = "".join(ch for ch in str(value) if ch.isalnum())
    return text[-4:] if len(text) >= 4 else None


# --------------------------------------------------------------------------
# CompanyInfo
# --------------------------------------------------------------------------

COMPANY_INFO_SPEC: Final[tuple[Rule, ...]] = (
    # `Id` is deliberately absent. The samples on Intuit's page show it as "1",
    # and whether a live company returns its realm id there has not been
    # observed. Either way the field adds nothing a caller needs, and if it is
    # the realm id, projecting it would publish what realm_id_last4 exists to
    # withhold. (Held by
    # test_a_successful_intuit_backed_result_carries_the_provenance_fields.)
    Scalar("CompanyName", "company_name", as_str),
    Scalar("LegalName", "legal_name", as_str),
    Scalar("CompanyStartDate", "company_start_date", as_str),
    Scalar("FiscalYearStartMonth", "fiscal_year_start_month", as_str),
    Scalar("Country", "country", as_str),
    Scalar("SupportedLanguages", "supported_languages", as_str),
    # The employer id comes back in clear and must never leave whole.
    Scalar("EmployerId", "employer_id_last4", last4),
    Nested(
        "MetaData",
        "metadata",
        (
            Scalar("CreateTime", "create_time", as_str),
            Scalar("LastUpdatedTime", "last_updated_time", as_str),
        ),
    ),
)
# Also absent on purpose, and not by oversight: CompanyAddr, LegalAddr,
# CustomerCommunicationAddr, PrimaryPhone, Email, WebAddr, and the NameValue
# list (which is a free-form bag that has been observed to carry contact data).


def project_company_info(payload: Mapping[str, Any]) -> dict[str, Any]:
    return project(payload, COMPANY_INFO_SPEC)


# --------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------

PREFERENCES_SPEC: Final[tuple[Rule, ...]] = (
    Nested(
        "AccountingInfoPrefs",
        "accounting",
        (
            Scalar("FirstMonthOfFiscalYear", "fiscal_year_start_month", as_str),
            Scalar("BookCloseDate", "book_close_date", as_str),
            Scalar("ClassTrackingPerTxn", "class_tracking_per_transaction", as_bool),
            Scalar("ClassTrackingPerTxnLine", "class_tracking_per_line", as_bool),
            Scalar("TrackDepartments", "track_departments", as_bool),
            Scalar("DepartmentTerminology", "department_terminology", as_str),
            Scalar("UseAccountNumbers", "use_account_numbers", as_bool),
        ),
    ),
    Nested(
        "CurrencyPrefs",
        "currency",
        (
            Scalar("MultiCurrencyEnabled", "multi_currency_enabled", as_bool),
            Nested("HomeCurrency", "home_currency", (Scalar("value", "value", as_str),)),
        ),
    ),
    Nested(
        "ReportPrefs",
        "reports",
        (
            Scalar("ReportBasis", "report_basis", as_str),
            Scalar("CalcAgingReportFromTxnDate", "aging_from_transaction_date", as_bool),
        ),
    ),
    Nested(
        "ProductAndServicesPrefs",
        "products_and_services",
        (
            Scalar("ForSales", "for_sales", as_bool),
            Scalar("ForPurchase", "for_purchase", as_bool),
            Scalar("QuantityWithPriceAndRate", "quantity_with_price_and_rate", as_bool),
            Scalar("QuantityOnHand", "quantity_on_hand", as_bool),
        ),
    ),
)
# Absent on purpose: EmailMessagesPrefs and SalesFormsPrefs carry customer-facing
# message templates and default email addresses; VendorAndPurchasesPrefs and
# TimeTrackingPrefs are not needed to answer anything this server serves.


def project_preferences(payload: Mapping[str, Any]) -> dict[str, Any]:
    return project(payload, PREFERENCES_SPEC)


def settings_from_preferences(preferences: Mapping[str, Any]) -> dict[str, Any]:
    """The flat ``settings`` block ``get_company_info`` returns.

    Built from the already-projected preferences, so it cannot reintroduce a
    field the projection removed.
    """
    accounting = preferences.get("accounting", {}) or {}
    currency = preferences.get("currency", {}) or {}
    reports = preferences.get("reports", {}) or {}
    home_currency = (currency.get("home_currency") or {}).get("value")
    return {
        "report_basis": reports.get("report_basis"),
        "fiscal_year_start_month": accounting.get("fiscal_year_start_month"),
        "book_close_date": accounting.get("book_close_date"),
        "home_currency": home_currency,
        "multi_currency_enabled": bool(currency.get("multi_currency_enabled", False)),
        "class_tracking_enabled": bool(
            accounting.get("class_tracking_per_transaction", False)
            or accounting.get("class_tracking_per_line", False)
        ),
        "department_tracking_enabled": bool(accounting.get("track_departments", False)),
        "department_terminology": accounting.get("department_terminology"),
    }


# --------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------


def _acct_num_last4(payload: Mapping[str, Any]) -> str | None:
    """Last four of ``AcctNum`` on Bank and Credit Card accounts; the full
    value on every other type.

    ``AcctNum`` is a user-defined chart-of-accounts code on every account
    (Intuit's Account page). Truncating the bank and card ones is a product
    policy, because users put real account numbers there. What a user typed
    into the others is not known here, so the full value passes through as the
    field's documented purpose intends -- not because it is known to be
    harmless.
    """
    raw = payload.get("AcctNum")
    if raw is None or not is_scalar(raw):
        return None
    if as_str(payload.get("AccountType", "")) in _MASKED_ACCTNUM_TYPES:
        return last4(raw)
    return as_str(raw)


ACCOUNT_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("Name", "name", as_str),
    Scalar("FullyQualifiedName", "fully_qualified_name", as_str),
    Scalar("Description", "description", as_str),
    Scalar("AccountType", "account_type", as_str),
    Scalar("AccountSubType", "account_sub_type", as_str),
    Scalar("Classification", "classification", as_str),
    Scalar("Active", "active", as_bool),
    Scalar("SubAccount", "sub_account", as_bool),
    # Two distinct balances, never combined. See project_account.
    Scalar("CurrentBalance", "current_balance", as_number),
    Scalar("CurrentBalanceWithSubAccounts", "current_balance_with_sub_accounts", as_number),
    Nested("ParentRef", "parent", (Scalar("value", "id", as_str),)),
    Nested("CurrencyRef", "currency", (Scalar("value", "value", as_str),)),
    Derived("AcctNum", "acct_num", _acct_num_last4),
)


def project_account(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project one account.

    ``current_balance`` and ``current_balance_with_sub_accounts`` are both
    carried through unchanged and are never added together anywhere in this
    package. ``CurrentBalanceWithSubAccounts`` on a parent *already contains*
    its children's balances; summing a parent's cumulative figure with its
    children double-counts every child, and the result looks plausible, which
    is what makes it dangerous. Hierarchy is preserved through ``parent.id`` so
    a caller can do whichever roll-up it actually wants.
    """
    return project(payload, ACCOUNT_SPEC)


# --------------------------------------------------------------------------
# Vendor
# --------------------------------------------------------------------------

#: Fixed reasons for an inconclusive ``tax_id_on_file``. Fixed, because this
#: field is exactly where a vague message becomes a false claim about a tax
#: record.
TAX_ID_NOT_RETURNED: Final = (
    "the TaxIdentifier field was not returned for this vendor. That can mean "
    "the connected user lacks permission to see it, not that no tax id is "
    "recorded, so neither answer can be given."
)


def _tax_id_on_file(payload: Mapping[str, Any]) -> tuple[bool | None, str | None]:
    """``True`` / ``False`` / ``None`` with a reason -- never digits.

    ``True`` means a value is recorded against the vendor. It does **not** mean
    the value is well-formed, that it matches anything, or that any filing
    obligation is satisfied; this package cannot see the value and would not
    report it if it could.

    ``None`` is the honest answer when the field is simply absent, because
    absence and "not permitted to see it" are indistinguishable from here.
    """
    if "TaxIdentifier" not in payload or payload.get("TaxIdentifier") is None:
        return None, TAX_ID_NOT_RETURNED
    text = str(payload["TaxIdentifier"]).strip()
    return (True, None) if text else (False, None)


VENDOR_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("DisplayName", "display_name", as_str),
    Scalar("CompanyName", "company_name", as_str),
    Scalar("Active", "active", as_bool),
    Scalar("Vendor1099", "vendor_1099", as_bool),
    Scalar("Balance", "balance", as_number),
    Nested("TermRef", "terms", (Scalar("value", "id", as_str), Scalar("name", "name", as_str))),
    Nested("CurrencyRef", "currency", (Scalar("value", "value", as_str),)),
    Derived("TaxIdentifier", "tax_id_on_file", lambda p: _tax_id_on_file(p)[0], emit_none=True),
    Derived("TaxIdentifier", "tax_id_on_file_reason", lambda p: _tax_id_on_file(p)[1]),
)
# Absent on purpose: BillAddr, PrimaryPhone, Mobile, Fax, PrimaryEmailAddr,
# WebAddr, AcctNum, PrintOnCheckName, Notes, and TaxIdentifier itself -- only
# the boolean above ever leaves this process.


# --------------------------------------------------------------------------
# Customer
# --------------------------------------------------------------------------

CUSTOMER_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("DisplayName", "display_name", as_str),
    Scalar("CompanyName", "company_name", as_str),
    Scalar("Active", "active", as_bool),
    Scalar("Job", "job", as_bool),
    Scalar("Balance", "balance", as_number),
    Scalar("BalanceWithJobs", "balance_with_jobs", as_number),
    Nested("ParentRef", "parent", (Scalar("value", "id", as_str),)),
    Nested("CurrencyRef", "currency", (Scalar("value", "value", as_str),)),
)
# Absent on purpose: every address, every contact field, Notes, and any tax
# identifier. Customers carry no tax ids, notes or contact fields out of here.


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------

_REF_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("value", "id", as_str),
    Scalar("name", "name", as_str),
)

#: Line-level tax code: whether tax was applied to a line and under which
#: code. Documented on every served line-detail type (the sample bodies on
#: Intuit's Purchase and Bill pages; its JournalEntry page).
_TAX_CODE_REF: Final = Nested("TaxCodeRef", "tax_code", _REF_SPEC)

#: A transaction line. `Description` is included: on an expense line it is the
#: primary descriptor and the whole point of reading the line. It is excluded
#: from nothing else, and `CustomerMemo` -- the customer-facing field -- is not
#: projected anywhere.
TRANSACTION_LINE_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("LineNum", "line_num", as_str),
    Scalar("Description", "description", as_str),
    Scalar("Amount", "amount", as_number),
    Scalar("DetailType", "detail_type", as_str),
    Nested("AccountBasedExpenseLineDetail", "expense_detail", (
        Nested("AccountRef", "account", _REF_SPEC),
        Nested("ClassRef", "class_ref", _REF_SPEC),
        Nested("CustomerRef", "customer", _REF_SPEC),
        Scalar("BillableStatus", "billable_status", as_str),
        _TAX_CODE_REF,
    )),
    Nested("ItemBasedExpenseLineDetail", "item_detail", (
        Nested("ItemRef", "item", _REF_SPEC),
        Nested("ClassRef", "class_ref", _REF_SPEC),
        Nested("CustomerRef", "customer", _REF_SPEC),
        Scalar("Qty", "quantity", as_number),
        Scalar("UnitPrice", "unit_price", as_number),
        _TAX_CODE_REF,
    )),
    Nested("JournalEntryLineDetail", "journal_detail", (
        Scalar("PostingType", "posting_type", as_str),
        Nested("AccountRef", "account", _REF_SPEC),
        Nested("Entity", "entity", (
            Scalar("Type", "type", as_str),
            Nested("EntityRef", "ref", _REF_SPEC),
        )),
        Nested("ClassRef", "class_ref", _REF_SPEC),
        Nested("DepartmentRef", "department", _REF_SPEC),
        _TAX_CODE_REF,
    )),
    Nested("SalesItemLineDetail", "sales_detail", (
        Nested("ItemRef", "item", _REF_SPEC),
        Nested("ClassRef", "class_ref", _REF_SPEC),
        Scalar("Qty", "quantity", as_number),
        Scalar("UnitPrice", "unit_price", as_number),
        _TAX_CODE_REF,
    )),
    Nested("DepositLineDetail", "deposit_detail", (
        Nested("AccountRef", "account", _REF_SPEC),
        Nested("Entity", "entity", _REF_SPEC),
        Nested("ClassRef", "class_ref", _REF_SPEC),
        Nested("PaymentMethodRef", "payment_method", _REF_SPEC),
    )),
    Nested("DiscountLineDetail", "discount_detail", (
        Nested("DiscountAccountRef", "discount_account", _REF_SPEC),
    )),
    ListOf("LinkedTxn", "linked_transactions", (
        Scalar("TxnId", "transaction_id", as_str),
        Scalar("TxnType", "transaction_type", as_str),
        Scalar("TxnLineId", "transaction_line_id", as_str),
    )),
)

#: Routing and structural references are kept after projection: the accounts
#: money moved between, the parties it involved, and the objects a transaction
#: hangs off. Each one here is documented for at least one served type on
#: Intuit's entity pages, and ``tests/test_detector_canaries.py`` carries a
#: preservation fixture per served type. A reference is always projected as a
#: reference -- id and name -- so a child Intuit adds later is dropped rather
#: than carried along.
TRANSACTION_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("DocNumber", "doc_number", as_str),
    Scalar("TxnDate", "txn_date", as_str),
    Scalar("DueDate", "due_date", as_str),
    Scalar("TotalAmt", "total_amount", as_number),
    # A Transfer's figure. It has no TotalAmt.
    Scalar("Amount", "amount", as_number),
    Scalar("HomeTotalAmt", "home_total_amount", as_number),
    Scalar("Balance", "balance", as_number),
    Scalar("ExchangeRate", "exchange_rate", as_number),
    Scalar("PrivateNote", "private_note", as_str),
    # Payment type fields.
    Scalar("PaymentType", "payment_type", as_str),
    Scalar("PayType", "pay_type", as_str),
    # A reference like every other. As a scalar it would carry the repr of the
    # whole dict, unknown children and all, in an "id" field.
    Nested("PaymentMethodRef", "payment_method", _REF_SPEC),
    Scalar("Credit", "credit", as_bool),
    Scalar("TotalTax", "total_tax", as_number),
    # Status.
    Scalar("TxnStatus", "txn_status", as_str),
    Scalar("POStatus", "po_status", as_str),
    Scalar("EmailStatus", "email_status", as_str),
    Scalar("PrintStatus", "print_status", as_str),
    Scalar("Adjustment", "adjustment", as_bool),
    # Refs.
    Nested("VendorRef", "vendor", _REF_SPEC),
    Nested("CustomerRef", "customer", _REF_SPEC),
    Nested("EntityRef", "entity_ref", (
        Scalar("value", "id", as_str),
        Scalar("name", "name", as_str),
        # Purchase.EntityRef may identify a Customer, a Vendor OR an Employee,
        # so the type travels with the reference and is never assumed.
        Scalar("type", "type", as_str),
    )),
    Nested("AccountRef", "account", _REF_SPEC),
    # The accounts at either end. Without these a Transfer keeps its own id and
    # loses both accounts, and a BillPayment loses the account that paid it.
    Nested("FromAccountRef", "from_account", _REF_SPEC),
    Nested("ToAccountRef", "to_account", _REF_SPEC),
    Nested("CheckPayment", "check_payment", (Nested("BankAccountRef", "bank_account", _REF_SPEC),)),
    Nested("CreditCardPayment", "credit_card_payment", (Nested("CCAccountRef", "cc_account", _REF_SPEC),)),
    Nested("APAccountRef", "ap_account", _REF_SPEC),
    Nested("DepositToAccountRef", "deposit_to_account", _REF_SPEC),
    Nested("CurrencyRef", "currency", (Scalar("value", "value", as_str),)),
    Nested("DepartmentRef", "department", _REF_SPEC),
    Nested("ClassRef", "class_ref", _REF_SPEC),
    Nested("SalesTermRef", "terms", _REF_SPEC),
    Nested("ProjectRef", "project", _REF_SPEC),
    Nested("RecurDataRef", "recurring_template", _REF_SPEC),
    # Tax structure: the transaction-level tax code and the rate behind each
    # tax line, so a reader can say whether tax was applied and how.
    # TxnTaxDetail is documented on Bill, CreditMemo, Deposit, Invoice,
    # JournalEntry, Purchase, PurchaseOrder, RefundReceipt and SalesReceipt.
    # References as references: a child Intuit adds later is dropped, not
    # carried.
    Nested("TxnTaxDetail", "tax_detail", (
        Nested("TxnTaxCodeRef", "txn_tax_code", _REF_SPEC),
        Scalar("TotalTax", "total_tax", as_number),
        ListOf("TaxLine", "tax_lines", (
            Scalar("Amount", "amount", as_number),
            Scalar("DetailType", "detail_type", as_str),
            Nested("TaxLineDetail", "tax_line_detail", (
                Nested("TaxRateRef", "tax_rate", _REF_SPEC),
                Scalar("PercentBased", "percent_based", as_bool),
                Scalar("TaxPercent", "tax_percent", as_number),
                Scalar("NetAmountTaxable", "net_amount_taxable", as_number),
            )),
        )),
    )),
    ListOf("LinkedTxn", "linked_transactions", (
        Scalar("TxnId", "transaction_id", as_str),
        Scalar("TxnType", "transaction_type", as_str),
        Scalar("TxnLineId", "transaction_line_id", as_str),
    )),
    ListOf("Line", "lines", TRANSACTION_LINE_SPEC),
)
# Absent on purpose:
#   TaxExemptionRef   - a reference whose name is the customer's state-issued
#                       tax exemption id (Intuit's Invoice page): an identifier
#                       about a person, not structure. Deliberately not
#                       projected.
#   ShipMethodRef     - a free-text shipping method; Intuit's Invoice page says
#                       there is no shipping method list. Not needed to read
#                       a transaction. Deliberately not projected.
#   ItemAccountRef    - the account behind a reimbursable line item (Intuit's
#                       page on linked transactions). Not needed to read a
#                       transaction; the line's AccountRef already routes it.
#                       Deliberately not projected.
#   CreditChargeInfo  - card holder name, number, expiry, auth codes. A live
#                       probe found it absent from all 16 sandbox payments.
#                       Absent in sandbox is not absent in production -- a
#                       sandbox company has no real card on file to return --
#                       so it stays excluded, which was always the behaviour
#                       regardless of the answer.
#   BillAddr / ShipAddr / RemitToAddr / VendorAddr - addresses.
#   CustomerMemo      - customer-facing text.
#   TxnSource         - transaction source.
#   SyncToken         - concurrency token; meaningless to a reader and a
#                       write-path artefact.
#   domain, sparse    - API plumbing.
#   MetaData          - excluded everywhere except the changes tool, which
#                       needs LastUpdatedTime to be about anything at all.

_METADATA_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("CreateTime", "create_time", as_str),
    Scalar("LastUpdatedTime", "last_updated_time", as_str),
)


def _with_metadata(spec: tuple[Rule, ...], include: bool) -> tuple[Rule, ...]:
    if not include:
        return spec
    return (*spec, Nested("MetaData", "metadata", _METADATA_SPEC))


def project_vendor(
    payload: Mapping[str, Any], *, include_metadata: bool = False
) -> dict[str, Any]:
    return project(payload, _with_metadata(VENDOR_SPEC, include_metadata))


def project_customer(
    payload: Mapping[str, Any], *, include_metadata: bool = False
) -> dict[str, Any]:
    return project(payload, _with_metadata(CUSTOMER_SPEC, include_metadata))


def project_transaction(
    payload: Mapping[str, Any], *, include_metadata: bool = False
) -> dict[str, Any]:
    """Project one transaction of any served type.

    One spec covers all thirteen types. The shapes overlap heavily, rules skip
    fields that are absent, and a spec per type would be thirteen places to
    forget the same exclusion.
    """
    return project(payload, _with_metadata(TRANSACTION_SPEC, include_metadata))


# --------------------------------------------------------------------------
# Reference entities
# --------------------------------------------------------------------------
#
# Finalised against live sandbox shapes observed 2026-09-24 (field names; every
# value below is synthetic in the tests) for Item, Term, PaymentMethod, TaxCode
# and TaxRate. Class and Department returned ZERO rows there -- class and
# department tracking are off on the sandbox company -- so their two specs are
# DOCUMENTATION-DERIVED from Intuit's pages. Each spec lists the fields it
# keeps. Kept: names, ids, active
# flags, hierarchy, Term days and discount terms, Item type and its three
# account references, TaxCode's flags and its rate lists' ids, TaxRate's value
# and agency. Excluded everywhere: SyncToken, domain, sparse, MetaData (except
# through list_changes, which needs LastUpdatedTime), LastModifiedByRef (a user
# identity), and every field not named.
#
# Free text: Item.Description, Item.PurchaseDesc, TaxCode.Description and
# TaxRate.Description are kept -- they are the item's and the code's own
# labels, not a person's data -- under the free-text disclosure every tool that
# returns company text carries.

#: Kept: Id, Name, ParentRef, SubClass, Active, FullyQualifiedName. Absent:
#: SyncToken, MetaData.
CLASS_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("Name", "name", as_str),
    Scalar("Active", "active", as_bool),
    Scalar("SubClass", "sub_class", as_bool),
    Nested("ParentRef", "parent", (Scalar("value", "id", as_str),)),
    Scalar("FullyQualifiedName", "fully_qualified_name", as_str),
)

#: Kept: Id, Name, ParentRef, Active, FullyQualifiedName, SubDepartment.
#: Absent: SyncToken, MetaData.
DEPARTMENT_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("Name", "name", as_str),
    Scalar("Active", "active", as_bool),
    Scalar("SubDepartment", "sub_department", as_bool),
    Nested("ParentRef", "parent", (Scalar("value", "id", as_str),)),
    Scalar("FullyQualifiedName", "fully_qualified_name", as_str),
)

#: Kept: Id, Name, InvStartDate, Type, QtyOnHand, AssetAccountRef, Sku,
#: TrackQtyOnHand, Description, SubItem, Taxable, PurchaseDesc, Active,
#: ClassRef, PurchaseCost, ParentRef, UnitPrice, FullyQualifiedName, Level,
#: IncomeAccountRef, ExpenseAccountRef. Absent on purpose: SyncToken,
#: SalesTaxIncluded, SalesTaxCodeRef, CustomExtensions, Source,
#: PurchaseTaxIncluded, ReorderPoint, MetaData, PrefVendorRef,
#: PurchaseTaxCodeRef, TaxClassificationRef -- none is needed to read what an
#: item is and where its money goes, and each is one more field to review
#: later.
ITEM_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("Name", "name", as_str),
    Scalar("FullyQualifiedName", "fully_qualified_name", as_str),
    Scalar("Active", "active", as_bool),
    Scalar("Type", "type", as_str),
    Scalar("Sku", "sku", as_str),
    Scalar("Description", "description", as_str),
    Scalar("PurchaseDesc", "purchase_description", as_str),
    Scalar("Taxable", "taxable", as_bool),
    Scalar("UnitPrice", "unit_price", as_number),
    Scalar("PurchaseCost", "purchase_cost", as_number),
    Scalar("QtyOnHand", "qty_on_hand", as_number),
    Scalar("TrackQtyOnHand", "track_qty_on_hand", as_bool),
    Scalar("InvStartDate", "inv_start_date", as_str),
    Scalar("SubItem", "sub_item", as_bool),
    Scalar("Level", "level", lambda v: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None),
    Nested("ParentRef", "parent", (Scalar("value", "id", as_str),)),
    Nested("ClassRef", "class_ref", _REF_SPEC),
    Nested("IncomeAccountRef", "income_account", _REF_SPEC),
    Nested("ExpenseAccountRef", "expense_account", _REF_SPEC),
    Nested("AssetAccountRef", "asset_account", _REF_SPEC),
)

#: Kept: Id, Name, DiscountPercent, DiscountDays, Active, Type, DayOfMonthDue,
#: DiscountDayOfMonth, DueNextMonthDays, DueDays. Absent: SyncToken, MetaData.
TERM_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("Name", "name", as_str),
    Scalar("Active", "active", as_bool),
    Scalar("Type", "type", as_str),
    Scalar("DueDays", "due_days", lambda v: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None),
    Scalar("DiscountDays", "discount_days", lambda v: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None),
    Scalar("DiscountPercent", "discount_percent", as_number),
    Scalar("DayOfMonthDue", "day_of_month_due", lambda v: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None),
    Scalar("DueNextMonthDays", "due_next_month_days", lambda v: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None),
    Scalar("DiscountDayOfMonth", "discount_day_of_month", lambda v: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None),
)

#: Kept: Id, Name, Active, Type. Absent: SyncToken, MetaData.
PAYMENT_METHOD_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("Name", "name", as_str),
    Scalar("Active", "active", as_bool),
    Scalar("Type", "type", as_str),
)

#: From a TaxRateList only the rate ids travel. In the samples on Intuit's
#: TaxCode page, TaxRateDetail carries TaxRateRef, TaxTypeApplicable and
#: TaxOrder; the ids are kept and nothing else.
_TAX_RATE_LIST_SPEC: Final[tuple[Rule, ...]] = (
    ListOf("TaxRateDetail", "rates", (Nested("TaxRateRef", "tax_rate", (Scalar("value", "id", as_str),)),)),
)

#: Kept: Id, Name, PurchaseTaxRateList, SalesTaxRateList, TaxGroup, Taxable,
#: Active, Description, Hidden, TaxCodeConfigType. Absent: SyncToken, MetaData.
TAX_CODE_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("Name", "name", as_str),
    Scalar("Active", "active", as_bool),
    Scalar("Taxable", "taxable", as_bool),
    Scalar("TaxGroup", "tax_group", as_bool),
    Scalar("Hidden", "hidden", as_bool),
    Scalar("TaxCodeConfigType", "config_type", as_str),
    Scalar("Description", "description", as_str),
    Derived("SalesTaxRateList", "sales_tax_rates",
            lambda p: project(p.get("SalesTaxRateList") or {}, _TAX_RATE_LIST_SPEC).get("rates")),
    Derived("PurchaseTaxRateList", "purchase_tax_rates",
            lambda p: project(p.get("PurchaseTaxRateList") or {}, _TAX_RATE_LIST_SPEC).get("rates")),
)

#: Kept: Id, RateValue, Name, AgencyRef, SpecialTaxType, DisplayType, Active,
#: Description. Absent: SyncToken, EffectiveTaxRate, TaxReturnLineRef,
#: MetaData, OriginalTaxRate.
TAX_RATE_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("Name", "name", as_str),
    Scalar("Active", "active", as_bool),
    Scalar("RateValue", "rate_value", as_number),
    Scalar("SpecialTaxType", "special_tax_type", as_str),
    Scalar("DisplayType", "display_type", as_str),
    Scalar("Description", "description", as_str),
    Nested("AgencyRef", "agency", (Scalar("value", "id", as_str),)),
)

#: A recorded Size written as text: digits, and optionally a decimal part.
_SIZE_TEXT: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?")


class UnreadableSize(ValueError):
    """A recorded Size that is present but not a non-negative whole number.
    Its text names the problem; it never repeats the value."""


def recorded_size(value: Any) -> int | None:
    """An Attachable's ``Size`` as the whole number of bytes it records.

    Intuit's page types Size as Decimal; the one value observed, in sandbox,
    was the JSON integer 671, the file's exact length. A whole number is read
    as itself however it is written -- 671, 671.0, "671" or "671.0" -- because
    reading one written as text as absent would switch off the exact-size
    check (a string has not been observed). ``None`` is absent, and a download
    then stays unsized. Anything else present raises
    :class:`UnreadableSize`, naming what is wrong, so 671.5 is never read as
    671 and -1 never passes as a size.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise UnreadableSize("is true or false")
    if isinstance(value, str):
        if not _SIZE_TEXT.fullmatch(value):
            raise UnreadableSize("is text that is not a non-negative number")
        number = Decimal(value)
        if number != number.to_integral_value():
            raise UnreadableSize("is not a whole number")
        return int(number)
    if isinstance(value, int):
        if value < 0:
            raise UnreadableSize("is negative")
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise UnreadableSize("is not a whole number")
        if value < 0:
            raise UnreadableSize("is negative")
        return int(value)
    raise UnreadableSize("is not a number")


def _listed_size(value: Any) -> int | None:
    """The listing's reading: an unreadable Size is shown as no size.
    get_attachment reads the same value with :func:`recorded_size`, and
    refuses the download instead."""
    try:
        return recorded_size(value)
    except UnreadableSize:
        return None


#: Kept: Id, FileName, Note, Category, ContentType, AttachableRef (EntityRef
#: type and value, IncludeOnSend), Tag, Size, and MetaData's two timestamps
#: (the list tool exposes created and updated). NEVER: TempDownloadUri and
#: ThumbnailTempDownloadUri (temporary signed links), FileAccessUri and
#: ThumbnailFileAccessUri (full paths), Long, Lat and PlaceName (where the
#: upload came from), SyncToken. The link the opt-in download uses is read from
#: the raw row inside get_attachment and leaves nowhere.
ATTACHABLE_SPEC: Final[tuple[Rule, ...]] = (
    Scalar("Id", "id", as_str),
    Scalar("FileName", "file_name", as_str),
    Scalar("ContentType", "content_type", as_str),
    Scalar("Size", "size", _listed_size),
    Scalar("Note", "note", as_str),
    Scalar("Category", "category", as_str),
    Scalar("Tag", "tag", as_str),
    ListOf("AttachableRef", "refs", (
        Derived("EntityRef", "entity_type",
                lambda p: as_str((p.get("EntityRef") or {}).get("type")) if isinstance(p.get("EntityRef"), Mapping) else None),
        Derived("EntityRef", "entity_id",
                lambda p: as_str((p.get("EntityRef") or {}).get("value")) if isinstance(p.get("EntityRef"), Mapping) else None),
        Scalar("IncludeOnSend", "include_on_send", as_bool),
    )),
    Derived("MetaData", "created_at",
            lambda p: as_str(p["MetaData"].get("CreateTime")) if isinstance(p.get("MetaData"), Mapping) else None),
    Derived("MetaData", "last_updated_at",
            lambda p: as_str(p["MetaData"].get("LastUpdatedTime")) if isinstance(p.get("MetaData"), Mapping) else None),
)


def project_attachable(payload: Mapping[str, Any], *, include_metadata: bool = False) -> dict[str, Any]:
    """The timestamps are always projected, so ``include_metadata`` adds nothing."""
    return project(payload, ATTACHABLE_SPEC)


REFERENCE_SPECS: Final[dict[str, tuple[Rule, ...]]] = {
    "Class": CLASS_SPEC,
    "Department": DEPARTMENT_SPEC,
    "Item": ITEM_SPEC,
    "Term": TERM_SPEC,
    "PaymentMethod": PAYMENT_METHOD_SPEC,
    "TaxCode": TAX_CODE_SPEC,
    "TaxRate": TAX_RATE_SPEC,
}

#: The two whose live shape has not been seen (the sandbox company has the
#: features off); named so a reader does not take the spec for an observation.
DOCUMENTATION_DERIVED_REFERENCE_SPECS: Final[frozenset[str]] = frozenset({"Class", "Department"})


def project_reference(
    entity: str, payload: Mapping[str, Any], *, include_metadata: bool = False
) -> dict[str, Any]:
    """Project one reference entity through its spec."""
    return project(payload, _with_metadata(REFERENCE_SPECS[entity], include_metadata))


def _reference_projector(entity: str) -> Callable[..., dict[str, Any]]:
    def projector(payload: Mapping[str, Any], *, include_metadata: bool = False) -> dict[str, Any]:
        return project_reference(entity, payload, include_metadata=include_metadata)

    projector.__name__ = f"project_{entity.lower()}"
    return projector


#: Every projector, by the entity name the query grammar uses. Keyed so a
#: newly served type cannot be reached by any route until it appears here --
#: this mapping is the "completed projection" the changes tool checks.
PROJECTORS: Final[dict[str, Any]] = {
    "Account": project_account,
    "Vendor": project_vendor,
    "Customer": project_customer,
    **{entity: _reference_projector(entity) for entity in REFERENCE_SPECS},
    "Attachable": project_attachable,
}


def register_transaction_projectors(transaction_types: Sequence[str]) -> None:
    for name in transaction_types:
        PROJECTORS.setdefault(name, project_transaction)


def has_projection(entity: str) -> bool:
    return entity in PROJECTORS


def project_entity(
    entity: str, payload: Mapping[str, Any], *, include_metadata: bool = False
) -> dict[str, Any]:
    projector = PROJECTORS.get(entity)
    if projector is None:
        raise KeyError(entity)
    if projector is project_account:
        return projector(payload)
    return projector(payload, include_metadata=include_metadata)
