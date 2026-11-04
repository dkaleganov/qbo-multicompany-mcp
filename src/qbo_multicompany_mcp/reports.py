"""Flattening QuickBooks report JSON into typed rows.

``list_transactions`` and every report tool share this core, so it is built to
be general.

Several things here are easy to get wrong and expensive to get wrong.

**Cells are positional, not self-describing.** A row's ``ColData`` array is
paired with the report's declared column order. Some cells carry their own
metadata and some do not, so code that keyed off whatever each cell happened to
contain would silently mis-align every row in a report whose cells are sparse.
The column list is the schema; the cells are values in that order.

**Report columns are inside the privacy boundary.** Projecting the *entities*
protects nothing if the same address, email, telephone or account number
arrives as a report cell. The allowlist here is keyed on **column identifiers**,
not on display headings -- headings are localised and user-editable, so a
denylist of English titles protects nobody. Unknown column keys are excluded
and named, because a column this package has never reviewed is exactly the one
that might carry something. Two kinds of column can't be reviewed by key,
because the key changes with the request: a period column and an entity
grouping column. Those pass on their shape instead, each says how it got in
(``admitted_by``), and both rest on Intuit's column metadata being accurate. A
column with no key at all passes only by its position or by a title the report's
shape lists.

**Nothing is numerically inferred.** Every cell keeps its verbatim upstream
text. A document number of ``00123`` stays ``"00123"``; a reference that looks
like a float stays text. Monetary columns *additionally* carry an exact decimal
string and a currency, so a caller that wants arithmetic gets it without anyone
guessing which columns were numbers.

**Report parameter validation is not uniform, and one parameter's behaviour is
no evidence for another's.** The table below is the current instance. Every
parameter needs its own probe, with its own control.

Established by differential probing with controls, on sandbox:

=============================  ====================  =========================
What is wrong                  What the endpoint     What the caller sees
                               does
=============================  ====================  =========================
Unknown parameter **name**     discards it           **everything**
Enumerated value, unaccepted   **HTTP 400**          a loud, specific error
Id or free-text value,         200, zero rows        **nothing**
  no match
=============================  ====================  =========================

The enumerated filters -- ``transaction_type``, ``source_account_type``,
``cleared`` -- refuse an unaccepted value outright with
``Invalid Enumeration : <value>``, naming what they refused. Nothing is silent
there.

The silent failure is real but belongs to the **id and free-text** filters:
``vendor``, ``customer``, ``docnum``, ``memo``. An id's *shape* is validated
and its *existence* is not -- ``vendor=not-an-id`` is a 400 Invalid Reference
Id, while a well-formed id for a vendor that does not exist returns 200 with
zero rows. ``docnum`` and ``memo`` are free text with nothing to validate at
all. For those, an empty report is indistinguishable from a value that matched
nothing, which is why a filtered request returning zero rows is re-checked
against the same window unfiltered.

Because an unaccepted enumerated value produces a 400, **a 200 proves
membership** whether or not the window holds matching rows. That makes each
CANDIDATE exactly testable. It does not make a vocabulary exhaustive: a value
nobody tried could still be accepted. So each pinned list is the set proven to
work in sandbox, and may be narrower than what the API allows -- a pin can refuse
a value that would have worked. Each is pinned in the published input schema so
a bad value is an impossible input rather than a runtime API failure.

**``transaction_type`` is a third vocabulary.** It is neither the display labels
that appear in the ``txn_type`` column nor the entity names used by ``query``
and ``get_transaction``. ``JournalEntry`` is accepted while ``Journal Entry``
is not; ``Purchase``, ``BillPayment``, ``Transfer`` and ``VendorCredit`` -- the
entity names a caller is most likely to reach for -- are all refused. See
:data:`REPORT_TRANSACTION_TYPES`.

**``source_account_type`` is the same trap one layer down.** It is the
documented AccountType enum *with the spaces removed*: ``AccountsPayable``
filters, ``Accounts Payable`` is a 400 -- and the chart of accounts, including
this server's own ``list_accounts``, reports the **spaced** form. So again, the
value a caller reads is not the value that filters on it. See
:data:`REPORT_SOURCE_ACCOUNT_TYPES`.

**Emptiness is never evidence.** An empty ``Rows`` array means the response
carried no rows -- not that the company has no transactions. This server reads
no cap signal from a report response, and being far from a cap proves nothing
either. Where completeness cannot be established it is reported as
``unknown`` rather than assumed.

**Nothing becomes a silent zero or null.** Two kinds of problem, handled in
opposite ways:

*Confined to a cell or a row* -- a malformed amount, a row with too few or too
many cells, an allowlisted column of a type this package does not recognise.
The result is PARTIAL: a malformed cell keeps its original text and carries no
amount, a row with the wrong number of cells has every value withheld (with
the count wrong, no cell's column is known), every anomaly is listed in
:attr:`FlattenedReport.anomalies` (row, column key, kind), completeness is
never ``complete``, a total QuickBooks states is kept as stated but flagged as
not reproducible from the returned cells, and a total this server computes
over an affected cell is ``unknown`` (:func:`computed_total`).

*Breaking the mapping between cells and columns* -- a malformed column
definition, an unsupported shape. The request FAILS with :class:`ReportError`,
because then no cell can be trusted. Skip a non-object column definition, for
one, and the surviving columns pair against a compacted list, so a private
value comes out under a money key.

**Five shapes the documentation samples never showed (observed live,
2026-09-24).** Each is the documented shape of a real report, not an anomaly,
and the parser makes each ordinary BEFORE the anomaly rules run:

* *Nested columns.* ItemSales declares two top-level columns, the second of
  which groups seven sub-columns under ``Columns.Column``; rows carry one cell
  per LEAF. Columns are flattened to leaves, depth-first, each carrying its
  parent's title and any period metadata, and a leaf's identity is its path
  (``total/Amount``). Cells are paired against leaves.
* *Period columns with dynamic keys.* Summarised by Month or Quarter, a report
  keys its Money columns ``Jan 2026`` or ``Jan - Mar, 2026``: no fixed
  allowlist can admit them. A period column is identified by its
  ``StartDate``/``EndDate`` metadata and monetary type (Money, Amount or Rate), on the reports whose shape
  declares periods, and the period is surfaced on the cell.
* *Keyless label columns.* Fourteen reports declare a first column with no
  ``ColKey`` (the aging summaries, CashFlow, the sales summaries, ...) and
  TrialBalance has none on any column. On hierarchical and summary reports the
  first column is admitted by position and type -- Account, Customer, Vendor,
  ProductsAndService, Class, Department or String -- under a synthetic key
  (:data:`LABEL_COLUMN_KEYS`). Detail-style reports keep the TransactionList
  rule: a column without a key cannot be allowlisted.
* *An absent ``ColType`` on an empty report.* InventoryValuationDetail over a
  window with no rows declared nine columns with no type at all; with rows it
  declared them all. An absent type is an anomaly only when there are rows to
  type; on an empty report it is disclosed once and the result is not partial.
* *``Quantity``.* A numeric, non-monetary type: exact decimal, no currency.
  A column KEYED ``Quantity`` is one whatever its declared type, because
  InventoryValuationSummary and ItemSales declare theirs Money
  (:data:`QUANTITY_COLUMN_KEYS`).

**A sixth shape, observed 2026-09-25: the entity groupings of
``summarize_column_by``.** Summarised by Customers, Vendors, Classes,
Departments, Employees or ProductsAndServices, a report returns one Money
column PER ENTITY keyed by the entity's ID with its name as ColTitle, plus
``not_specified`` and ``total``, and no period metadata on any of them. A
grouping column is admitted by that shape -- Money, an integer-like key or
``not_specified`` -- only under a request that named the grouping, and the
id goes on the column as ``entity_id`` (:data:`ENTITY_GROUPINGS`).

**A seventh shape, observed 2026-09-25 on both sandbox companies: percent
columns declared Money.** ItemSales declares ``PercentSales`` ("% of Sales")
and ``GrossMarginPerc`` ("Gross Margin %") with ColType ``Money``, and their
cells carry ``'30.69 %'`` -- a decimal, a space and the sign. Read as money,
every cell is a ``malformed_amount`` and every SalesByProduct result is
PARTIAL, which teaches a reader to ignore PARTIAL. So a PERCENT COLUMN -- a
Money-typed column whose key ends in ``Perc`` or ``PercentSales``, or whose
title carries ``%`` (:func:`ReportColumn.is_percent`; never an entity
grouping's column by its title, which is the entity's name) -- has its cells
read as an exact decimal followed by the sign, published with
``unit: percent``, no currency and no anomaly; any other text in such a column
is a ``malformed_amount``, a bare number included, because a number without
the sign in a percent column is not known to be a percent. A ``%`` in a Money
column that is not a percent column stays an anomaly. A percent column is
never totalled by :func:`computed_total`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from .errors import QboMcpError
from .projections import is_scalar

#: Column types QuickBooks marks as monetary. These get an exact decimal
#: representation alongside their text, with a currency -- unless the column
#: is a quantity by its key (:data:`QUANTITY_COLUMN_KEYS`).
MONEY_COLUMN_TYPES: Final[frozenset[str]] = frozenset({"Money", "Amount", "Rate"})

#: Numeric but not monetary: an exact decimal with NO currency. In no
#: documentation sample. OBSERVED live on 2026-09-24: InventoryValuationDetail
#: over a window with rows declared its ``quantity`` column with ColType
#: ``Quantity``. The type is one way a column is a quantity; its key is the
#: other (next).
QUANTITY_COLUMN_TYPES: Final[frozenset[str]] = frozenset({"Quantity"})

#: Column KEYS that are a quantity whatever their declared type (key-based,
#: like the percent rule). Intuit declares ColType ``Money`` for
#: InventoryValuationSummary's 'Qty' and ItemSales' 'Quantity', both ColKey
#: ``Quantity``: observed in sandbox on 2026-09-24, and the samples on Intuit's
#: pages agree. A rule by type publishes ten units as ten dollars.
#: ``quantity`` is the same figure's key where InventoryValuationDetail
#: declares ColType Quantity and where JournalReport documents a column no
#: live response has shown. No other key observed in sandbox names a quantity
#: and is typed Money (checked 2026-09-26; ``qty_on_hand`` is declared String).
QUANTITY_COLUMN_KEYS: Final[frozenset[str]] = frozenset({"Quantity", "quantity"})

#: A percent column is a Money-typed column whose declared key ends in one
#: of these, or whose title carries :data:`PERCENT_TITLE_MARK`. OBSERVED
#: 2026-09-25 on both sandbox companies: ItemSales's ``PercentSales`` ("% of
#: Sales") and ``GrossMarginPerc`` ("Gross Margin %"), both declared Money,
#: cells ``'30.69 %'``. The key rule is what identifies them where a title is
#: localised or edited; the title rule catches a percent column under a key
#: this package has not seen.
PERCENT_KEY_SUFFIXES: Final[tuple[str, ...]] = ("Perc", "PercentSales")
PERCENT_TITLE_MARK: Final = "%"

#: The ``unit`` a percent cell carries. Money cells carry a ``currency``
#: instead; quantity cells carry neither.
UNIT_PERCENT: Final = "percent"

#: Column types this package recognises. The capitalised values are the ones
#: the report samples on Intuit's pages carry in the ``ColType``-plus-``ColKey``
#: shape (BalanceSheet, ProfitAndLoss, TransactionListWithSplits and the report
#: pages beside them), plus the monetary and quantity sets above. A PUBLISHED
#: column of any other type is returned as text only and listed as an anomaly:
#: this package cannot say what its values mean.
#:
#: ``TimeStamp`` is in no documentation sample. It was OBSERVED live: sandbox
#: TransactionList responses on 2026-09-23 declared ``create_date`` and
#: ``last_mod_date`` with it, for both sandbox companies. Unrecognised, it
#: marks every live list_transactions result partial -- and a tool whose every
#: result says PARTIAL teaches its reader to ignore the word. Its values are
#: returned as text, like ``Date``: timestamps pass through unconverted.
RECOGNISED_COLUMN_TYPES: Final[frozenset[str]] = (
    MONEY_COLUMN_TYPES
    | QUANTITY_COLUMN_TYPES
    | frozenset(
        {
            "String", "Date", "TimeStamp", "Boolean", "Account", "Customer", "Vendor",
            "Class", "Department", "ProductsAndService", "Employee",
        }
    )
)

#: The types a KEYLESS first column may carry and still be admitted, on a
#: hierarchical or summary report, and the synthetic key each is published
#: under. The label column carries the row's identity -- the account, the
#: customer, the vendor, the item -- and fourteen reports declare it with no
#: ColKey (observed 2026-09-24). It is admitted by POSITION (leaf index 0) and
#: TYPE, never by key, and only on a report whose shape is hierarchical; a
#: detail-style report keeps the rule that a column without a key cannot be
#: allowlisted.
LABEL_COLUMN_KEYS: Final[dict[str, str]] = {
    "Account": "account",
    "Customer": "customer",
    "Vendor": "vendor",
    "ProductsAndService": "item",
    "Class": "class",
    "Department": "department",
    "String": "label",
}
LABEL_COLUMN_TYPES: Final[frozenset[str]] = frozenset(LABEL_COLUMN_KEYS)

#: The entity groupings of ``summarize_column_by`` -> the entity type each of
#: their columns names, which is also where the id routes: Customer to
#: ``list_customers``, Vendor to ``list_vendors``, Class, Department and Item
#: to ``list_reference_data``; Employee is served nowhere and is named so a
#: reader knows the id has no route here.
#:
#: OBSERVED 2026-09-25 on ProfitAndLoss, on the sandbox company: Customers
#: gave eight columns, Money columns keyed ``2``, ``7``, ``8``, ``7`` again,
#: ``20``, then ``not_specified`` and ``total``; Vendors keyed ``49``, ``50``;
#: ProductsAndServices keyed ``6``, ``10``, ``5``, ``13``, ``14``, ``17``.
#: Classes, Departments and Employees gave only ``not_specified`` and
#: ``total`` because those features are off on that company. No
#: StartDate/EndDate metadata on any grouping column, which is why the period
#: rule cannot admit them.
#:
#: The repeated key is the parent's total: the Customers response declares
#: ``7`` (the parent's own column), then ``8`` (its sub-customer), then ``7``
#: again titled "Total ..." whose cells sum the two. The ORDER is what the
#: rule reads -- the second occurrence of an entity key under a grouping is
#: that entity's total column, published as ``<id> (total)`` with
#: ``entity_total`` set; the title is user language and is not consulted.
#: Anywhere else a repeated key still fails the request: it breaks the mapping
#: between cells and columns.
ENTITY_GROUPINGS: Final[dict[str, str]] = {
    "Customers": "Customer",
    "Vendors": "Vendor",
    "Classes": "Class",
    "Departments": "Department",
    "Employees": "Employee",
    "ProductsAndServices": "Item",
}
#: The column a grouping gives the amounts it could not attribute.
NOT_SPECIFIED_KEY: Final = "not_specified"

#: Detail-style: one row per transaction, every column keyed, a keyless column
#: cannot be allowlisted (TransactionList, GeneralLedger, the aging details).
STYLE_DETAIL: Final = "detail"
#: Hierarchical or summary: sections with headers and totals, a label column
#: that may be keyless, and possibly period columns with dynamic keys.
STYLE_HIERARCHICAL: Final = "hierarchical"


@dataclass(frozen=True, slots=True)
class ReportShape:
    """What one report's columns look like, and which of them are published.

    ``allowed_keys`` are leaf ColKeys (the allowlist proper). ``money_keys``
    are keys that carry amounts and are flagged if declared non-monetary.
    ``period_columns`` admits the SUMMARISED columns a ``summarize_column_by``
    report declares: Money leaves that carry ``StartDate`` and ``EndDate``
    metadata whatever their key, and, under a request that named an entity
    grouping, Money leaves keyed by an entity id (:data:`ENTITY_GROUPINGS`).
    ``titled_keys`` are keys whose cells carry the column title, because the
    key alone (``0``, ``1``) says nothing to a reader.
    """

    style: str
    allowed_keys: frozenset[str]
    money_keys: frozenset[str] = frozenset()
    period_columns: bool = False
    titled_keys: frozenset[str] = frozenset()
    #: KEYLESS Money columns admitted by their ColTitle, under a synthetic
    #: key: TrialBalance declares no ColKey on any column and its two Money
    #: columns are "Debit" and "Credit" by title only (observed 2026-09-24).
    #: Titles are user language, so this is used only on a report with no
    #: keys at all to match on, and the column says it was admitted by title.
    titled_money: dict[str, str] = field(default_factory=dict)


#: The aging summaries' bucket keys, with the human label in the ColTitle
#: ("1 - 30" and so on), observed 2026-09-24.
AGING_BUCKET_KEYS: Final[frozenset[str]] = frozenset({"current", "0", "1", "2", "3", "total"})

_TRANSACTION_LIST_MONEY: Final[frozenset[str]] = frozenset(
    {
        "subt_nat_amount", "nat_amount", "nat_open_bal",
        "subt_nat_home_amount", "nat_home_amount", "nat_home_open_bal",
    }
)
_GENERAL_LEDGER_MONEY: Final[frozenset[str]] = frozenset(
    {"subt_nat_amount", "rbal_nat_amount", "net_amount", "tax_amount"}
)

#: Per-report column shapes. In the comments below, "observed" is what a live
#: sandbox response showed (2026-09-24 unless stated); a key present on Intuit's
#: page for the report but not yet seen live is listed as documented, not
#: confirmed. Sensitive keys (:data:`SENSITIVE_COLUMN_KEYS`) are never in an
#: allowlist and a second gate refuses them anyway.
REPORT_SHAPES: Final[dict[str, ReportShape]] = {
    # UNVERIFIED against sandbox beyond the 16 columns the 2026-09-23 probe
    # returned (docs/api-findings.md); the list is deliberately
    # conservative and **fails closed** -- an unrecognised key is excluded and
    # named in `excluded_column_keys`, never passed through as harmless.
    "TransactionList": ReportShape(
        STYLE_DETAIL,
        frozenset(
            {
                "tx_date", "txn_type", "doc_num", "name", "memo",
                "account_name", "split_acc", "other_account",
                "subt_nat_amount", "nat_amount", "nat_open_bal",
                "subt_nat_home_amount", "nat_home_amount", "nat_home_open_bal",
                "is_cleared", "is_ap_paid", "is_no_post",
                "term_name", "due_date", "currency", "exch_rate",
                "dept_name", "klass_name", "tax_code",
                "create_date", "last_mod_date",
            }
        ),
        money_keys=_TRANSACTION_LIST_MONEY,
    ),
    # Observed: account (Account) + total (Money) by default; by Month the
    # Money columns are keyed by period label with StartDate/EndDate metadata.
    # Row groups seen: Income, GrossProfit, Expenses, NetOperatingIncome,
    # NetIncome.
    "ProfitAndLoss": ReportShape(
        STYLE_HIERARCHICAL, frozenset({"account", "total"}),
        money_keys=frozenset({"total"}), period_columns=True,
    ),
    # Observed: as ProfitAndLoss; by Quarter keyed 'Jan - Mar, 2026'. Four
    # levels deep.
    "BalanceSheet": ReportShape(
        STYLE_HIERARCHICAL, frozenset({"account", "total"}),
        money_keys=frozenset({"total"}), period_columns=True,
    ),
    # Observed default columns: tx_date, txn_type, doc_num, name, memo,
    # split_acc, subt_nat_amount, rbal_nat_amount. Documented requestable
    # columns: account_name, cust_name, vend_name, emp_name, item_name,
    # inv_date, is_adj, is_ap_paid, is_ar_paid, is_cleared, quantity, rate,
    # tax_code, klass_name, dept_name, create_date, last_mod_date, net_amount,
    # tax_amount. Deliberately absent: account_num (an account number;
    # sensitive), chk_print_state, create_by and last_mod_by (user identities,
    # unreviewed).
    "GeneralLedger": ReportShape(
        STYLE_DETAIL,
        frozenset(
            {
                "tx_date", "txn_type", "doc_num", "name", "memo", "split_acc",
                "subt_nat_amount", "rbal_nat_amount",
                "account_name", "cust_name", "vend_name", "emp_name", "item_name",
                "inv_date", "is_adj", "is_ap_paid", "is_ar_paid", "is_cleared",
                "quantity", "rate", "net_amount", "tax_amount", "tax_code",
                "klass_name", "dept_name", "create_date", "last_mod_date",
            }
        ),
        money_keys=_GENERAL_LEDGER_MONEY,
    ),
    # Observed: keyless Vendor label + current/0/1/2/3/total.
    "AgedPayables": ReportShape(
        STYLE_HIERARCHICAL, AGING_BUCKET_KEYS,
        money_keys=AGING_BUCKET_KEYS, titled_keys=AGING_BUCKET_KEYS,
    ),
    # Observed: keyless Customer label + the same buckets.
    "AgedReceivables": ReportShape(
        STYLE_HIERARCHICAL, AGING_BUCKET_KEYS,
        money_keys=AGING_BUCKET_KEYS, titled_keys=AGING_BUCKET_KEYS,
    ),
    # Observed default: tx_date, txn_type, doc_num, vend_name, due_date,
    # past_due, subt_neg_amount, subt_neg_open_bal. Documented: memo,
    # term_name, vend_comp_name, dept_name, create_date, last_mod_date.
    # Deliberately absent: vend_bill_addr, vend_pri_cont, vend_pri_email,
    # vend_pri_tel (sensitive), create_by, last_mod_by (unreviewed user
    # identities).
    "AgedPayableDetail": ReportShape(
        STYLE_DETAIL,
        frozenset(
            {
                "tx_date", "txn_type", "doc_num", "vend_name", "due_date", "past_due",
                "subt_neg_amount", "subt_neg_open_bal",
                "memo", "term_name", "vend_comp_name", "dept_name",
                "create_date", "last_mod_date",
            }
        ),
        money_keys=frozenset({"subt_neg_amount", "subt_neg_open_bal"}),
    ),
    # Observed default: tx_date, txn_type, doc_num, cust_name, due_date,
    # subt_amount, subt_open_bal. Documented: memo, past_due, term_name,
    # cust_comp_name, sale_sent_state, dept_name, create_date, last_mod_date.
    # Deliberately absent: bill_addr, ship_addr, deliv_addr, cust_bill_email,
    # cust_msg (sensitive), sales_cust1-3 (custom fields, unreviewed),
    # create_by, last_mod_by.
    "AgedReceivableDetail": ReportShape(
        STYLE_DETAIL,
        frozenset(
            {
                "tx_date", "txn_type", "doc_num", "cust_name", "due_date",
                "subt_amount", "subt_open_bal",
                "memo", "past_due", "term_name", "cust_comp_name", "sale_sent_state",
                "dept_name", "create_date", "last_mod_date",
            }
        ),
        money_keys=frozenset({"subt_amount", "subt_open_bal"}),
    ),
    # -- run_report: the reports with parser rules of their own --------------
    # Observed: keyless ProductsAndService label; a 'total' parent grouping
    # seven Money leaves, three with period metadata.
    "ItemSales": ReportShape(
        STYLE_HIERARCHICAL,
        frozenset({"Quantity", "Amount", "PercentSales", "AvgPrice", "Cogs", "GrossMargin", "GrossMarginPerc"}),
        period_columns=True,
    ),
    # Observed (wide window): tx_date Date, txn_type String, doc_num String,
    # name String, quantity Quantity, rate Money, home_amount Money,
    # qty_on_hand String, home_asset_value Money. The empty window declared
    # the same nine keys with no ColType.
    "InventoryValuationDetail": ReportShape(
        STYLE_DETAIL,
        frozenset(
            {"tx_date", "txn_type", "doc_num", "name", "quantity", "rate",
             "home_amount", "qty_on_hand", "home_asset_value"}
        ),
        money_keys=frozenset({"home_amount", "home_asset_value"}),
    ),
    # Observed: no ColKey on ANY column -- Account, Money "Debit", Money
    # "Credit". The label is admitted by position; the two Money columns by
    # title under synthetic keys. period_columns because Intuit's page lists
    # summarize_column_by; what it returns then has not been observed.
    "TrialBalance": ReportShape(
        STYLE_HIERARCHICAL, frozenset(), period_columns=True,
        titled_money={"Debit": "debit", "Credit": "credit"},
    ),
    # -- run_report: the rest --------------------------------------------
    # Every vocabulary below is what the sandbox returned on 2026-09-24 plus
    # the requestable keys Intuit's page lists, each named as
    # documented-not-seen. Address, email, telephone, message and
    # account-number columns are never allowlisted and SENSITIVE_COLUMN_KEYS
    # refuses them a second time; create_by and last_mod_by (user identities)
    # are unreviewed and absent.
    #
    # Observed: account_name String, account_type String, detail_acc_type
    # String, account_desc String, account_bal Money; rows carry no id.
    # Documented: create_date, last_mod_date; absent create_by, last_mod_by.
    "AccountList": ReportShape(
        STYLE_DETAIL,
        frozenset({"account_name", "account_type", "detail_acc_type", "account_desc", "account_bal",
                   "create_date", "last_mod_date"}),
        money_keys=frozenset({"account_bal"}),
    ),
    # Observed: keyless Account label + total; sections by group
    # (OperatingActivities, ...). Intuit's page lists summarize_column_by.
    "CashFlow": ReportShape(
        STYLE_HIERARCHICAL, frozenset({"total"}), money_keys=frozenset({"total"}), period_columns=True,
    ),
    # Observed: keyless Customer label + total. Intuit's page lists
    # summarize_column_by.
    "CustomerBalance": ReportShape(
        STYLE_HIERARCHICAL, frozenset({"total"}), money_keys=frozenset({"total"}), period_columns=True,
    ),
    # Observed default: tx_date, txn_type, doc_num, due_date, subt_amount,
    # subt_open_bal, rbal_open_bal, one section per customer whose header
    # carries the id. Documented: memo, term_name, cust_comp_name, cust_name,
    # sale_sent_state, dept_name, create_date, last_mod_date. Deliberately
    # absent: bill_addr, ship_addr, deliv_addr, cust_bill_email, cust_msg,
    # cust_tel, cust_phone_other (sensitive); ship_date, ship_via,
    # tracking_num, sales_cust1-3 (unreviewed); create_by, last_mod_by.
    "CustomerBalanceDetail": ReportShape(
        STYLE_DETAIL,
        frozenset({"tx_date", "txn_type", "doc_num", "due_date", "subt_amount", "subt_open_bal", "rbal_open_bal",
                   "memo", "term_name", "cust_comp_name", "cust_name", "sale_sent_state", "dept_name",
                   "create_date", "last_mod_date"}),
        money_keys=frozenset({"subt_amount", "subt_open_bal", "rbal_open_bal"}),
    ),
    # Observed: keyless Customer label + income, expense, total (Money).
    # Intuit's page lists summarize_column_by.
    "CustomerIncome": ReportShape(
        STYLE_HIERARCHICAL, frozenset({"income", "expense", "total"}),
        money_keys=frozenset({"income", "expense", "total"}), period_columns=True,
    ),
    # Observed: keyless ProductsAndService label + Item/ItemSKU String,
    # Quantity Money, HomeAmount Money, AvgCost Money -- this report declares
    # its quantity column Money, unlike InventoryValuationDetail, and the key
    # makes it a quantity (QUANTITY_COLUMN_KEYS), so it is no money key here.
    # Intuit's page lists summarize_column_by.
    "InventoryValuationSummary": ReportShape(
        STYLE_HIERARCHICAL, frozenset({"Item/ItemSKU", "Quantity", "HomeAmount", "AvgCost"}),
        money_keys=frozenset({"HomeAmount", "AvgCost"}), period_columns=True,
    ),
    # Observed default: tx_date, txn_type, doc_num, name, memo, account_name,
    # debt_amt, credit_amt. Documented: due_date, is_ar_paid, is_ap_paid,
    # item_name, journal_code_name, paid_date, pmt_mthd, quantity, rate,
    # txn_num, neg_open_bal, create_date, last_mod_date. Deliberately absent:
    # acct_num_with_extn (an account number), create_by, last_mod_by.
    "JournalReport": ReportShape(
        STYLE_DETAIL,
        frozenset({"tx_date", "txn_type", "doc_num", "name", "memo", "account_name", "debt_amt", "credit_amt",
                   "due_date", "is_ar_paid", "is_ap_paid", "item_name", "journal_code_name", "paid_date",
                   "pmt_mthd", "quantity", "rate", "txn_num", "neg_open_bal", "create_date", "last_mod_date"}),
        money_keys=frozenset({"debt_amt", "credit_amt", "neg_open_bal"}),
    ),
    # Observed default: as GeneralLedger's eight. Documented: pmt_mthd,
    # tax_code, klass_name, dept_name, create_date, last_mod_date; absent
    # create_by, last_mod_by.
    "ProfitAndLossDetail": ReportShape(
        STYLE_DETAIL,
        frozenset({"tx_date", "txn_type", "doc_num", "name", "memo", "split_acc", "subt_nat_amount",
                   "rbal_nat_amount", "pmt_mthd", "tax_code", "klass_name", "dept_name", "create_date",
                   "last_mod_date"}),
        money_keys=frozenset({"subt_nat_amount", "rbal_nat_amount"}),
    ),
    # Observed: keyless Class / Customer / Department label + total. Intuit's
    # pages for all three list summarize_column_by.
    "ClassSales": ReportShape(
        STYLE_HIERARCHICAL, frozenset({"total"}), money_keys=frozenset({"total"}), period_columns=True,
    ),
    "CustomerSales": ReportShape(
        STYLE_HIERARCHICAL, frozenset({"total"}), money_keys=frozenset({"total"}), period_columns=True,
    ),
    "DepartmentSales": ReportShape(
        STYLE_HIERARCHICAL, frozenset({"total"}), money_keys=frozenset({"total"}), period_columns=True,
    ),
    # DOCUMENTATION-DERIVED, never observed live (fault 5020 on the sandbox
    # company, 2026-09-24). The sample on Intuit's page declares two keyless
    # columns, a String label and a Money "Total", and one Data row per
    # "Case NN" group; the label is admitted by position and the total by
    # title, like TrialBalance. Every result says so.
    "TaxSummary": ReportShape(
        STYLE_HIERARCHICAL, frozenset(), titled_money={"Total": "total"},
    ),
    # Observed default: tx_date, txn_type, doc_num, is_no_post, memo,
    # account_name, amount (ByCustomer) / nat_amount (ByVendor). Documented:
    # name, other_account, due_date, inv_date, is_ap_paid, is_ar_paid,
    # is_cleared, is_adj, pmt_mthd, term_name, printed, olb_status,
    # extra_doc_num, cust_name / vendor_name, po_status, dept_name,
    # create_date, last_mod_date. Deliberately absent: cust_msg (a customer
    # message), tracking_num, ship_via, sales_cust1-3 (unreviewed),
    # create_by, last_mod_by.
    "TransactionListByCustomer": ReportShape(
        STYLE_DETAIL,
        frozenset({"tx_date", "txn_type", "doc_num", "is_no_post", "memo", "account_name", "amount",
                   "name", "other_account", "due_date", "inv_date", "is_ap_paid", "is_ar_paid", "is_cleared",
                   "is_adj", "pmt_mthd", "term_name", "printed", "olb_status", "extra_doc_num", "cust_name",
                   "dept_name", "create_date", "last_mod_date"}),
        money_keys=frozenset({"amount"}),
    ),
    "TransactionListByVendor": ReportShape(
        STYLE_DETAIL,
        frozenset({"tx_date", "txn_type", "doc_num", "is_no_post", "memo", "account_name", "nat_amount",
                   "name", "other_account", "due_date", "inv_date", "is_ap_paid", "is_ar_paid", "is_cleared",
                   "is_adj", "pmt_mthd", "term_name", "printed", "olb_status", "extra_doc_num", "vendor_name",
                   "po_status", "dept_name", "create_date", "last_mod_date"}),
        money_keys=frozenset({"nat_amount"}),
    ),
    # Observed default: tx_date, txn_type, doc_num, is_no_post, name, memo,
    # account_name, nat_amount. Documented: amount, is_adj, cust_name,
    # vend_name, rate, quantity, item_name, emp_name, pmt_mthd, nat_open_bal,
    # tax_type, is_billable, debt_amt, credit_amt, is_cleared, olb_status,
    # dept_name, create_date, last_mod_date; absent create_by, last_mod_by.
    "TransactionListWithSplits": ReportShape(
        STYLE_DETAIL,
        frozenset({"tx_date", "txn_type", "doc_num", "is_no_post", "name", "memo", "account_name", "nat_amount",
                   "amount", "is_adj", "cust_name", "vend_name", "rate", "quantity", "item_name", "emp_name",
                   "pmt_mthd", "nat_open_bal", "tax_type", "is_billable", "debt_amt", "credit_amt", "is_cleared",
                   "olb_status", "dept_name", "create_date", "last_mod_date"}),
        money_keys=frozenset({"nat_amount", "amount", "nat_open_bal", "debt_amt", "credit_amt"}),
    ),
    # Observed: keyless Vendor label + total. Intuit's VendorBalance and
    # VendorExpenses pages list summarize_column_by.
    "VendorBalance": ReportShape(
        STYLE_HIERARCHICAL, frozenset({"total"}), money_keys=frozenset({"total"}), period_columns=True,
    ),
    # Observed default: tx_date, txn_type, doc_num, due_date, subt_neg_amount,
    # subt_neg_open_bal, rbal_neg_open_bal. Documented: memo, term_name,
    # vend_comp_name, vend_name, dept_name, create_date, last_mod_date.
    # Deliberately absent: vend_bill_addr, vend_pri_cont, vend_pri_email,
    # vend_pri_tel (sensitive), create_by, last_mod_by.
    "VendorBalanceDetail": ReportShape(
        STYLE_DETAIL,
        frozenset({"tx_date", "txn_type", "doc_num", "due_date", "subt_neg_amount", "subt_neg_open_bal",
                   "rbal_neg_open_bal", "memo", "term_name", "vend_comp_name", "vend_name", "dept_name",
                   "create_date", "last_mod_date"}),
        money_keys=frozenset({"subt_neg_amount", "subt_neg_open_bal", "rbal_neg_open_bal"}),
    ),
    "VendorExpenses": ReportShape(
        STYLE_HIERARCHICAL, frozenset({"total"}), money_keys=frozenset({"total"}), period_columns=True,
    ),
}

#: Column keys that must never leave this process, whatever a report returns.
#: A second gate behind the per-report allowlist: if a new column key is added
#: to an allowlist by mistake, this still catches the categories that matter.
SENSITIVE_COLUMN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "addr", "address", "bill_addr", "billaddr", "ship_addr", "shipaddr",
        "email", "email_addr", "phone", "phone_num", "fax", "website",
        "account_num", "acct_num", "bank_account_num", "routing_num",
        "tax_id", "taxid", "ssn", "ein", "vendor_tax_id",
        "cust_msg", "customer_memo", "print_on_check_name",
        "contact", "contact_name", "full_name_with_address",
        # The aging detail pages' contact and address columns.
        "vend_bill_addr", "vend_pri_cont", "vend_pri_email", "vend_pri_tel",
        "cust_bill_email", "deliv_addr",
        # CustomerBalanceDetail's telephone columns and JournalReport's
        # account number with extension.
        "cust_tel", "cust_phone_other", "acct_num_with_extn",
    }
)

#: Per-report column allowlists, keyed on the leaf ColKey. The same table
#: gates the request (:func:`requestable_columns`) and the response
#: (:func:`select_columns`), so a withheld column is never fetched.
REPORT_COLUMN_ALLOWLIST: Final[dict[str, frozenset[str]]] = {
    name: shape.allowed_keys for name, shape in REPORT_SHAPES.items()
}

#: Column keys that carry amounts, per report. A published one declared with a
#: non-monetary type would otherwise lose every amount without a word -- a
#: recognised type, just not the one that carries money.
MONEY_COLUMN_KEYS: Final[dict[str, frozenset[str]]] = {
    name: shape.money_keys for name, shape in REPORT_SHAPES.items() if shape.money_keys
}

#: Tool-facing report names -> the live endpoint path below ``reports/``.
#:
#: Intuit's pages are titled one way and name their endpoint another:
#: APAgingSummary is ``reports/AgedPayables``, ARAgingSummary
#: ``reports/AgedReceivables``, APAgingDetail ``reports/AgedPayableDetail``,
#: ARAgingDetail ``reports/AgedReceivableDetail``; ProfitAndLoss, BalanceSheet
#: and GeneralLedger match their titles. All seven answered 200 live on
#: 2026-09-24. run_report's entries extend the map (ClassSales, CustomerSales,
#: DepartmentSales, ItemSales, AccountList ...). The sample request on Intuit's
#: CustomerBalanceDetail page carries the typo "CustomerBalancedDetail"; it is
#: not propagated.
REPORT_ENDPOINTS: Final[dict[str, str]] = {
    "ProfitAndLoss": "ProfitAndLoss",
    "BalanceSheet": "BalanceSheet",
    "GeneralLedger": "GeneralLedger",
    "APAgingSummary": "AgedPayables",
    "ARAgingSummary": "AgedReceivables",
    "APAgingDetail": "AgedPayableDetail",
    "ARAgingDetail": "AgedReceivableDetail",
    # run_report: the nine paths that differ from their page titles are the
    # five below plus the four aging paths above.
    "AccountListDetail": "AccountList",
    "CashFlow": "CashFlow",
    "CustomerBalance": "CustomerBalance",
    "CustomerBalanceDetail": "CustomerBalanceDetail",
    "CustomerIncome": "CustomerIncome",
    "InventoryValuationDetail": "InventoryValuationDetail",
    "InventoryValuationSummary": "InventoryValuationSummary",
    "JournalReport": "JournalReport",
    "ProfitAndLossDetail": "ProfitAndLossDetail",
    "SalesByClassSummary": "ClassSales",
    "SalesByCustomer": "CustomerSales",
    "SalesByDepartment": "DepartmentSales",
    "SalesByProduct": "ItemSales",
    "TaxSummary": "TaxSummary",
    "TransactionListByCustomer": "TransactionListByCustomer",
    "TransactionListByVendor": "TransactionListByVendor",
    "TransactionListWithSplits": "TransactionListWithSplits",
    "TrialBalance": "TrialBalance",
    "VendorBalance": "VendorBalance",
    "VendorBalanceDetail": "VendorBalanceDetail",
    "VendorExpenses": "VendorExpenses",
}


#: ``transaction_type`` values accepted by TransactionList.
#:
#: **A floor, not a closed set.** An unaccepted value returns HTTP 400
#: ``Invalid Enumeration``, so each of these ten is proven by a 200 (all
#: re-confirmed by a controlled probe). The candidates tried were the types
#: present in one sandbox company plus the common entity names, so a value
#: nobody tried could still be accepted: testing every candidate is not
#: testing every possible value.
REPORT_TRANSACTION_TYPES: Final[tuple[str, ...]] = (
    "Bill",
    "Charge",
    "Check",
    "CreditCardCharge",
    "CreditMemo",
    "Deposit",
    "Estimate",
    "Invoice",
    "JournalEntry",
    "SalesReceipt",
)

#: ``source_account_type`` values accepted by TransactionList.
#:
#: **Every documented value, not necessarily every accepted one**: 15 of 15
#: values of the documented AccountType enum **with the spaces removed**, each
#: proven by a 200. An undocumented value could still be accepted.
#:
#: The spacing is the trap. ``AccountsPayable`` filters; ``Accounts Payable``
#: is a 400. The chart of accounts -- including this server's own
#: ``list_accounts``, whose ``account_type`` field passes the value through
#: verbatim -- reports the spaced form. A caller who reads an account type off
#: an account and filters transactions with it gets a hard error.
REPORT_SOURCE_ACCOUNT_TYPES: Final[tuple[str, ...]] = (
    "Bank",
    "AccountsReceivable",
    "OtherCurrentAsset",
    "FixedAsset",
    "OtherAsset",
    "AccountsPayable",
    "CreditCard",
    "LongTermLiability",
    "OtherCurrentLiability",
    "Equity",
    "Income",
    "CostOfGoodsSold",
    "Expense",
    "OtherExpense",
    "OtherIncome",
)

#: ``transaction_type`` values proven REFUSED, kept so the refusal can explain
#: itself.
#:
#: Every one appears somewhere a caller might reasonably copy it from: the
#: ``txn_type`` column of this very report, the entity names this server's
#: other tools use, or the QuickBooks user interface. Naming them is the
#: difference between "invalid value" and "you have read the display label, and
#: the filter takes a different vocabulary".
REPORT_TRANSACTION_TYPES_REJECTED: Final[frozenset[str]] = frozenset(
    {
        # Display labels, as they appear in the txn_type column.
        "Payment", "Expense", "Cash Expense", "Credit Card Expense",
        "Credit Card Credit", "Bill Payment (Check)", "Bill Payment (Credit Card)",
        "Journal Entry", "Sales Receipt", "Credit Memo", "Sales Tax Payment",
        "Purchase Order", "Time Charge", "Billable Expense Charge",
        "Inventory Qty Adjust", "Refund",
        # Entity names, as used by run_query and get_transaction.
        "Purchase", "BillPayment", "Transfer", "VendorCredit",
    }
)

_ENTITY_NAME_VALUES: Final[frozenset[str]] = frozenset(
    {"Purchase", "BillPayment", "Transfer", "VendorCredit"}
)

#: Filters whose value is an id or free text. These are the only ones where a
#: wrong value can produce a silent empty result; the enumerated ones 400.
SILENT_MISMATCH_FILTERS: Final[frozenset[str]] = frozenset(
    {"vendor", "customer", "docnum", "memo"}
)

#: Filters whose value is drawn from a closed enumeration and refused loudly.
ENUMERATED_FILTERS: Final[frozenset[str]] = frozenset(
    {"transaction_type", "cleared", "source_account_type"}
)

_REFUSED_SUFFIX: Final = (
    "QuickBooks would refuse it with HTTP 400 Invalid Enumeration, so this is "
    "rejected here instead: a schema-level enum turns a runtime API failure "
    "into an impossible input."
)


def explain_rejected_transaction_type(value: str) -> str:
    """A refusal that says where the caller probably got the value from.

    It never repeats the value. Where the value is recognised, the
    explanation says what kind of value it was, which is the useful part.
    """
    if value in _ENTITY_NAME_VALUES:
        reason = (
            "that is an entity name, used by run_query and get_transaction. The "
            "report filter uses a different vocabulary"
        )
    elif value in REPORT_TRANSACTION_TYPES_REJECTED:
        reason = (
            "that is a display label, as it appears in the txn_type column of "
            "this report. The filter does not accept the labels it emits"
        )
    else:
        reason = "that value is not in this filter's vocabulary"
    return (
        f"the transaction_type given cannot be used: {reason}. {_REFUSED_SUFFIX} "
        f"Accepted values: {', '.join(REPORT_TRANSACTION_TYPES)}."
    )


def explain_rejected_source_account_type(value: str) -> str:
    """A refusal that names the spacing trap when that is what happened."""
    despaced = value.replace(" ", "")
    if despaced in REPORT_SOURCE_ACCOUNT_TYPES and despaced != value:
        # `despaced` is quoted because it is one of this package's own
        # constants, matched exactly; the caller's spelling is not.
        return (
            f"the source_account_type given cannot be used: this filter takes "
            f"the AccountType enum with the spaces removed, so use "
            f"'{despaced}'. The chart of accounts -- including list_accounts "
            f"here -- reports the spaced form, so the value you read off an "
            f"account is not the value that filters on it. {_REFUSED_SUFFIX}"
        )
    return (
        f"the source_account_type given cannot be used: that value is not in "
        f"this filter's vocabulary. {_REFUSED_SUFFIX} Accepted values: "
        f"{', '.join(REPORT_SOURCE_ACCOUNT_TYPES)}."
    )


class ReportError(QboMcpError):
    """The report broke the mapping between its cells and its columns.

    A structural failure: the whole request fails, because once that mapping
    is in doubt no cell can be trusted. The text is fixed and names a position,
    never a value -- the report is upstream data.
    """


def _structural(what: str) -> ReportError:
    return ReportError(
        f"QuickBooks returned a report this server cannot read safely: {what}. "
        f"Nothing from it is returned, because when the mapping between cells "
        f"and columns is in doubt no cell can be trusted."
    )


# --------------------------------------------------------------------------
# Anomalies
# --------------------------------------------------------------------------

ANOMALY_MALFORMED_AMOUNT: Final = "malformed_amount"
ANOMALY_MALFORMED_CELL: Final = "malformed_cell"
ANOMALY_SHORT_ROW: Final = "short_row"
ANOMALY_LONG_ROW: Final = "long_row"
ANOMALY_UNRECOGNISED_COLUMN_TYPE: Final = "unrecognised_column_type"
ANOMALY_STATED_TOTAL: Final = "stated_total_not_reproducible"
ANOMALY_UNRECOGNISED_ROW: Final = "unrecognised_row"

_UNRECOGNISED_ROW_DETAIL: Final = (
    "the row carries no cells, header, summary or nested rows this server "
    "reads, so nothing from it is returned"
)
_UNREAD_ROWS_DETAIL: Final = (
    "these rows are held outside the list this server reads, so nothing from "
    "them is returned"
)
_UNREAD_MEMBER_DETAIL: Final = (
    "the row holds a member this server doesn't read, so what it holds is not "
    "returned"
)

#: What a report row holds that this server knows. ``type`` names the kind of
#: row, which the other members already show.
_ROW_MEMBERS: Final = frozenset({"type", "group", "Header", "Rows", "Summary", "ColData"})

_MALFORMED_AMOUNT_DETAIL: Final = (
    "the cell's text is not an amount this server can read exactly; it is "
    "returned as text with no amount"
)
_MALFORMED_PERCENT_DETAIL: Final = (
    "the cell's text is not a percentage this server can read exactly (a "
    "decimal followed by '%'); it is returned as text with no amount"
)
_MALFORMED_QUANTITY_DETAIL: Final = (
    "the cell's text is not a number this server can read exactly; it is "
    "returned as text with no amount"
)
_MALFORMED_CELL_DETAIL: Final = (
    "the cell is not in the documented shape; it is returned with no text and "
    "no amount"
)
_UNRECOGNISED_TYPE_DETAIL: Final = (
    "the column's declared type is not one this server recognises, so its "
    "cells are returned as text with no amount"
)
_NON_MONEY_TYPE_DETAIL: Final = (
    "this column carries amounts but is declared with a non-monetary type, so "
    "its cells are returned as text with no amount"
)
_STATED_TOTAL_DETAIL: Final = (
    "QuickBooks states this total and it is reported as stated, but a cell it "
    "covers could not be read, so summing the returned cells would not "
    "reproduce it"
)


@dataclass(frozen=True, slots=True)
class ReportAnomaly:
    """One problem, listed at the top level of the result.

    ``row_id`` is ``None`` for a whole column; ``column_key`` is ``None`` for a
    whole row. ``detail`` is fixed text and never carries a cell's value.
    """

    row_id: str | None
    column_key: str | None
    kind: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "column_key": self.column_key,
            "kind": self.kind,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReportColumn:
    """One LEAF column: the thing a cell is paired with.

    ``key`` is the column's identity in this result: the leaf's own ``ColKey``
    for a flat column, the path ``parent/leaf`` for a nested one, a synthetic
    key for an admitted keyless label, and ``column_<index>`` for a keyless
    column nothing admits. ``leaf_key`` is the ColKey as declared (``None``
    when absent), which is what the allowlist is matched on. ``index`` is the
    leaf position, which is the position of the cell that belongs to it.
    """

    index: int
    key: str
    title: str
    type: str
    metadata: dict[str, str] = field(default_factory=dict)
    leaf_key: str | None = None
    parent_title: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    synthetic_key: bool = False
    #: Under an entity grouping of ``summarize_column_by``: the entity type
    #: the column names, the entity's id (``None`` for ``not_specified``),
    #: and whether the column is the entity's total (its key repeated after
    #: its children). See :data:`ENTITY_GROUPINGS`.
    entity_type: str | None = None
    entity_id: str | None = None
    entity_total: bool = False
    #: How the column came to be published: ``key`` (its ColKey is on the
    #: report's allowlist), ``period`` (StartDate/EndDate metadata and a monetary type),
    #: ``grouping`` (an entity id under a named grouping), ``label_position``
    #: (the keyless first column of a hierarchical report) or ``title`` (a
    #: keyless Money column matched by ColTitle on a report with no keys).
    #: ``None`` on a column that was not published.
    admitted_by: str | None = None

    @property
    def is_money(self) -> bool:
        """Declared monetary, and not a quantity by its key: a Money-typed
        ``Quantity`` column carries no currency and enters no money total."""
        return self.type in MONEY_COLUMN_TYPES and not self.is_quantity

    @property
    def is_grouping(self) -> bool:
        """A column an entity grouping produced: admitted by that shape."""
        return self.entity_type is not None

    @property
    def is_quantity(self) -> bool:
        """Declared ``Quantity``, or keyed as one whatever its declared type
        (:data:`QUANTITY_COLUMN_KEYS`)."""
        return self.type in QUANTITY_COLUMN_TYPES or (self.leaf_key or self.key) in QUANTITY_COLUMN_KEYS

    @property
    def is_percent(self) -> bool:
        """A Money-typed column whose declared key ends in ``Perc`` or
        ``PercentSales``, or whose title carries ``%`` (module docstring, the
        seventh shape). Only a Money column: the type is what QuickBooks
        declared, and a String column titled with a sign is still text. Never
        a grouping column by its title: that title is an entity's NAME, user
        data, and a customer named '10% Club' does not make its amounts
        percentages."""
        if not self.is_money:
            return False
        declared = self.leaf_key or self.key
        if declared.endswith(PERCENT_KEY_SUFFIXES):
            return True
        return not self.is_grouping and PERCENT_TITLE_MARK in (self.title or "")

    @property
    def is_period(self) -> bool:
        """The shape that identifies a period column: both bounds, real dates
        in order, and a declared monetary type. The DECLARED type, so a Money-typed
        quantity column keeps its period on each cell (ItemSales' ``Quantity``)."""
        if self.type not in MONEY_COLUMN_TYPES:
            return False
        start, end = _period_date(self.period_start), _period_date(self.period_end)
        return start is not None and end is not None and start <= end


_PERIOD_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _period_date(text: Any) -> date | None:
    """``text`` as a date, in the one form Intuit sends (``fromisoformat`` also
    takes compact and week forms)."""
    if not (isinstance(text, str) and _PERIOD_DATE.fullmatch(text)):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _is_list(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def parse_columns(
    payload: Mapping[str, Any],
    *,
    report_name: str | None = None,
    entity_grouping: str | None = None,
) -> list[ReportColumn]:
    """Read the declared column order, flattened to leaves, or fail.

    ``entity_grouping`` is the ``summarize_column_by`` value the REQUEST
    named, if any. Under one of the entity groupings (:data:`ENTITY_GROUPINGS`)
    a top-level Money leaf keyed by an integer-like id or ``not_specified``
    is annotated with the entity type and id, and an id that repeats after
    its children is that entity's total column. A period grouping (Month,
    Quarter, ...) annotates nothing, so a stray integer-keyed column there
    stays unallowlistable.

    ``ColKey`` lives in a ``MetaData`` name/value list rather than as a plain
    attribute. A column that carries ``Columns.Column`` is a GROUP: it
    publishes no cell of its own, and its children are the leaves, each
    carrying the group's title and its identity in their path. Cells are
    paired with leaves, in depth-first order, so the leaf count is what a
    row's cell count is measured against.

    A leaf with no key is ``column_<leaf index>`` -- unless ``report_name``
    names a hierarchical report and the leaf is the first column with a label
    type, in which case it is admitted under its synthetic key
    (:data:`LABEL_COLUMN_KEYS`). Without a report name, no synthetic key is
    given: the positional contract still holds and the column stays
    unallowlistable, which is the TransactionList rule.

    Every column definition must be readable. Skipping one that is not would
    compact the list while the surviving columns kept their original indexes
    -- and cells would then pair against the compacted list, each landing one
    column to the left. That is structural: it fails. So is a group whose
    children cannot be read, because then every leaf index after it is in
    doubt.
    """
    block = payload.get("Columns")
    raw = block.get("Column") if isinstance(block, Mapping) else None
    if not _is_list(raw):
        raise ReportError("the report carried no column definitions")

    shape = REPORT_SHAPES.get(report_name) if report_name else None
    hierarchical = shape is not None and shape.style == STYLE_HIERARCHICAL
    grouping_type = ENTITY_GROUPINGS.get(entity_grouping) if entity_grouping else None

    leaves: list[ReportColumn] = []
    seen: set[tuple[str, ...]] = set()
    seen_keys: set[str] = set()

    def read_definition(item: Any, where: str) -> tuple[dict[str, str], str | None, str | None]:
        if not isinstance(item, Mapping):
            raise _structural(f"column definition {where} is not an object")
        meta_raw = item.get("MetaData")
        if meta_raw is None:
            meta_raw = []
        if not _is_list(meta_raw):
            raise _structural(f"column definition {where} carries malformed metadata")
        metadata: dict[str, str] = {}
        for entry in meta_raw:
            if not isinstance(entry, Mapping):
                raise _structural(f"column definition {where} carries malformed metadata")
            name, value = entry.get("Name"), entry.get("Value")
            if name == "ColKey":
                if not isinstance(value, str) or not value or "ColKey" in metadata:
                    raise _structural(f"column definition {where} carries a malformed key")
            if isinstance(name, str) and isinstance(value, str):
                metadata[name] = value
        col_type = item.get("ColType")
        title = item.get("ColTitle")
        if col_type is not None and not isinstance(col_type, str):
            raise _structural(f"column definition {where} carries a malformed type")
        if title is not None and not isinstance(title, str):
            raise _structural(f"column definition {where} carries a malformed title")
        return metadata, col_type, title

    def walk(items: Sequence[Any], ancestors: tuple[str, ...], parent_title: str | None, where: str) -> None:
        for position, item in enumerate(items):
            here = f"{where}.{position}" if where else str(position)
            metadata, col_type, title = read_definition(item, here)
            own_key = metadata.get("ColKey")

            children = item.get("Columns")
            if children is not None:
                if not isinstance(children, Mapping) or not _is_list(children.get("Column")):
                    raise _structural(f"column definition {here} groups sub-columns in an unsupported shape")
                identity = own_key or title or f"group_{here}"
                walk(children["Column"], ancestors + (identity,), title or own_key or None, here)
                continue

            index = len(leaves)
            synthetic = False
            admitted: str | None = None
            if own_key is not None:
                leaf = own_key
            elif (
                hierarchical and index == 0 and not ancestors
                and col_type in LABEL_COLUMN_TYPES
            ):
                leaf = LABEL_COLUMN_KEYS[col_type]
                synthetic = True
                admitted = "label_position"
            elif (
                shape is not None and not ancestors
                and col_type in MONEY_COLUMN_TYPES and (title or "") in shape.titled_money
            ):
                # A report with no keys at all (TrialBalance): the monetary
                # columns are told apart by title only, and say so.
                leaf = shape.titled_money[title or ""]
                synthetic = True
                admitted = "title"
            else:
                leaf = f"column_{index}"

            entity_type = entity_id = None
            entity_total = False
            if (
                own_key is not None and grouping_type is not None and not ancestors
                and col_type in MONEY_COLUMN_TYPES
                and (own_key.isdigit() or own_key == NOT_SPECIFIED_KEY)
            ):
                entity_type = grouping_type
                entity_id = own_key if own_key.isdigit() else None
                if entity_id is not None and (own_key,) in seen:
                    # The entity's own column came first; this is its total
                    # over itself and its children (seen under Customers).
                    entity_total = True
                    leaf = f"{own_key} (total)"

            path = ancestors + (leaf,)
            key = "/".join(path)
            # Two paths can join to one key, when a ColKey holds a "/", and
            # cells are matched to the published columns by key: a repeat
            # would let an excluded column's cells through under a published
            # column's name.
            if path in seen or key in seen_keys:
                raise _structural(f"column definition {here} repeats a key another column uses")
            seen.add(path)
            seen_keys.add(key)
            leaves.append(
                ReportColumn(
                    index=index,
                    key=key,
                    title=title or "",
                    type=col_type or "",
                    metadata=metadata,
                    leaf_key=own_key,
                    parent_title=parent_title,
                    period_start=metadata.get("StartDate") or None,
                    period_end=metadata.get("EndDate") or None,
                    synthetic_key=synthetic,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    entity_total=entity_total,
                    admitted_by=admitted,
                )
            )

    walk(raw, (), None, "")
    return leaves


def allowed_column_keys(report_name: str) -> frozenset[str]:
    return REPORT_COLUMN_ALLOWLIST.get(report_name, frozenset())


def report_shape(report_name: str) -> ReportShape:
    """The shape for ``report_name``, or a closed detail shape that admits
    nothing: an unknown report publishes no column at all."""
    return REPORT_SHAPES.get(report_name) or ReportShape(STYLE_DETAIL, frozenset())


def select_columns(
    columns: Sequence[ReportColumn], report_name: str
) -> tuple[list[ReportColumn], list[str]]:
    """Split declared leaves into permitted and excluded.

    Returns ``(kept, excluded_keys)``. Excluded keys are named in the result so
    a caller can see that a column existed and was withheld -- which is a
    different statement from the column not existing.

    Four ways in, checked in this order after the sensitive gate:

    * the leaf's own ``ColKey`` is on the report's allowlist;
    * the report's shape declares period columns and the leaf has the period
      shape -- ``StartDate`` and ``EndDate`` metadata holding real dates in
      order, and a monetary type (Money, Amount or Rate) --
      whatever its key, because the key is the period label and changes with
      every window ('Jan 2026', 'Jan - Mar, 2026');
    * the leaf is keyless and :func:`parse_columns` gave it a synthetic key: the
      first column of a hierarchical report, of a label type, or a Money column
      with a title the report's shape lists;
    * the report's shape declares summarised columns and the leaf is a
      grouping column -- Money, keyed by an entity id or ``not_specified``,
      annotated by :func:`parse_columns` because the REQUEST named an entity
      grouping (:data:`ENTITY_GROUPINGS`; observed 2026-09-25).

    A keyless column anywhere else, of any type, is excluded and named: there
    is no identifier to allowlist and no position rule that covers it.
    """
    shape = report_shape(report_name)
    kept: list[ReportColumn] = []
    excluded: list[str] = []
    for column in columns:
        probe = column.leaf_key if column.leaf_key is not None else column.key
        if probe in SENSITIVE_COLUMN_KEYS or probe.casefold() in SENSITIVE_COLUMN_KEYS:
            excluded.append(column.key)
            continue
        if column.leaf_key is not None and column.leaf_key in shape.allowed_keys:
            kept.append(replace(column, admitted_by="key"))
        elif column.leaf_key is not None and shape.period_columns and column.is_period:
            kept.append(replace(column, admitted_by="period"))
        elif column.leaf_key is None and column.synthetic_key:
            kept.append(column)
        elif column.leaf_key is not None and shape.period_columns and column.is_grouping:
            kept.append(replace(column, admitted_by="grouping"))
        else:
            excluded.append(column.key)
    return kept, excluded


def requestable_columns(report_name: str) -> list[str]:
    """The column keys this server is willing to ask for.

    The allowlist is applied to the *request* as well as the response, so a
    withheld column is never fetched in the first place.
    """
    return sorted(allowed_column_keys(report_name))


# --------------------------------------------------------------------------
# Cells
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReportCell:
    column_key: str
    #: The verbatim upstream text. ``None`` where upstream sent ``""``.
    text: str | None
    #: Exact decimal string: money columns (with a currency) and quantity
    #: columns (without one). Never inferred from shape.
    amount: str | None = None
    currency: str | None = None
    #: ``ColData.id`` where present -- this is how a report row routes to
    #: ``get_transaction``, and how a hierarchical row names its account,
    #: customer, vendor or item.
    entity_id: str | None = None
    #: The column title, carried onto the cell where the key alone says
    #: nothing to a reader (an aging bucket keyed ``1`` titled "31 - 60").
    label: str | None = None
    #: ``start/end`` for a period column, so a cell under 'Jan 2026' says
    #: which dates that is.
    period: str | None = None
    #: ``percent`` on a percent column's cell (:data:`UNIT_PERCENT`), where
    #: ``amount`` is the figure before the sign and there is no currency.
    #: ``None`` on every other cell.
    unit: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"column_key": self.column_key, "text": self.text}
        if self.amount is not None:
            out["amount"] = self.amount
        if self.currency is not None:
            out["currency"] = self.currency
        if self.unit is not None:
            out["unit"] = self.unit
        if self.entity_id is not None:
            out["entity_id"] = self.entity_id
        if self.label is not None:
            out["label"] = self.label
        if self.period is not None:
            out["period"] = self.period
        return out


#: An amount as a report cell writes one: optional minus, digits with commas
#: only as thousands separators, an optional fraction. Parentheses for a
#: negative are handled first. Anything else -- NaN, Infinity, an exponent, a
#: currency symbol, a misplaced comma -- is not read at all.
_AMOUNT_RE: Final = re.compile(r"^-?(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d+)?$")


def _exact_decimal(text: str) -> str | None:
    """Exact decimal string, or ``None`` if the text is not a readable amount.

    Decimal, not float: a report is money and binary floating point loses
    cents. ``Decimal`` itself is too willing -- it accepts ``NaN``,
    ``Infinity`` and exponents -- so the shape is checked first. Returning
    ``None`` rather than guessing keeps ``text`` the single source of truth for
    anything that does not parse; the caller lists it as an anomaly.
    """
    candidate = text.strip()
    if candidate.startswith("(") and candidate.endswith(")"):
        inner = candidate[1:-1]
        if not inner or inner.startswith("-") or inner != inner.strip():
            return None
        candidate = "-" + inner
    if not candidate or candidate in ("-", ".", "-.") or not _AMOUNT_RE.match(candidate):
        return None
    try:
        return str(Decimal(candidate.replace(",", "")))
    except InvalidOperation:  # pragma: no cover - the shape check admits only decimals
        return None


def _exact_percent(text: str) -> str | None:
    """The figure before a trailing ``%``, read by :func:`_exact_decimal`, or
    ``None``. Exactly one sign at the end, whatever whitespace precedes it
    (the live cells carry one space: ``'30.69 %'``); a bare number, a sign
    anywhere else, or a non-decimal before it is not a percent this server
    can read."""
    candidate = text.strip()
    if not candidate.endswith(PERCENT_TITLE_MARK):
        return None
    figure = candidate[:-1].strip()
    if not figure or PERCENT_TITLE_MARK in figure:
        return None
    return _exact_decimal(figure)


def build_cells(
    row_data: Sequence[Any],
    columns: Sequence[ReportColumn],
    *,
    currency: str | None,
    kept_keys: frozenset[str],
    row_id: str = "",
    anomalies: list[ReportAnomaly] | None = None,
    titled_keys: frozenset[str] = frozenset(),
) -> tuple[list[ReportCell], int]:
    """Pair cells with the declared LEAF column order.

    Returns ``(cells, upstream_cell_count)``. The upstream count is every cell
    upstream sent, taken **before** any allowlist filtering, so a caller can
    tell that cells existed and were withheld.

    A row whose cell count differs from the declared leaf count cannot be
    paired without a guess, so none of its values is published: the row is
    listed as an anomaly and its cells are withheld, whether it is short or
    long and whether or not every declared column is published. Pairing a
    short row by position assumes the missing cells are the last ones, and
    when that guess is wrong a value -- and the routing id beside it -- lands
    under the wrong key. Withholding only the rows that touch a withheld
    column is too narrow: with every column published, a short row can still
    put an amount under ``txn_type``.
    """
    found = anomalies if anomalies is not None else []
    upstream_count = len(row_data)
    expected = len(columns)

    if upstream_count != expected:
        kind = ANOMALY_SHORT_ROW if upstream_count < expected else ANOMALY_LONG_ROW
        found.append(
            ReportAnomaly(
                row_id=row_id,
                column_key=None,
                kind=kind,
                detail=(
                    f"the row carried {upstream_count} cells for {expected} declared "
                    f"columns. Its cells are withheld: pairing them by position "
                    f"would guess which column each value belongs to"
                ),
            )
        )
        return [], upstream_count

    cells: list[ReportCell] = []
    for column, raw in zip(columns, row_data):
        if column.key not in kept_keys:
            continue
        cells.append(
            _read_cell(
                raw, column, currency=currency, row_id=row_id, anomalies=found,
                # A grouping column's key is an id: the entity's name rides on
                # the cell as its label, as an aging bucket's title does.
                titled=(column.leaf_key in titled_keys) or column.is_grouping,
            )
        )
    return cells, upstream_count


def _read_cell(
    raw: Any,
    column: ReportColumn,
    *,
    currency: str | None,
    row_id: str,
    anomalies: list[ReportAnomaly],
    titled: bool = False,
) -> ReportCell:
    """One cell: verbatim text, and an exact number only where both the column
    and the text say it is one. Never coerced. Money gets a currency; a
    quantity does not."""
    label = (column.title or None) if titled else None
    period = (
        f"{column.period_start}/{column.period_end}" if column.is_period else None
    )

    def malformed() -> ReportCell:
        anomalies.append(
            ReportAnomaly(row_id, column.key, ANOMALY_MALFORMED_CELL, _MALFORMED_CELL_DETAIL)
        )
        return ReportCell(column_key=column.key, text=None, label=label, period=period)

    if not isinstance(raw, Mapping):
        return malformed()

    value = raw.get("value")
    if value is not None and not isinstance(value, str):
        return malformed()
    text = value or None

    entity_id = raw.get("id")
    if entity_id is not None and not (
        isinstance(entity_id, str) or (isinstance(entity_id, int) and not isinstance(entity_id, bool))
    ):
        return malformed()

    amount = None
    unit = None
    if column.is_percent and text is not None:
        # The seventh shape (module docstring): a decimal followed by the
        # sign, and nothing else, is a percent. Anything else in a percent
        # column -- a bare number included -- is disclosed, never guessed.
        amount = _exact_percent(text)
        if amount is None:
            anomalies.append(
                ReportAnomaly(row_id, column.key, ANOMALY_MALFORMED_AMOUNT, _MALFORMED_PERCENT_DETAIL)
            )
        else:
            unit = UNIT_PERCENT
    elif (column.is_money or column.is_quantity) and text is not None:
        amount = _exact_decimal(text)
        if amount is None:
            anomalies.append(
                ReportAnomaly(
                    row_id, column.key, ANOMALY_MALFORMED_AMOUNT,
                    _MALFORMED_AMOUNT_DETAIL if column.is_money else _MALFORMED_QUANTITY_DETAIL,
                )
            )

    return ReportCell(
        column_key=column.key,
        text=text,
        amount=amount,
        currency=currency if (amount is not None and column.is_money and unit is None) else None,
        entity_id=str(entity_id) if entity_id not in (None, "") else None,
        label=label,
        period=period,
        unit=unit,
    )


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------

ROW_DATA: Final = "data"
ROW_SECTION_HEADER: Final = "section_header"
ROW_SECTION_SUMMARY: Final = "section_summary"

#: What a summary row covers.
COVERS_UPSTREAM_SECTION: Final = "upstream_section"
COVERS_UNKNOWN: Final = "unknown"


@dataclass(slots=True)
class FlatRow:
    row_id: str
    parent_row_id: str | None
    row_type: str
    depth: int
    group: str | None
    cells: list[ReportCell]
    #: Summary rows only: what the total covers, and over what scope.
    scope: str | None = None
    covers: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "row_id": self.row_id,
            "parent_row_id": self.parent_row_id,
            "row_type": self.row_type,
            "depth": self.depth,
            "group": self.group,
            "cells": [cell.to_json() for cell in self.cells],
        }
        if self.scope is not None:
            out["scope"] = self.scope
        if self.covers is not None:
            out["covers"] = self.covers
        return out


@dataclass(slots=True)
class FlattenedReport:
    report_name: str
    start_period: str | None
    end_period: str | None
    currency: str | None
    columns: list[ReportColumn]
    excluded_column_keys: list[str]
    rows: list[FlatRow]
    upstream_cell_count: int
    upstream_row_count: int
    upstream_partial: bool
    client_truncated: bool
    completeness: str
    #: Every cell-, row- and column-level problem, over every upstream row --
    #: including rows a client limit then trimmed. Empty means none was found.
    anomalies: list[ReportAnomaly] = field(default_factory=list)
    #: Things a reader should know that are not anomalies and do not make the
    #: result partial: fixed text, each said once. Today: published columns
    #: that declared no type on a report with no rows to type.
    disclosures: list[str] = field(default_factory=list)
    #: ``Header.ReportBasis``: the accounting method QuickBooks says it used.
    report_basis: str | None = None
    #: ``Header.SummarizeColumnsBy``, as echoed by QuickBooks.
    summarize_columns_by: str | None = None
    #: ``Header.DateMacro``, as echoed by QuickBooks (lower-cased by it).
    date_macro: str | None = None
    #: ``Header.Option[NoReportData]``: QuickBooks's own statement that the
    #: report holds no data. This is upstream's claim, reported as stated; it
    #: is never inferred from an empty rows array.
    no_report_data: bool | None = None


def flatten(
    payload: Mapping[str, Any],
    *,
    report_name: str,
    upstream_partial: bool,
    max_rows: int | None = None,
    entity_grouping: str | None = None,
) -> FlattenedReport:
    """Flatten a report payload, preserving the section tree.

    ``entity_grouping`` is the ``summarize_column_by`` the request named, so
    the columns an entity grouping produces can be admitted by their shape
    (:func:`parse_columns`, :data:`ENTITY_GROUPINGS`).

    ``row_id`` values are positional paths (``"0"``, ``"0.1"``, ``"0.1.2"``).
    QuickBooks report rows carry no identifier of their own, so these are
    synthesised -- but they are stable for a given payload and they preserve
    the association of a section header, its children and its summary at every
    depth, which is the part a caller actually needs.
    """
    header = payload.get("Header")
    header = header if isinstance(header, Mapping) else {}
    currency = _header_text(header, "Currency")

    columns = parse_columns(payload, report_name=report_name, entity_grouping=entity_grouping)
    kept, excluded = select_columns(columns, report_name)
    kept_keys = frozenset(column.key for column in kept)
    shape = report_shape(report_name)

    anomalies: list[ReportAnomaly] = []
    rows: list[FlatRow] = []
    cell_total = 0
    row_total = 0

    def cells_of(block: Mapping[str, Any], row_id: str) -> tuple[list[ReportCell], int]:
        col_data = block.get("ColData", [])
        if not _is_list(col_data):
            raise _structural(f"row {row_id} carries its cells in an unsupported shape")
        return build_cells(
            col_data, columns, currency=currency, kept_keys=kept_keys,
            row_id=row_id, anomalies=anomalies, titled_keys=shape.titled_keys,
        )

    def list_unread(row_id: str, parent: str | None, depth: int, detail: str) -> None:
        # A row with no cells, as a row with nothing read is, so what counts
        # rows sees it and a total over it is flagged.
        nonlocal row_total
        anomalies.append(ReportAnomaly(row_id, None, ANOMALY_UNRECOGNISED_ROW, detail))
        row_total += 1
        rows.append(
            FlatRow(row_id=row_id, parent_row_id=parent, row_type=ROW_DATA, depth=depth, group=None, cells=[])
        )

    def walk_rows(container: Any, parent: str | None, depth: int) -> None:
        nonlocal cell_total, row_total
        if container is None:
            return
        if not isinstance(container, Mapping):
            raise _structural(f"the rows under {parent or 'the report'} are not an object")
        block = container.get("Row")
        if block is None:
            block = []
        elif not _is_list(block):
            raise _structural(f"the rows under {parent or 'the report'} are not a list")
        for position, raw in enumerate(block):
            row_id = f"{parent}.{position}" if parent else str(position)
            if not isinstance(raw, Mapping):
                raise _structural(f"row {row_id} is not an object")
            group = _text_or_none(raw.get("group"), f"row {row_id}'s group")

            header_block = raw.get("Header")
            summary_block = raw.get("Summary")
            nested = raw.get("Rows")
            for name, part in (("header", header_block), ("summary", summary_block)):
                if part is not None and not isinstance(part, Mapping):
                    raise _structural(f"row {row_id} carries a {name} in an unsupported shape")
            if header_block is None and summary_block is None and nested is None and "ColData" not in raw:
                # Nothing here this server reads. Passed over quietly, a row in
                # a shape Intuit adds later would vanish from the result. Listed
                # as a short row is, with no cells, so what counts rows sees it.
                anomalies.append(
                    ReportAnomaly(row_id, None, ANOMALY_UNRECOGNISED_ROW, _UNRECOGNISED_ROW_DETAIL)
                )
                row_total += 1
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

            # A member this server doesn't read, beside ones it does: what it
            # holds would vanish as rows outside a list would. A header and a
            # total hold only cells, so the same goes for anything beside
            # theirs. Listed under the row, before its total if it has one, so
            # the total is flagged too.
            unread_id = (
                f"{row_id}.unread"
                if any(key not in _ROW_MEMBERS and value not in (None, "", [], {}) for key, value in raw.items())
                or any(
                    key != "ColData" and value not in (None, "", [], {})
                    for part in (header_block, summary_block)
                    if isinstance(part, Mapping)
                    for key, value in part.items()
                )
                else None
            )

            if isinstance(header_block, Mapping):
                cells, count = cells_of(header_block, f"{row_id}.header")
                cell_total += count
                row_total += 1
                rows.append(
                    FlatRow(
                        row_id=f"{row_id}.header",
                        parent_row_id=parent,
                        row_type=ROW_SECTION_HEADER,
                        depth=depth,
                        group=group,
                        cells=cells,
                    )
                )

            if nested is not None:
                walk_rows(nested, row_id, depth + 1)

            if unread_id is not None and isinstance(summary_block, Mapping):
                list_unread(unread_id, row_id, depth + 1, _UNREAD_MEMBER_DETAIL)

            if isinstance(summary_block, Mapping):
                cells, count = cells_of(summary_block, f"{row_id}.summary")
                cell_total += count
                row_total += 1
                rows.append(
                    FlatRow(
                        row_id=f"{row_id}.summary",
                        parent_row_id=parent,
                        row_type=ROW_SECTION_SUMMARY,
                        depth=depth,
                        group=group,
                        cells=cells,
                        # A total identifies what it is a total OF, and whether
                        # it covers everything upstream or only what came back.
                        scope=group or f"section {row_id}",
                        covers=(
                            COVERS_UNKNOWN if upstream_partial else COVERS_UPSTREAM_SECTION
                        ),
                    )
                )

            if "ColData" in raw:
                cells, count = cells_of(raw, row_id)
                cell_total += count
                row_total += 1
                rows.append(
                    FlatRow(
                        row_id=row_id,
                        parent_row_id=parent,
                        row_type=ROW_DATA,
                        depth=depth,
                        group=group,
                        cells=cells,
                    )
                )

            if unread_id is not None and not isinstance(summary_block, Mapping):
                list_unread(unread_id, row_id, depth + 1, _UNREAD_MEMBER_DETAIL)

        if any(key != "Row" and value not in (None, "", [], {}) for key, value in container.items()):
            # Rows held anywhere but "Row", a shape Intuit could add later,
            # would vanish with nothing to say so. Listed after the rows read
            # here.
            row_id = f"{parent}.{len(block)}" if parent else str(len(block))
            list_unread(row_id, parent, depth, _UNREAD_ROWS_DETAIL)

    walk_rows(payload.get("Rows"), None, 0)

    # Column-level problems are judged AFTER the rows are counted: a column
    # with no declared type is an anomaly only when there are rows to type.
    column_anomalies, disclosures = _column_anomalies(kept, report_name, row_count=row_total)
    anomalies = column_anomalies + anomalies
    anomalies.extend(_stated_total_anomalies(rows, anomalies, kept))

    client_truncated = False
    if max_rows is not None and len(rows) > max_rows:
        rows = rows[:max_rows]
        client_truncated = True

    return FlattenedReport(
        report_name=report_name,
        start_period=_header_text(header, "StartPeriod"),
        end_period=_header_text(header, "EndPeriod"),
        currency=currency,
        columns=kept,
        excluded_column_keys=sorted(set(excluded)),
        rows=rows,
        upstream_cell_count=cell_total,
        upstream_row_count=row_total,
        upstream_partial=upstream_partial,
        client_truncated=client_truncated,
        completeness=completeness_of(
            upstream_partial=upstream_partial,
            client_truncated=client_truncated,
            anomalous=bool(anomalies),
        ),
        anomalies=anomalies,
        disclosures=disclosures,
        report_basis=_header_text(header, "ReportBasis"),
        summarize_columns_by=_header_text(header, "SummarizeColumnsBy"),
        date_macro=_header_text(header, "DateMacro"),
        no_report_data=_no_report_data(header.get("Option")),
    )


def _no_report_data(options: Any) -> bool | None:
    """``Header.Option[Name=NoReportData]`` as a boolean, or ``None`` when the
    header does not say. Intuit's Run reports page documents the option as
    returned for every report type."""
    if not _is_list(options):
        return None
    for option in options:
        if isinstance(option, Mapping) and option.get("Name") == "NoReportData":
            value = option.get("Value")
            if value == "true":
                return True
            if value == "false":
                return False
    return None


def _column_anomalies(
    kept: Sequence[ReportColumn], report_name: str, *, row_count: int
) -> tuple[list[ReportAnomaly], list[str]]:
    """Published columns whose declared type cannot be read as intended.

    Only published columns: an excluded column publishes nothing, so its type
    changes nothing.

    An ABSENT type is judged by whether there were rows to type. With rows, it
    is an unrecognised type like any other: the cells are text, the result is
    partial. Without rows -- InventoryValuationDetail over an empty window
    declared nine columns with no ColType at all (2026-09-24) -- nothing was
    left untyped, so it is disclosed once and the result is not partial.
    """
    money_keys = MONEY_COLUMN_KEYS.get(report_name, frozenset())
    found: list[ReportAnomaly] = []
    untyped = 0
    for column in kept:
        if column.type == "" and row_count == 0:
            untyped += 1
        elif column.type not in RECOGNISED_COLUMN_TYPES:
            found.append(
                ReportAnomaly(None, column.key, ANOMALY_UNRECOGNISED_COLUMN_TYPE,
                              _UNRECOGNISED_TYPE_DETAIL)
            )
        elif (column.leaf_key or column.key) in money_keys and not column.is_money:
            found.append(
                ReportAnomaly(None, column.key, ANOMALY_UNRECOGNISED_COLUMN_TYPE,
                              _NON_MONEY_TYPE_DETAIL)
            )
    disclosures: list[str] = []
    if untyped:
        disclosures.append(
            f"{untyped} published column{'s' if untyped != 1 else ''} declared no type "
            f"and the report carried no rows to type, so nothing is affected; the "
            f"same report over a window with rows declares its types"
        )
    return found, disclosures


def _section_of(row_id: str) -> str:
    """``0.1.summary`` -> ``0.1``: the section a summary row totals."""
    return row_id.removesuffix(".summary")


def _stated_total_anomalies(
    rows: Sequence[FlatRow],
    anomalies: Sequence[ReportAnomaly],
    kept: Sequence[ReportColumn],
) -> list[ReportAnomaly]:
    """Flag every total QuickBooks states that the returned cells cannot reproduce.

    A section total is flagged for a money column when a cell in that column,
    or a whole row, inside the section is affected. A section with no rows of
    its own -- a computed line such as net income, whose inputs are elsewhere --
    is flagged whenever anything in the report is affected, because what it
    covers cannot be told from here. The stated figure itself is never changed.
    """
    money = {column.key for column in kept if column.is_money}
    affected_cells = {
        (a.row_id, a.column_key) for a in anomalies
        if a.row_id is not None and a.column_key is not None
    }
    affected_rows = {
        a.row_id for a in anomalies if a.row_id is not None and a.column_key is None
    }
    if not affected_cells and not affected_rows:
        return []

    found: list[ReportAnomaly] = []
    for row in rows:
        if row.row_type != ROW_SECTION_SUMMARY:
            continue
        section = _section_of(row.row_id)
        inside = [
            other.row_id for other in rows
            if other.row_id != row.row_id and other.row_id.startswith(section + ".")
        ]
        for cell in row.cells:
            if cell.column_key not in money or cell.text is None:
                continue
            if inside:
                hit = any(
                    (rid, cell.column_key) in affected_cells or rid in affected_rows
                    for rid in inside
                )
            else:
                hit = True
            if hit:
                found.append(
                    ReportAnomaly(row.row_id, cell.column_key, ANOMALY_STATED_TOTAL,
                                  _STATED_TOTAL_DETAIL)
                )
    return found


def completeness_of(
    *, upstream_partial: bool, client_truncated: bool, anomalous: bool = False
) -> str:
    """``complete`` / ``partial`` / ``unknown``, kept separate on purpose.

    Known loss comes first: a local trim or an anomaly makes a result
    ``partial`` whatever else is true -- rows were withheld, or some cell
    could not be read as sent -- which is what the tools say of their own
    results (tools_data.py, tools_reports.py). An upstream cap signal alone
    is ``unknown``: proximity to a cap never proves completeness, and it is
    not ``complete`` with a caveat in prose that a model will not read.
    """
    if client_truncated or anomalous:
        return "partial"
    if upstream_partial:
        return "unknown"
    return "complete"


# --------------------------------------------------------------------------
# Totals this server computes, and the sentence that leads the text
# --------------------------------------------------------------------------

COMPUTED: Final = "computed"
UNKNOWN: Final = "unknown"


@dataclass(frozen=True, slots=True)
class ComputedTotal:
    """A total this server worked out, or the reason it could not.

    Distinct from a total QuickBooks states, which is reported as stated. This
    is the one every report tool must use to add up a column: over an affected
    cell, a trimmed report or a capped one, it is ``unknown`` with the reason.
    """

    column_key: str
    amount: str | None
    status: str
    reason: str | None


def computed_total(report: FlattenedReport, column_key: str) -> ComputedTotal:
    """Sum ``column_key`` over the report's data rows -- never its totals."""

    def unknown(reason: str) -> ComputedTotal:
        return ComputedTotal(column_key, None, UNKNOWN, reason)

    if report.upstream_partial:
        return unknown("upstream signalled a cap, so the rows are not all of the data")
    if report.client_truncated:
        return unknown("rows were trimmed to the requested limit, so this would total only some")
    published = {column.key: column for column in report.columns}
    column = published.get(column_key)
    if column is None or not column.is_money:
        return unknown("the column is not a published monetary column")
    if column.is_percent:
        return unknown("the column is a percent column; percentages are not summed")
    for anomaly in report.anomalies:
        if anomaly.column_key == column_key and anomaly.row_id is None:
            return unknown("the column's declared type could not be read as monetary")

    data_rows = {row.row_id for row in report.rows if row.row_type == ROW_DATA}
    for anomaly in report.anomalies:
        if anomaly.row_id in data_rows and anomaly.column_key in (None, column_key):
            return unknown("a cell it covers could not be read, so any sum would be wrong")

    total = Decimal("0")
    for row in report.rows:
        if row.row_type != ROW_DATA:
            continue
        cell = next((c for c in row.cells if c.column_key == column_key), None)
        if cell is None or cell.amount is None:
            return unknown(
                "a cell it covers is empty; an empty cell is not a zero, so the sum "
                "is not known"
            )
        total += Decimal(cell.amount)
    return ComputedTotal(column_key, str(total), COMPUTED, None)


def disclosure_sentence(report: FlattenedReport, *, completeness: str) -> str:
    """The first sentence of the text a model reads.

    The structured fields say everything below; this says the one thing that
    must not be missed, first, because a model reads the text and may read
    nothing else. A disclosure that is not an anomaly follows the count, so an
    empty report with untyped columns says so without saying PARTIAL. A local
    trim is known loss, so the first sentence says how many rows ``max_rows``
    withheld, with anomalies or without.
    """
    rows = len(report.rows)
    # upstream_row_count counts every flattened row before the trim, as
    # len(rows) counts them after it.
    withheld = report.upstream_row_count - rows if report.client_truncated else 0
    if not report.anomalies:
        trimmed = f" and {withheld} withheld by max_rows" if withheld else ""
        sentence = (
            f"{report.report_name}: {rows} row{'s' if rows != 1 else ''} returned{trimmed}; "
            f"completeness {completeness}."
        )
        if report.disclosures:
            sentence += " Disclosed: " + " ".join(f"{d}." for d in report.disclosures)
        return sentence
    counts: dict[str, int] = {}
    for anomaly in report.anomalies:
        counts[anomaly.kind] = counts.get(anomaly.kind, 0) + 1
    breakdown = ", ".join(
        f"{count} {kind.replace('_', ' ')}" for kind, count in sorted(counts.items())
    )
    total = len(report.anomalies)
    trimmed = (
        f", and {withheld} row{'s' if withheld != 1 else ''} beyond max_rows "
        f"{'were' if withheld != 1 else 'was'} withheld" if withheld else ""
    )
    return (
        f"PARTIAL RESULT: this {report.report_name} report has {total} "
        f"anomal{'y' if total == 1 else 'ies'} ({breakdown}){trimmed}. Affected cells keep "
        f"their original text and carry no amount, a row with the wrong cell count has "
        "its values withheld, and every one is listed in "
        f"'anomalies'; do not total the returned cells without reading it."
    )


def _text_or_none(value: Any, where: str) -> str | None:
    """One header or row field as text, or ``None`` when it is absent.

    An object there is refused, never turned into text: its text would carry
    every field nested in it past the column and cell checks.
    """
    if value in (None, ""):
        return None
    if not is_scalar(value):
        raise _structural(f"{where} is not a single value")
    return str(value)


def _header_text(header: Mapping[str, Any], name: str) -> str | None:
    return _text_or_none(header.get(name), f"the report header's {name}")
