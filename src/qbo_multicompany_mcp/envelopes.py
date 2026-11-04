"""Envelope models: one validated shape per tool result.

Each tool is annotated ``Annotated[CallToolResult, SomeEnvelope]``. The SDK
reads the metadata arm, publishes its JSON schema as the tool's
``outputSchema``, and validates the ``structured_content`` of whatever the
handler returns against it. The handler builds both halves of the result --
the compact JSON text block and ``structured_content`` -- from the *same*
validated envelope instance, so the two can never drift apart.

Every model sets ``extra="forbid"``. That is deliberate coupling: if a
projection starts emitting a key no model declares, the result fails validation
loudly in the test suite rather than quietly carrying an unreviewed QuickBooks
field to a model.

``display_name_from_registry`` is spelled that way on purpose. The name is what
the local ``entities.yaml`` claims, not what QuickBooks reports; a caller
reading a bare ``display_name`` would reasonably assume the opposite.
``company_name_matches_registry`` is the field that says whether the two agree.

``realm_id_last4`` is diagnostic -- enough to tell two connected companies
apart in a transcript, not enough to address an API call. The full realm id
never appears in a result, an error or a log.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Shared blocks
# --------------------------------------------------------------------------


class CapabilitiesBlock(Strict):
    """What this company's QuickBooks plan and settings actually support."""

    class_tracking: bool
    department_tracking: bool
    inventory: bool
    multi_currency: bool
    accounting_basis: str
    accounts_payable: Literal["available", "unavailable", "unknown"] = Field(
        default="unknown",
        description="Established by first use, not by Preferences and not by the "
        "chart of accounts: 'unavailable' after a BillPayment read QuickBooks "
        "refused with its feature fault, 'available' after one that succeeded, "
        "'unknown' until either. Asked again after a re-consent.",
    )
    accounts_payable_fault_code: str | None = Field(
        default=None, description="The fault code that established 'unavailable'."
    )
    fiscal_year_start_month: int | None = Field(
        default=None,
        description="FirstMonthOfFiscalYear as a month number 1-12, from Preferences; "
        "null when Preferences carried nothing readable. The fiscal date macros "
        "resolve against it and are refused when it is unknown.",
    )


class CompanySettings(Strict):
    report_basis: str | None = None
    fiscal_year_start_month: str | None = None
    book_close_date: str | None = None
    home_currency: str | None = None
    multi_currency_enabled: bool = False
    class_tracking_enabled: bool = False
    department_tracking_enabled: bool = False
    department_terminology: str | None = None


class CompanyMetadata(Strict):
    create_time: str | None = None
    last_updated_time: str | None = None


class CompanyInfoBlock(Strict):
    # No `id`: whether a live company returns its realm id there hasn't been
    # observed, and the full realm id never leaves this server. Use
    # realm_id_last4 on the envelope.
    company_name: str | None = None
    legal_name: str | None = None
    company_start_date: str | None = None
    fiscal_year_start_month: str | None = None
    country: str | None = None
    supported_languages: str | None = None
    employer_id_last4: str | None = Field(
        default=None,
        description="Last four digits only. The employer id is returned in clear by "
        "QuickBooks and never leaves this server whole.",
    )
    metadata: CompanyMetadata | None = None


class AccountRef(Strict):
    id: str | None = None


class CurrencyRef(Strict):
    value: str | None = None


class AccountBlock(Strict):
    id: str | None = None
    name: str | None = None
    fully_qualified_name: str | None = None
    description: str | None = None
    account_type: str | None = None
    account_sub_type: str | None = None
    classification: str | None = None
    active: bool | None = None
    sub_account: bool | None = None
    current_balance: float | None = Field(
        default=None,
        description="This account's own balance, excluding sub-accounts.",
    )
    current_balance_with_sub_accounts: float | None = Field(
        default=None,
        description="This account's balance INCLUDING its sub-accounts. Already "
        "contains every child's balance: never add it to a child's figure.",
    )
    parent: AccountRef | None = None
    currency: CurrencyRef | None = None
    acct_num: str | None = Field(
        default=None,
        description="A user-defined chart-of-accounts code. Last four digits only "
        "on Bank and Credit Card accounts, by policy; passed through in full on "
        "every other account type, so treat it as text the user typed.",
    )


class IntuitBacked(Strict):
    """Fields every Intuit-backed success carries."""

    entity: str
    environment: str
    display_name_from_registry: str
    realm_id_last4: str
    request_started_at: datetime
    fetched_at: datetime


# --------------------------------------------------------------------------
# list_entities
# --------------------------------------------------------------------------


class HardExpiryBlock(Strict):
    """What is known of a refresh token's five-year limit."""

    basis: Literal["observed", "upper_bound_from_consent", "unknown"] = Field(
        description="'observed': Intuit's token response reported it. "
        "'upper_bound_from_consent': not reported; days is the documented "
        "five-year maximum counted from this store's consent date, so the true "
        "hard expiry is no later than that. 'unknown': not reported and not "
        "boundable (an imported grant, or a row of unknown origin)."
    )
    days: float | None = None
    note: str


class EntitySummary(Strict):
    entity: str
    display_name_from_registry: str
    environment: str
    realm_id_last4: str
    auth_state: Literal[
        "authorized",
        "needs_consent",
        "refresh_uncertain",
        "expired",
        "name_mismatch",
        "never_authorized",
        "malformed",
        "unavailable",
    ] = Field(
        description="'malformed': this realm's stored row could not be read and "
        "blocks this entity only; reconnect it. 'unavailable': the whole store "
        "could not be read."
    )
    last_refresh_at: datetime | None = None
    refresh_age_seconds: float | None = None
    stale_refresh: bool | None = Field(
        default=None,
        description="Computed per realm from this realm's own last_refresh_at. A "
        "passive observation, not an alert.",
    )
    days_to_refresh_token_expiry: float | None = Field(
        default=None,
        description="The INACTIVITY clock: 100 days, restarted when Intuit issues a new "
        "refresh-token value, at most once per 24 hours. "
        "Not the five-year hard limit; see refresh_token_hard_expiry.",
    )
    refresh_token_hard_expiry: HardExpiryBlock | None = None
    days_to_access_token_expiry: float | None = None
    company_name_matches_registry: bool | None = Field(
        default=None,
        description="The persisted guard verdict, when one was checked against "
        "this registry's current display_name. None when no verdict applies: "
        "never verified, or verified against a display_name that has since "
        "been changed.",
    )
    name_mismatch_checked_at: datetime | None = None
    capabilities: CapabilitiesBlock | None = Field(
        default=None,
        description="Present only when a previous call in some process cached it.",
    )


class ListEntitiesEnvelope(Strict):
    environment: str
    read_only: Literal[True] = True
    entity_count: int
    entities: list[EntitySummary]
    token_store_available: bool
    token_store_note: str | None = None
    fetched_at: datetime


# --------------------------------------------------------------------------
# server_info
# --------------------------------------------------------------------------


class ServerInfoEnvelope(Strict):
    name: str
    version: str
    build: str
    environment: str
    api_base: str
    read_only: Literal[True] = True
    writes_enabled: Literal[False] = False
    documents_enabled: bool
    entity_count: int
    authorized_entity_count: int
    token_store_protection: str
    token_store_available: bool
    token_store_note: str | None = None
    credential_use_enabled: bool = Field(
        description="False when the server has no app credentials configured: "
        "it still lists tools and answers the request-free diagnostics, and "
        "every call that would send a stored credential fails with a "
        "configuration error."
    )
    credential_use_note: str | None = Field(
        default=None, description="What to set, when credential use is disabled."
    )
    any_stale: bool | None = Field(
        default=None,
        description="True when at least one realm's own last_refresh_at is older "
        "than an hour. None when the store could not be read.",
    )
    stale_entities: list[str]
    fetched_at: datetime


# --------------------------------------------------------------------------
# get_company_info
# --------------------------------------------------------------------------


class CompanyInfoEnvelope(IntuitBacked):
    company_info: CompanyInfoBlock
    #: Both are ``null`` when ``company_name_matches_registry`` is false:
    #: Preferences are not fetched for a mismatched entity, and reporting every
    #: capability as ``false`` would be a lie dressed as an answer.
    settings: CompanySettings | None = None
    capabilities: CapabilitiesBlock | None = None
    guard_checked_at: datetime
    company_name_matches_registry: bool
    company_name_mismatch_difference_class: str | None = Field(
        default=None,
        description="One word describing how the registry name and the QuickBooks "
        "name differ (whitespace, case, punctuation, other). Present only on a "
        "mismatch, because a whitespace difference is invisible otherwise.",
    )
    guard_note: str | None = Field(
        default=None,
        description="Present only on a mismatch: what to do about it.",
    )


# --------------------------------------------------------------------------
# list_accounts
# --------------------------------------------------------------------------


class ListEnvelope(IntuitBacked):
    """The shape every list-returning tool uses.

    ``count``/``rows``/``truncated``/``duplicates_dropped`` are deliberately
    generic: one idea, one vocabulary. Eight list tools with eight shapes costs
    tokens on every call and invites a model to look in the wrong place.
    """

    count: int = Field(description="Number of rows returned, after deduplication.")
    truncated: bool = Field(
        description="True when rows were withheld -- by a client limit or by an "
        "upstream cap. False means the walk reached the end."
    )
    duplicates_dropped: int = Field(
        default=0,
        description="Rows discarded because another row carried the same "
        "identity. Non-zero is normal across page boundaries; it is reported "
        "rather than hidden because it is also how a paging bug shows itself.",
    )
    pages_walked: int = 0


class WalkedList(ListEnvelope):
    """A list produced by a paged walk.

    A walk is a best-effort read of a
    system somebody may be editing: count equality catches some omissions and
    cannot catch an amount changing between pages. Stating that in every result
    is the difference between a disclosed limitation and a hidden one.
    """

    walk_started_at: datetime
    walk_finished_at: datetime
    consistency: Literal["non_snapshot"] = "non_snapshot"


class ListAccountsEnvelope(WalkedList):
    """The chart of accounts, and it is a walk like any other.

    It reports its window and its consistency as every walked list does. Its
    rows carry money, which makes it the list where an undisclosed repeat
    costs the most.
    """

    capabilities: CapabilitiesBlock
    guard_checked_at: datetime
    company_name_matches_registry: bool
    rows: list[AccountBlock]


def dump(envelope: BaseModel) -> dict[str, Any]:
    """The single JSON form a result is built from.

    Both the text block and ``structured_content`` come from this one call, so
    the two halves of a result cannot disagree.
    """
    return envelope.model_dump(mode="json")


# ==========================================================================
# Data tools
# ==========================================================================


# -------------------------------------------------------------- vendors ---


class VendorRef(Strict):
    id: str | None = None
    name: str | None = None


class VendorBlock(Strict):
    id: str | None = None
    display_name: str | None = None
    company_name: str | None = None
    active: bool | None = None
    vendor_1099: bool | None = Field(
        default=None,
        description="The company's own 1099 flag on the vendor record. It is a "
        "bookkeeping setting, not a determination that any form is required.",
    )
    balance: float | None = None
    terms: VendorRef | None = None
    currency: CurrencyRef | None = None
    tax_id_on_file: bool | None = Field(
        default=None,
        description="Whether a tax identifier is recorded. Never the value. "
        "null means it could not be determined -- see tax_id_on_file_reason. "
        "true means a value exists, NOT that it is valid or that any filing "
        "obligation is met.",
    )
    tax_id_on_file_reason: str | None = None
    metadata: CompanyMetadata | None = None


class VendorListEnvelope(WalkedList):
    rows: list[VendorBlock]


# ------------------------------------------------------------ customers ---


class CustomerBlock(Strict):
    id: str | None = None
    display_name: str | None = None
    company_name: str | None = None
    active: bool | None = None
    job: bool | None = None
    balance: float | None = None
    balance_with_jobs: float | None = None
    parent: AccountRef | None = None
    currency: CurrencyRef | None = None
    metadata: CompanyMetadata | None = None


class CustomerListEnvelope(WalkedList):
    rows: list[CustomerBlock]


# ----------------------------------------------------------- run_query ---


class QueryEnvelope(WalkedList):
    statement: str = Field(
        description="The canonical statement actually sent. The caller's text is "
        "parsed and rebuilt, never forwarded, so this is what ran."
    )
    query_entity: str
    count_only: bool = False
    total_count: int | None = None
    rows: list[dict[str, Any]] = Field(
        description="Projected rows. The projection, not this schema, is what "
        "decides which fields may appear."
    )


# ----------------------------------------------------- one transaction ---


class LinkedTransaction(Strict):
    transaction_id: str | None = None
    transaction_type: str | None = None
    transaction_line_id: str | None = None


class LineExpenseDetail(Strict):
    account: VendorRef | None = None
    class_ref: VendorRef | None = None
    customer: VendorRef | None = None
    billable_status: str | None = None
    tax_code: VendorRef | None = None


class LineItemDetail(Strict):
    item: VendorRef | None = None
    class_ref: VendorRef | None = None
    customer: VendorRef | None = None
    quantity: float | None = None
    unit_price: float | None = None
    tax_code: VendorRef | None = None


class LineEntity(Strict):
    type: str | None = None
    ref: VendorRef | None = None


class LineJournalDetail(Strict):
    posting_type: str | None = None
    account: VendorRef | None = None
    entity: LineEntity | None = None
    class_ref: VendorRef | None = None
    department: VendorRef | None = None
    tax_code: VendorRef | None = None


class LineDepositDetail(Strict):
    account: VendorRef | None = None
    entity: VendorRef | None = None
    class_ref: VendorRef | None = None
    payment_method: VendorRef | None = None


class LineDiscountDetail(Strict):
    discount_account: VendorRef | None = None


class TransactionLine(Strict):
    id: str | None = None
    line_num: str | None = None
    description: str | None = None
    amount: float | None = None
    detail_type: str | None = None
    expense_detail: LineExpenseDetail | None = None
    item_detail: LineItemDetail | None = None
    journal_detail: LineJournalDetail | None = None
    sales_detail: LineItemDetail | None = None
    deposit_detail: LineDepositDetail | None = None
    discount_detail: LineDiscountDetail | None = None
    linked_transactions: list[LinkedTransaction] | None = None


class EntityRefBlock(Strict):
    id: str | None = None
    name: str | None = None
    type: str | None = Field(
        default=None,
        description="Purchase.EntityRef may identify a Customer, a Vendor or an "
        "Employee. The type travels with the reference and is never assumed.",
    )


class TaxLineDetailBlock(Strict):
    tax_rate: VendorRef | None = None
    percent_based: bool | None = None
    tax_percent: float | None = None
    net_amount_taxable: float | None = None


class TaxLineBlock(Strict):
    amount: float | None = None
    detail_type: str | None = None
    tax_line_detail: TaxLineDetailBlock | None = None


class TransactionTaxDetailBlock(Strict):
    """Whether tax was applied, under which code and at which rate."""

    txn_tax_code: VendorRef | None = None
    total_tax: float | None = None
    tax_lines: list[TaxLineBlock] | None = None


class CheckPaymentBlock(Strict):
    bank_account: VendorRef | None = None


class CreditCardPaymentBlock(Strict):
    cc_account: VendorRef | None = None


class TransactionBlock(Strict):
    id: str | None = None
    doc_number: str | None = None
    txn_date: str | None = None
    due_date: str | None = None
    total_amount: float | None = None
    amount: float | None = Field(
        default=None,
        description="A Transfer's amount. Transfers carry no total_amount.",
    )
    home_total_amount: float | None = None
    balance: float | None = None
    exchange_rate: float | None = None
    private_note: str | None = None
    payment_type: str | None = None
    pay_type: str | None = None
    payment_method: VendorRef | None = None
    # Intuit's Purchase page: valid only for the CreditCard payment type.
    credit: bool | None = Field(
        default=None,
        description="Documented for CreditCard refunds only. Not a general "
        "negative-amount flag.",
    )
    total_tax: float | None = None
    txn_status: str | None = None
    po_status: str | None = None
    email_status: str | None = None
    print_status: str | None = None
    adjustment: bool | None = None
    vendor: VendorRef | None = None
    customer: VendorRef | None = None
    entity_ref: EntityRefBlock | None = None
    account: VendorRef | None = None
    from_account: VendorRef | None = Field(
        default=None, description="Transfer: the account money left."
    )
    to_account: VendorRef | None = Field(
        default=None, description="Transfer: the account money arrived in."
    )
    check_payment: CheckPaymentBlock | None = Field(
        default=None, description="BillPayment by check: the bank account that paid."
    )
    credit_card_payment: CreditCardPaymentBlock | None = Field(
        default=None, description="BillPayment by card: the card account that paid."
    )
    ap_account: VendorRef | None = None
    deposit_to_account: VendorRef | None = None
    currency: CurrencyRef | None = None
    department: VendorRef | None = None
    class_ref: VendorRef | None = None
    terms: VendorRef | None = None
    project: VendorRef | None = None
    recurring_template: VendorRef | None = None
    tax_detail: TransactionTaxDetailBlock | None = None
    linked_transactions: list[LinkedTransaction] | None = None
    lines: list[TransactionLine] | None = None
    metadata: CompanyMetadata | None = None


class TransactionEnvelope(IntuitBacked):
    transaction_type: str
    transaction: TransactionBlock


# -------------------------------------------------- list_transactions ---


class ReportCellBlock(Strict):
    column_key: str
    text: str | None = Field(
        description="The verbatim upstream value. Identifiers, document numbers "
        "and free text are never numerically inferred, so '00123' stays '00123'."
    )
    amount: str | None = Field(
        default=None,
        description="Exact decimal string: a money column's cell (with currency), a "
        "quantity column's cell (no currency; a column declared Quantity, or keyed "
        "Quantity or quantity whatever its declared type), or a percent column's cell "
        "(the figure before the sign, unit 'percent', no currency).",
    )
    currency: str | None = None
    unit: Literal["percent"] | None = Field(
        default=None,
        description="'percent' on a cell of a percent column: a Money-typed column whose "
        "key ends in Perc or PercentSales or whose title carries '%' (never an entity "
        "grouping's column by its title, which is the entity's name), with cell text "
        "such as '30.69 %'. amount is then 30.69 and there is no currency. Absent on "
        "every other cell.",
    )
    entity_id: str | None = Field(
        default=None,
        description="Upstream id for this cell where one was supplied. With the "
        "transaction-type cell, this is how a row routes to get_transaction; on a "
        "hierarchical report's label cell it is the account, customer, vendor or "
        "item id.",
    )
    label: str | None = Field(
        default=None,
        description="The column title, carried onto the cell where the key alone "
        "says nothing: an aging bucket keyed '1' is titled '31 - 60'.",
    )
    period: str | None = Field(
        default=None,
        description="'start/end' (YYYY-MM-DD/YYYY-MM-DD) for a cell under a period "
        "column such as 'Jan 2026', so the cell says which dates it covers.",
    )


class ReportColumnBlock(Strict):
    index: int = Field(description="Leaf position; the position of the cell that belongs to it.")
    key: str = Field(
        description="The column's identity in this result: its ColKey, 'parent/leaf' "
        "for a nested column, a synthetic key for an admitted keyless label column "
        "(account, customer, vendor, item, class, department, label)."
    )
    title: str
    type: str = Field(
        description="ColType as declared; '' when QuickBooks declared none. A column "
        "keyed Quantity is a quantity even where this says Money (InventoryValuationSummary "
        "and SalesByProduct declare it so): its cells carry no currency."
    )
    leaf_key: str | None = Field(
        default=None, description="The ColKey as declared, or null when the column had none."
    )
    parent_title: str | None = Field(
        default=None, description="For a nested column, the title of the column that groups it."
    )
    period_start: str | None = Field(default=None, description="StartDate metadata, period columns.")
    period_end: str | None = Field(default=None, description="EndDate metadata, period columns.")
    synthetic_key: bool = Field(
        default=False,
        description="True when the key was given by this server to a keyless label column, "
        "admitted by position and type rather than by key.",
    )
    entity_type: str | None = Field(
        default=None,
        description="Under an entity grouping of summarize_column_by (Customers, Vendors, "
        "Classes, Departments, Employees, ProductsAndServices): the type this column "
        "names -- Customer, Vendor, Class, Department, Employee or Item -- which is "
        "where entity_id routes (list_customers, list_vendors, list_reference_data; "
        "Employee is served nowhere).",
    )
    entity_id: str | None = Field(
        default=None,
        description="Under an entity grouping: the entity's QuickBooks id, which is the "
        "column's ColKey; null for the not_specified column.",
    )
    entity_total: bool = Field(
        default=False,
        description="True when this column is an entity's total over itself and its "
        "children: its key repeated after theirs, published as '<id> (total)'.",
    )
    admitted_by: str | None = Field(
        default=None,
        description="How the column came to be published: 'key' (its ColKey is on the "
        "report's allowlist), 'period' (StartDate/EndDate metadata and a monetary type), 'grouping' "
        "(an entity id under a named grouping), 'label_position' (the keyless first "
        "column of a hierarchical report) or 'title' (a keyless column of a monetary type "
        "matched by its ColTitle on a report that declares no keys at all, such as TrialBalance).",
    )


class ReportRowBlock(Strict):
    row_id: str
    parent_row_id: str | None = None
    row_type: str
    depth: int
    group: str | None = None
    cells: list[ReportCellBlock]
    scope: str | None = None
    covers: str | None = Field(
        default=None,
        description="Summary rows only: what the total covers. 'upstream_section' "
        "is the total QuickBooks computed over the whole section. 'unknown' is kept "
        "for a response that signals a cap; no report this server calls publishes "
        "one, so every total reads 'upstream_section'.",
    )


class ReportAnomalyBlock(Strict):
    """One problem in a report, listed at the top level of the result."""

    row_id: str | None = Field(
        description="The affected row, or null when the whole column is affected."
    )
    column_key: str | None = Field(
        description="The affected column, or null when the whole row is affected."
    )
    kind: Literal[
        "malformed_amount",
        "malformed_cell",
        "short_row",
        "long_row",
        "unrecognised_column_type",
        "stated_total_not_reproducible",
        "unrecognised_row",
    ]
    detail: str = Field(description="Fixed text. Never carries a cell's value.")


class ZeroRowRecheck(Strict):
    """Why a filtered report came back empty.

    Zero rows is an ordinary answer, so an empty report cannot on its own
    distinguish "this window holds nothing" from "this filter matched nothing".
    Re-reading the window unfiltered separates the two.

    The ambiguity is confined to the **id and free-text** filters -- ``vendor``,
    ``customer``, ``docnum``, ``memo`` -- where a value that does not exist
    returns an empty report. The enumerated filters refuse a bad value with an
    error and so cannot produce this case.
    """

    performed: bool
    unfiltered_row_count: int | None = Field(
        default=None,
        description="Rows the same window returned with no filters applied.",
    )
    interpretation: str = Field(
        description="Which of the two cases this is, naming the filters that "
        "could have matched nothing silently."
    )


class TransactionListEnvelope(IntuitBacked):
    summary: str = Field(
        description="One sentence, placed first in the text. Begins 'PARTIAL "
        "RESULT' whenever any anomaly was found."
    )
    report_name: str
    start_period: str | None = None
    end_period: str | None = None
    currency: str | None = None
    columns: list[ReportColumnBlock]
    excluded_column_keys: list[str] = Field(
        description="Columns that existed upstream and were withheld. A column "
        "being named here is a different statement from it not existing."
    )
    rows: list[ReportRowBlock]
    count: int
    upstream_row_count: int
    upstream_cell_count: int = Field(
        description="Cells counted before any client trimming or allowlisting."
    )
    upstream_partial: bool
    client_truncated: bool
    completeness: Literal["complete", "partial", "unknown"] = Field(
        description="Never 'complete' while any anomaly exists."
    )
    anomalies: list[ReportAnomalyBlock] = Field(
        default_factory=list,
        description="Every cell-, row- and column-level problem found, over every "
        "upstream row including any trimmed by max_rows. An affected cell keeps its "
        "original text and carries no amount; a total QuickBooks states is kept as "
        "stated and flagged when the returned cells cannot reproduce it.",
    )
    filters_sent: list[str] = Field(
        default_factory=list,
        description="Filter parameters this server sent with the report request. "
        "What was sent, not what was applied: the report echoes no parameters, "
        "so nothing in a response confirms a filter acted on it. Validation is "
        "not uniform: the enumerated filters refuse a bad value with an error, "
        "while vendor, customer, docnum and memo can match nothing silently -- "
        "see zero_row_recheck.",
    )
    filterable_transaction_types: list[str] = Field(
        default_factory=list,
        description="The transaction_type values proven to work as filters. The "
        "API may accept others, but they are untested and cannot be sent. "
        "These are a THIRD "
        "vocabulary: not the display labels in the txn_type column, and not the "
        "entity names used by run_query and get_transaction. 'JournalEntry' is "
        "accepted; 'Journal Entry' is not. 'Purchase' and 'BillPayment' are not. "
        "Do not pass a txn_type cell value back as a filter.",
    )
    filterable_source_account_types: list[str] = Field(
        default_factory=list,
        description="The values source_account_type accepts: the AccountType "
        "enum WITH THE SPACES REMOVED. 'AccountsPayable' filters; 'Accounts "
        "Payable' is refused. list_accounts reports the SPACED form in its "
        "account_type field, so the value you read off an account is not the "
        "value that filters on it.",
    )
    zero_row_recheck: ZeroRowRecheck | None = Field(
        default=None,
        description="Present only when a filtered request returned no rows. The "
        "same window is re-read unfiltered before anything reports 'none', "
        "because an id or free-text filter that matched nothing produces an "
        "empty report rather than an error.",
    )
    note: str | None = None


# ------------------------------------------------------- report tools ---


class ReportEnvelope(IntuitBacked):
    """Every named report tool's result (report_profit_and_loss,
    report_balance_sheet, report_general_ledger, report_aging).

    The flattened report, plus what this server
    sent and what it checked before sending, so a reader can tell a filter
    that was applied from one that was refused, and a default accounting
    method from an explicit one.
    """

    summary: str = Field(
        description="One sentence, placed first in the text. Begins 'PARTIAL "
        "RESULT' whenever any anomaly was found."
    )
    tool: str
    report: str | None = Field(
        default=None,
        description="run_report only: the tool-facing report name that was asked for "
        "(the title of Intuit's page for it); report_name is the path that answered.",
    )
    report_name: str = Field(description="The QuickBooks report endpoint that answered.")
    side: Literal["payables", "receivables"] | None = None
    detail: Literal["summary", "detail"] | None = None
    start_period: str | None = None
    end_period: str | None = None
    currency: str | None = None
    report_basis: str | None = Field(
        default=None, description="Header.ReportBasis: the accounting method QuickBooks used."
    )
    summarize_columns_by: str | None = None
    date_macro: str | None = Field(
        default=None, description="Header.DateMacro as QuickBooks echoed it (lower-cased by it)."
    )
    date_macro_sent: str | None = Field(
        default=None, description="The date_macro this server sent, as given."
    )
    resolved_start: str | None = Field(
        default=None,
        description="Where this server resolved the macro to start, for its own checks "
        "(the six-month and 200-day rules). An estimate: start_period is what "
        "QuickBooks reported.",
    )
    resolved_end: str | None = Field(default=None, description="As resolved_start, for the end.")
    resolution_basis: str | None = Field(
        default=None,
        description="How the macro was resolved: the operator's local date and the "
        "company's fiscal year start, labelled as such.",
    )
    columns: list[ReportColumnBlock]
    excluded_column_keys: list[str] = Field(
        description="Columns that existed upstream and were withheld. A column "
        "being named here is a different statement from it not existing."
    )
    rows: list[ReportRowBlock]
    count: int
    upstream_row_count: int
    upstream_cell_count: int = Field(
        description="Cells counted before any client trimming or allowlisting."
    )
    upstream_partial: bool
    upstream_no_report_data: bool | None = Field(
        default=None,
        description="QuickBooks's own NoReportData option, reported as stated. Never "
        "inferred from an empty rows array.",
    )
    client_truncated: bool
    completeness: Literal["complete", "partial", "unknown"] = Field(
        description="Never 'complete' while any anomaly exists. 'unknown' when nothing "
        "in the response establishes it: these reports publish no cap signal."
    )
    anomalies: list[ReportAnomalyBlock] = Field(
        default_factory=list,
        description="Every cell-, row- and column-level problem found, over every "
        "upstream row including any trimmed by max_rows.",
    )
    disclosures: list[str] = Field(
        default_factory=list,
        description="Facts a reader should know that are not anomalies and do not make "
        "the result partial, each said once.",
    )
    parameters_sent: list[str] = Field(
        default_factory=list,
        description="Parameter NAMES this server sent with the request. What was sent, "
        "not what was applied: the report echoes only some parameters.",
    )
    accounting_method_sent: str | None = None
    accounting_method_source: Literal["caller", "company_report_basis"] | None = Field(
        default=None,
        description="'company_report_basis' when the caller gave none and the company's "
        "Preferences.ReportPrefs.ReportBasis was sent.",
    )
    capabilities_checked: list[str] = Field(
        default_factory=list,
        description="Company capabilities a filter required and this call verified "
        "before sending (class_tracking, department_tracking).",
    )
    accounts_payable: Literal["available", "unavailable", "unknown"] | None = Field(
        default=None,
        description="report_aging(payables) only: what this process has established "
        "about the company's accounts payable.",
    )
    note: str | None = None


# ------------------------------------------------- vendor payment scan ---


class CandidateVendorRow(Strict):
    vendor_id: str
    display_name: str | None = None
    active: bool | None = None
    candidate_non_card_amount: str
    candidate_card_amount: str
    payment_count: int
    sources: list[str]
    card_detail_withheld: bool | None = None


class CandidatePaymentRow(Strict):
    source: str
    transaction_id: str
    txn_date: str | None = None
    vendor_id: str | None = None
    vendor_name: str | None = None
    payment_type: str | None = None
    is_card: bool
    amount: str
    doc_number: str | None = None
    currency: str | None = None


class JournalReviewLine(Strict):
    transaction_id: str
    line_id: str | None = None
    txn_date: str | None = None
    vendor_id: str | None = None
    vendor_name: str | None = None
    direction: str
    signed_amount: str


class UnclassifiedRow(Strict):
    source: str
    transaction_id: str
    reason: str


class NeedsReview(Strict):
    no_payee: list[str] = Field(
        description="Transaction ids with no payee reference. Not attributed to "
        "any vendor and not in any total."
    )
    # Intuit's JournalEntry page: TotalAmt and HomeTotalAmt are always zero.
    journal_entry_vendor_lines: list[JournalReviewLine] = Field(
        description="Journal-entry lines touching a vendor. For review only and "
        "never summed: a JournalEntry header total is documented as zero, so "
        "these amounts come from signed lines and what they mean is a judgement."
    )


class SourceWalked(Strict):
    source: str
    walked: bool
    reason: str | None = None


class VendorPaymentsEnvelope(IntuitBacked):
    year: int
    date_basis: Literal["TxnDate"] = "TxnDate"
    currency_basis: str | None = None
    sources: list[SourceWalked]
    accounts_payable: Literal["available", "unavailable", "unknown"] = Field(
        description="As established by this entity's BillPayment reads so far. "
        "'unavailable' means the BillPayment source was not "
        "walked and sources says why."
    )
    excluded_payment_types: list[str]
    # Per-vendor candidate totals only, with no company-wide roll-up: candidate
    # totals and review amounts are different figures.
    vendors: list[CandidateVendorRow]
    needs_review: NeedsReview
    unclassified: list[UnclassifiedRow]
    excluded_summary: dict[str, int] = Field(
        description="Records deliberately not counted, by reason -- a payee that "
        "is a customer or an employee, for instance."
    )
    rows: list[CandidatePaymentRow] | None = Field(
        default=None, description="Present only when detail='rows'."
    )
    deduplication_note: str
    interpretation_note: str
    walk_started_at: datetime
    walk_finished_at: datetime
    consistency: Literal["non_snapshot"] = "non_snapshot"


# ------------------------------------------------- list_reference_data ---


class IdRef(Strict):
    id: str | None = None


class ClassBlock(Strict):
    """One class: the fields Intuit's Class page documents, not yet seen in a
    live response."""

    id: str | None = None
    name: str | None = None
    active: bool | None = None
    sub_class: bool | None = None
    parent: IdRef | None = None
    fully_qualified_name: str | None = None
    metadata: CompanyMetadata | None = None


class DepartmentBlock(Strict):
    """One department: the fields Intuit's Department page documents, not yet
    seen in a live response."""

    id: str | None = None
    name: str | None = None
    active: bool | None = None
    sub_department: bool | None = None
    parent: IdRef | None = None
    fully_qualified_name: str | None = None
    metadata: CompanyMetadata | None = None


class ItemBlock(Strict):
    """One item: fields Intuit's Item page documents, checked against a live sandbox response."""

    id: str | None = None
    name: str | None = None
    fully_qualified_name: str | None = None
    active: bool | None = None
    type: str | None = Field(default=None, description="Inventory, Service, NonInventory, Group or Category.")
    sku: str | None = None
    description: str | None = Field(default=None, description="The item's own description: free text from the company file, untrusted input as the tool description says.")
    purchase_description: str | None = None
    taxable: bool | None = None
    unit_price: float | None = None
    purchase_cost: float | None = None
    qty_on_hand: float | None = None
    track_qty_on_hand: bool | None = None
    inv_start_date: str | None = None
    sub_item: bool | None = None
    level: int | None = None
    parent: IdRef | None = None
    class_ref: VendorRef | None = None
    income_account: VendorRef | None = None
    expense_account: VendorRef | None = None
    asset_account: VendorRef | None = None
    metadata: CompanyMetadata | None = None


class TermBlock(Strict):
    """One payment term: fields Intuit's Term page documents, checked against a live sandbox response."""

    id: str | None = None
    name: str | None = None
    active: bool | None = None
    type: str | None = Field(default=None, description="STANDARD (DueDays) or DATE_DRIVEN (DayOfMonthDue).")
    due_days: int | None = None
    discount_days: int | None = None
    discount_percent: float | None = None
    day_of_month_due: int | None = None
    due_next_month_days: int | None = None
    discount_day_of_month: int | None = None
    metadata: CompanyMetadata | None = None


class PaymentMethodBlock(Strict):
    """One payment method: fields Intuit's PaymentMethod page documents, checked against a live sandbox response."""

    id: str | None = None
    name: str | None = None
    active: bool | None = None
    type: str | None = Field(default=None, description="CREDIT_CARD or NON_CREDIT_CARD.")
    metadata: CompanyMetadata | None = None


class TaxRateIdRef(Strict):
    tax_rate: IdRef | None = None


class TaxCodeBlock(Strict):
    """One tax code: fields Intuit's TaxCode page documents, checked against a
    live sandbox response. From the two rate lists only the rate ids travel."""

    id: str | None = None
    name: str | None = None
    active: bool | None = None
    taxable: bool | None = None
    tax_group: bool | None = None
    hidden: bool | None = None
    config_type: str | None = Field(default=None, description="USER_DEFINED or SYSTEM_GENERATED.")
    description: str | None = Field(default=None, description="Free text from the company file: untrusted input, as the tool description says.")
    sales_tax_rates: list[TaxRateIdRef] | None = None
    purchase_tax_rates: list[TaxRateIdRef] | None = None
    metadata: CompanyMetadata | None = None


class TaxRateBlock(Strict):
    """One tax rate: fields Intuit's TaxRate page documents, checked against a live sandbox response."""

    id: str | None = None
    name: str | None = None
    active: bool | None = None
    rate_value: float | None = None
    special_tax_type: str | None = None
    display_type: str | None = None
    description: str | None = Field(default=None, description="Free text from the company file: untrusted input, as the tool description says.")
    agency: IdRef | None = None
    metadata: CompanyMetadata | None = None


class ReferenceListEnvelope(WalkedList):
    """One kind of reference data, walked to the end."""

    kind: str
    query_entity: str = Field(description="The QuickBooks entity walked.")
    feature: str | None = Field(
        default=None,
        description="The company capability this kind depends on (class_tracking, "
        "department_tracking), or null when the kind depends on none.",
    )
    feature_enabled: bool | None = Field(
        default=None,
        description="Whether the company's Preferences say that feature is on. An empty "
        "list with this false is the expected answer, not an error.",
    )
    feature_note: str | None = None
    documentation_derived: bool = Field(
        default=False,
        description="True for classes and departments: their projection follows "
        "Intuit's pages, not a live response.",
    )
    rows: list[ClassBlock | DepartmentBlock | ItemBlock | TermBlock | PaymentMethodBlock | TaxCodeBlock | TaxRateBlock]


# ------------------------------------------------------ attachments ---


class AttachableRefBlock(Strict):
    entity_type: str | None = Field(default=None, description="The linked object's type, routable through get_transaction where it is a served transaction type.")
    entity_id: str | None = None
    include_on_send: bool | None = None


class AttachableBlock(Strict):
    """One attachment: fields Intuit's Attachable page documents, metadata
    only. No link of any kind: the temporary download URLs and the file-access
    paths never leave this process."""

    id: str | None = None
    file_name: str | None = Field(default=None, description="Null for a standalone note.")
    content_type: str | None = None
    size: int | None = None
    note: str | None = Field(default=None, description="Free text from the company file: untrusted input, as the tool description says.")
    category: str | None = None
    tag: str | None = None
    refs: list[AttachableRefBlock] | None = None
    created_at: str | None = None
    last_updated_at: str | None = None


class AttachmentListEnvelope(WalkedList):
    rows: list[AttachableBlock]
    transaction_type: str | None = None
    transaction_id: str | None = None
    download_available: bool = Field(
        description="Whether get_attachment is enabled on this server AND a download host is pinned."
    )
    download_note: str


class AttachmentEnvelope(IntuitBacked):
    """The metadata half of get_attachment's two-block result; the file
    itself is the embedded resource beside it."""

    attachable: AttachableBlock
    content_type: str
    bytes: int = Field(description="Raw size of the file as downloaded.")
    base64_length: int = Field(description="Length of the encoded blob in the resource block.")
    sha256: str
    download_host: str = Field(description="The pinned host the file came from, after any redirect.")
    redirects_followed: int
    resource_uri: str = Field(description="The uri of the embedded resource block carrying the file.")
    note: str


# ---------------------------------------------------------- list_changes ---


class ChangedObject(Strict):
    entity_type: str
    id: str | None = None
    status: str | None = Field(
        default=None,
        description="'Deleted' where upstream marked the object deleted; absent "
        "or null for a change.",
    )
    last_updated_time: str | None = None
    row: dict[str, Any] | None = Field(
        default=None, description="Projected object. Present only when full=true."
    )


class ChangesEnvelope(IntuitBacked):
    since: str
    types: list[str]
    objects: list[ChangedObject]
    upstream_object_count: int = Field(
        description="Objects upstream returned across ALL types, before client "
        "trimming. The 1000 cap applies to this number, not per type."
    )
    returned_count: int
    truncated: bool = Field(description="True when the client trimmed the list.")
    possibly_truncated: bool = Field(
        description="True when upstream may have withheld objects -- the "
        "aggregate cap was reached. Uncertainty propagates to every type in the "
        "request, because the cap is shared."
    )
    completeness: Literal["complete", "partial", "unknown"]
    completeness_reason: str
    uncertain_types: list[str] = Field(
        description="Types whose completeness cannot be established for this "
        "response."
    )
    checkpoint_safe: bool = Field(
        description="False whenever completeness is not 'complete'. A possibly "
        "incomplete response is NOT a safe synchronization checkpoint."
    )
    note: str
