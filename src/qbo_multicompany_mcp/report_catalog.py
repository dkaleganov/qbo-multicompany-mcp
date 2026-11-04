"""The catalogue behind ``run_report``: one spec per documented report without
a named tool.

Each spec says what this server may SEND to that report, and it is narrower
than the parameters Intuit documents on purpose: every name here is in the
report's inventory row, and the inventory row is the send-side backstop
(``tools_reports.REPORT_PARAMETER_INVENTORY``) while the network policy holds
exactly this set per path. Names the inventory lists but no spec exposes --
``qzurl``, ``custom1-3``, ``shipvia``, ``term``, ``adjusted_gain_loss``,
``arpaid``, ``appaid``, ``bothamount``, ``printed``, ``name``, ``group_by``,
``sort_by``, every ``*moddate*`` and ``*createdate*`` window, ``duedate_macro``,
``svcdate_macro``, the service-date window, ``account_status`` -- are refused
at both layers. Each is either an unreviewed free-text filter (``name``), a
custom field, a paid/unpaid switch whose behaviour has not been observed, or a
second window that would sit beside the one the tool validates. Adding one
is a reviewed change to the spec, the policy and a test.

**TransactionListByCustomer takes no ``columns``**, though Intuit's page lists
them: in production Intuit faults on any value
(:data:`BY_CUSTOMER_COLUMNS_REFUSAL`). It is sent none, so QuickBooks answers
with the report's own default set, and a caller's are refused with that
reason before any request.

**Windows.** ``date`` reports take ``start_date``/``end_date`` or a
``date_macro``, mutually exclusive. A DETAIL member requires one of them and
carries the six-calendar-month limit, on explicit dates and on a resolved
macro alike; a summary report may omit both and gets QuickBooks's default.
``duedate`` reports (the two balance details) take
``start_duedate``/``end_duedate`` both or neither, at most six months, with
``report_date`` as the as-of date -- the aging details' reading. ``as_of``
reports take ``report_date`` or ``date_macro``, not both.

**Macros differ per report.** Twenty-two of Intuit's pages list the same
twenty-three values; TransactionListByCustomer and TransactionListByVendor
list thirty-three, adding the Calendar macros; AccountListDetail,
CustomerBalanceDetail and the two aging details list none. VendorBalanceDetail
lists one beside ``report_date`` and a due-date window, and it is deliberately
NOT exposed there: what a range macro means on an as-of-plus-due-date report
has not been observed, and the balance details are kept symmetrical with the
aging details.

**Detail members** are ProfitAndLossDetail, TransactionListWithSplits,
TransactionListByCustomer, TransactionListByVendor, JournalReport,
InventoryValuationDetail, CustomerBalanceDetail and VendorBalanceDetail.
AccountListDetail is a list of accounts with an optional window, not a
transaction detail, and is not a member -- this server's interpretation.

Every endpoint path is the one Intuit's page names; nine differ from the page
title (confirmed live, 2026-09-24).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .date_macros import ALL_DATE_MACROS, DATE_MACROS

WINDOW_DATE: Final = "date"
WINDOW_DUEDATE: Final = "duedate"
WINDOW_AS_OF: Final = "as_of"

_D23: Final = DATE_MACROS
_D33: Final = ALL_DATE_MACROS
_NONE: Final[tuple[str, ...]] = ()


@dataclass(frozen=True, slots=True)
class ReportSpec:
    """One report as ``run_report`` serves it."""

    #: The tool-facing name: the title of Intuit's page for the report.
    name: str
    #: The live path below ``reports/``, as the page names it.
    endpoint: str
    #: Parameter names this server may send; a subset of the report's
    #: inventory row, and exactly the network policy's set for the path.
    parameters: frozenset[str]
    #: The ``date_macro`` vocabulary the page lists for this report.
    date_macros: tuple[str, ...]
    #: Which window the report takes (see the module docstring).
    window: str
    #: A detail member: its window is required and at most six calendar
    #: months.
    detail: bool
    #: Sent as ``columns`` when the report takes columns and the caller asked
    #: for none: the default set observed in sandbox, in the observed order.
    default_columns: tuple[str, ...] = ()
    #: The column shape was never observed live and comes from the page.
    documentation_derived: bool = False
    #: Non-empty for a report whose page lists ``columns`` and which is sent
    #: none, because Intuit faults on them: the fixed text a caller's
    #: ``columns`` is refused with, before any request. ``columns`` is then
    #: in neither ``parameters`` nor the network policy's set for the path.
    columns_refusal: str = ""


#: Why TransactionListByCustomer is sent no ``columns``. OBSERVED 2026-09-28
#: in production: any ``columns`` value -- the default set, the same set with
#: ``nat_amount``, and single columns tried one at a time -- got HTTP 200
#: carrying SystemFault 10000, a NullPointerException on Intuit's side, and
#: the same report without ``columns`` answered cleanly. The sandbox companies
#: accept the same columns, and TransactionListByVendor takes its columns in
#: production. Sent none, the report returns its own default set, the same
#: seven columns on both sandbox companies (observed 2026-09-28). Fixed text;
#: it names no caller value.
BY_CUSTOMER_COLUMNS_REFUSAL: Final = (
    "'columns' is not accepted by TransactionListByCustomer: in production "
    "Intuit answers this report with an internal error, fault 10000, "
    "whenever any columns are requested, even a single one (observed 2026-09-28), "
    "and answers it without them. This server requests none, so the report "
    "returns its own default columns. No request was made."
)


def _spec(
    name: str,
    endpoint: str,
    parameters: set[str],
    *,
    date_macros: tuple[str, ...],
    window: str,
    detail: bool = False,
    default_columns: tuple[str, ...] = (),
    documentation_derived: bool = False,
    columns_refusal: str = "",
) -> ReportSpec:
    return ReportSpec(
        name=name, endpoint=endpoint, parameters=frozenset(parameters),
        date_macros=date_macros, window=window, detail=detail,
        default_columns=default_columns, documentation_derived=documentation_derived,
        columns_refusal=columns_refusal,
    )


CATALOG: Final[dict[str, ReportSpec]] = {
    spec.name: spec
    for spec in (
        _spec(
            "AccountListDetail", "AccountList",
            {"start_date", "end_date", "account_type", "sort_order", "columns"},
            date_macros=_NONE, window=WINDOW_DATE,
            # The default observed in sandbox.
            default_columns=("account_name", "account_type", "detail_acc_type", "account_desc", "account_bal"),
        ),
        _spec(
            "CashFlow", "CashFlow",
            {"start_date", "end_date", "date_macro", "summarize_column_by",
             "customer", "vendor", "class", "item", "department", "sort_order"},
            date_macros=_D23, window=WINDOW_DATE,
        ),
        _spec(
            "CustomerBalance", "CustomerBalance",
            {"customer", "accounting_method", "date_macro", "report_date", "sort_order",
             "summarize_column_by", "department"},
            date_macros=_D23, window=WINDOW_AS_OF,
        ),
        _spec(
            "CustomerBalanceDetail", "CustomerBalanceDetail",
            {"customer", "start_duedate", "end_duedate", "report_date", "sort_order",
             "aging_method", "department", "columns"},
            date_macros=_NONE, window=WINDOW_DUEDATE, detail=True,
            default_columns=("tx_date", "txn_type", "doc_num", "due_date", "subt_amount", "subt_open_bal", "rbal_open_bal"),
        ),
        _spec(
            "CustomerIncome", "CustomerIncome",
            {"customer", "accounting_method", "start_date", "end_date", "date_macro", "class",
             "sort_order", "summarize_column_by", "department", "vendor"},
            date_macros=_D23, window=WINDOW_DATE,
        ),
        _spec(
            "InventoryValuationDetail", "InventoryValuationDetail",
            {"start_date", "end_date", "date_macro", "columns"},
            date_macros=_D23, window=WINDOW_DATE, detail=True,
            default_columns=("tx_date", "txn_type", "doc_num", "name", "quantity", "rate",
                             "home_amount", "qty_on_hand", "home_asset_value"),
        ),
        _spec(
            "InventoryValuationSummary", "InventoryValuationSummary",
            {"date_macro", "item", "report_date", "sort_order", "summarize_column_by"},
            date_macros=_D23, window=WINDOW_AS_OF,
        ),
        _spec(
            "JournalReport", "JournalReport",
            {"start_date", "end_date", "date_macro", "sort_order", "columns"},
            date_macros=_D23, window=WINDOW_DATE, detail=True,
            default_columns=("tx_date", "txn_type", "doc_num", "name", "memo", "account_name", "debt_amt", "credit_amt"),
        ),
        _spec(
            "ProfitAndLossDetail", "ProfitAndLossDetail",
            {"customer", "account", "accounting_method", "start_date", "end_date", "date_macro", "class",
             "payment_method", "sort_order", "employee", "department", "vendor", "account_type", "columns"},
            date_macros=_D23, window=WINDOW_DATE, detail=True,
            default_columns=("tx_date", "txn_type", "doc_num", "name", "memo", "split_acc",
                             "subt_nat_amount", "rbal_nat_amount"),
        ),
        _spec(
            "SalesByClassSummary", "ClassSales",
            {"customer", "accounting_method", "start_date", "end_date", "date_macro", "class", "item",
             "summarize_column_by", "department"},
            date_macros=_D23, window=WINDOW_DATE,
        ),
        _spec(
            "SalesByCustomer", "CustomerSales",
            {"customer", "accounting_method", "start_date", "end_date", "date_macro", "class", "item",
             "sort_order", "summarize_column_by", "department"},
            date_macros=_D23, window=WINDOW_DATE,
        ),
        _spec(
            "SalesByDepartment", "DepartmentSales",
            {"customer", "accounting_method", "start_date", "end_date", "date_macro", "class", "item",
             "sort_order", "summarize_column_by", "department"},
            date_macros=_D23, window=WINDOW_DATE,
        ),
        _spec(
            "SalesByProduct", "ItemSales",
            {"customer", "accounting_method", "start_date", "end_date", "date_macro", "class", "item",
             "sort_order", "summarize_column_by", "department"},
            date_macros=_D23, window=WINDOW_DATE,
        ),
        _spec(
            "TaxSummary", "TaxSummary",
            {"agency_id", "accounting_method", "start_date", "end_date", "date_macro", "sort_order"},
            date_macros=_D23, window=WINDOW_DATE, documentation_derived=True,
        ),
        _spec(
            "TransactionListByCustomer", "TransactionListByCustomer",
            {"start_date", "end_date", "date_macro", "customer", "transaction_type", "source_account_type",
             "cleared", "docnum", "memo", "payment_method", "department", "sort_order"},
            date_macros=_D33, window=WINDOW_DATE, detail=True,
            columns_refusal=BY_CUSTOMER_COLUMNS_REFUSAL,
        ),
        _spec(
            "TransactionListByVendor", "TransactionListByVendor",
            {"start_date", "end_date", "date_macro", "vendor", "transaction_type", "source_account_type",
             "cleared", "docnum", "memo", "payment_method", "department", "sort_order", "columns"},
            date_macros=_D33, window=WINDOW_DATE, detail=True,
            default_columns=("tx_date", "txn_type", "doc_num", "is_no_post", "memo", "account_name", "nat_amount"),
        ),
        _spec(
            "TransactionListWithSplits", "TransactionListWithSplits",
            {"start_date", "end_date", "date_macro", "docnum", "payment_method", "source_account_type",
             "transaction_type", "sort_order", "columns"},
            date_macros=_D23, window=WINDOW_DATE, detail=True,
            default_columns=("tx_date", "txn_type", "doc_num", "is_no_post", "name", "memo", "account_name", "nat_amount"),
        ),
        _spec(
            "TrialBalance", "TrialBalance",
            {"accounting_method", "start_date", "end_date", "date_macro", "sort_order", "summarize_column_by"},
            date_macros=_D23, window=WINDOW_DATE,
        ),
        _spec(
            "VendorBalance", "VendorBalance",
            {"accounting_method", "date_macro", "report_date", "sort_order", "summarize_column_by",
             "department", "vendor"},
            date_macros=_D23, window=WINDOW_AS_OF,
        ),
        _spec(
            "VendorBalanceDetail", "VendorBalanceDetail",
            {"start_duedate", "end_duedate", "accounting_method", "report_date", "sort_order",
             "department", "vendor", "columns"},
            date_macros=_NONE, window=WINDOW_DUEDATE, detail=True,
            default_columns=("tx_date", "txn_type", "doc_num", "due_date", "subt_neg_amount",
                             "subt_neg_open_bal", "rbal_neg_open_bal"),
        ),
        _spec(
            "VendorExpenses", "VendorExpenses",
            {"customer", "vendor", "start_date", "end_date", "date_macro", "class", "sort_order",
             "summarize_column_by", "department", "accounting_method"},
            date_macros=_D23, window=WINDOW_DATE,
        ),
    )
}

#: The tool-facing enum, in catalogue order.
REPORT_NAMES: Final[tuple[str, ...]] = tuple(CATALOG)

#: Reports a NAMED tool serves, and which one: refused here with a pointer,
#: because run_report's enum is disjoint from the named tools.
NAMED_TOOL_REPORTS: Final[dict[str, str]] = {
    "ProfitAndLoss": "report_profit_and_loss",
    "BalanceSheet": "report_balance_sheet",
    "GeneralLedger": "report_general_ledger",
    "APAgingSummary": "report_aging",
    "ARAgingSummary": "report_aging",
    "APAgingDetail": "report_aging",
    "ARAgingDetail": "report_aging",
    "TransactionList": "list_transactions",
}
