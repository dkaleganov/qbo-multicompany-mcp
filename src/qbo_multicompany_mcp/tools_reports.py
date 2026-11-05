"""The four named report tools.

Everything a report tool sends is checked here before its GET, in this
order: the entity resolves; every parameter the input alone decides is
validated against the parameters Intuit documents for its report and this
server's rules (date_macro and explicit dates are mutually exclusive, ``Days``
over 200 days and ``columns`` lists of 25 or more are refused, and explicit
dates on a detail report must fit the six-calendar-month window); then the
guard and the company's capabilities are established (once per entity per
process, which is where the guard's own reads happen), a ``date_macro`` is
resolved against the company's calendar and its window checked,
``accounting_method`` defaults to the company's ``ReportBasis``, and a filter
that needs a capability the company lacks is refused with a fixed error rather
than silently dropped. Only then is one GET sent, and the network policy
checks the same parameter set again per path.

``report_aging`` is one tool over FOUR endpoints with FOUR allowlists, because
the documented parameters differ materially: AgedPayableDetail takes
``accounting_method`` and no ``aging_method``, AgedReceivableDetail the
reverse, and the summaries take neither the due-date window nor ``columns``.
A combination the endpoint does not accept is refused before any request.

``include_ids`` is not a parameter anywhere because routing and structural ids
are kept unconditionally. The balance sheet takes ``as_of`` or ``date_macro``,
one required.

**Date macros are resolved before they are checked.** A macro is turned into
a window by :mod:`qbo_multicompany_mcp.date_macros` -- in the company's book
calendar, as of the operator's local date -- and that window is what the
six-month rule and the 200-day ``Days`` rule read. The request still sends the
macro; the resolution is disclosed on the result as
``resolved_start``/``resolved_end`` with its basis, and QuickBooks's own
``start_period``/``end_period`` remain the authority on what was reported.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Annotated, Any, Final

from mcp.server import MCPServer
from mcp_types import CallToolResult
from pydantic import Field

from . import date_macros as date_macros_mod
from . import reports as reports_mod
from .capabilities import ACCOUNTS_PAYABLE_FEATURE_FAULT, AP_UNAVAILABLE, Capabilities
from .config import Settings
from .envelopes import (
    ReportAnomalyBlock,
    ReportCellBlock,
    ReportColumnBlock,
    ReportEnvelope,
    ReportRowBlock,
)
from .errors import CapabilityUnavailable, UpstreamError
from .query_grammar import QueryError
from .registry import Entity, Registry
from .session import QboSession
from .tools import (
    FREE_TEXT_DISCLOSURE,
    EntityKey,
    IdList,
    IsoDate,
    enum_of,
    joined_ids,
    make_result,
    read_only,
    tool_errors,
)
from .tools import check_enum as _check_enum
from .tools_data import MAX_WINDOW_MONTHS, _add_months, _identity, _iso_date, window_refusal_text

#: ``max_rows`` on every report tool: default 500, max 5,000.
REPORT_DEFAULT_ROWS: Final = 500
REPORT_MAX_ROWS: Final = 5000

#: ``columns`` lists of 25 or more are refused. The limit comes from Intuit's
#: developer blog: a February 2026 post on Reports API best practices warns
#: that 25 or more columns in one request raises the risk of a 504 timeout.
#: Intuit's report pages do not carry the figure.
MAX_COLUMNS: Final = 25

#: A ``Days`` window over 200 days is refused before any request. Intuit's
#: developer blog (a March 2026 post on Reports API changes) says Days over a
#: year returns 200 columns and puts the rest in "Others"; Intuit's report pages
#: don't say so, and refusing past 200 days is this server's own rule.
MAX_DAYS_WINDOW: Final = 200

#: The 23 documented ``date_macro`` values, the same list on Intuit's
#: ProfitAndLoss, BalanceSheet, GeneralLedger and APAgingSummary pages. The
#: vocabulary and the resolver live in :mod:`qbo_multicompany_mcp.date_macros`.
DATE_MACROS: Final[tuple[str, ...]] = date_macros_mod.DATE_MACROS

#: "Today" for macro resolution: the operator's local date in the server
#: process. An ASSUMPTION about which calendar day the operator means, and
#: labelled as such on every result that carries a resolution; no timezone is
#: inferred beyond it. Indirection so the tests can pin a date.
_today: Callable[[], date] = date.today

#: The 12 documented ``summarize_column_by`` values plus ``FiscalYear``. The
#: period groupings (Days, Week, Month, Quarter, Year, FiscalYear) return Money
#: columns keyed by period label with StartDate/EndDate metadata, which the
#: parser admits by shape; the entity groupings (Customers, Vendors, Classes,
#: Departments, Employees, ProductsAndServices) return Money columns keyed by
#: ENTITY ID, observed in sandbox 2026-09-25 and admitted by that shape under
#: the request that named them (``reports.ENTITY_GROUPINGS``); the grouping is
#: passed to the parser.
SUMMARIZE_COLUMN_BY: Final[tuple[str, ...]] = (
    "Total", "Month", "Week", "Days", "Quarter", "Year",
    "Customers", "Vendors", "Classes", "Departments", "Employees", "ProductsAndServices",
    "FiscalYear",
)
#: The groupings whose window can be checked against the 200-day rule.
_DAYS: Final = "Days"

#: The accounting methods Intuit documents for ProfitAndLoss.
ACCOUNTING_METHODS: Final[tuple[str, ...]] = ("Cash", "Accrual")
#: The aging methods Intuit documents for ARAgingDetail and APAgingSummary.
AGING_METHODS: Final[tuple[str, ...]] = ("Report_Date", "Current")
#: The sort orders Intuit documents for ProfitAndLoss.
SORT_ORDERS: Final[tuple[str, ...]] = ("ascend", "descend")

#: GeneralLedger ``account_type``: the AccountType enum with the spaces
#: removed, plus NonPosting. The same spacing trap as TransactionList's
#: source_account_type: the chart of accounts reports the spaced form.
GL_ACCOUNT_TYPES: Final[tuple[str, ...]] = (
    "AccountsPayable", "AccountsReceivable", "Bank", "CostOfGoodsSold", "CreditCard",
    "Equity", "Expense", "FixedAsset", "Income", "LongTermLiability", "NonPosting",
    "OtherAsset", "OtherCurrentAsset", "OtherCurrentLiability", "OtherExpense", "OtherIncome",
)

#: GeneralLedger's default column set, in the order QuickBooks returned it
#: (observed in sandbox 2026-09-24). Sent explicitly so the request is
#: self-describing and the allowlist governs the request too.
GENERAL_LEDGER_DEFAULT_COLUMNS: Final[tuple[str, ...]] = (
    "tx_date", "txn_type", "doc_num", "name", "memo", "split_acc",
    "subt_nat_amount", "rbal_nat_amount",
)
#: Always requested on a detail report, whatever the caller asked for: the
#: transaction type carries the routing id.
ROUTING_COLUMNS: Final[tuple[str, ...]] = ("tx_date", "txn_type")

#: Every parameter Intuit documents for each endpoint; the four aging reports
#: differ. This is the send-side backstop: a parameter missing from its
#: report's entry is refused before any request, whatever produced it. The
#: network policy holds the narrower set each tool actually exposes.
REPORT_PARAMETER_INVENTORY: Final[dict[str, frozenset[str]]] = {
    "ProfitAndLoss": frozenset(
        {"customer", "qzurl", "accounting_method", "end_date", "date_macro", "adjusted_gain_loss",
         "class", "item", "sort_order", "summarize_column_by", "department", "vendor", "start_date"}
    ),
    "BalanceSheet": frozenset(
        {"customer", "qzurl", "accounting_method", "end_date", "date_macro", "adjusted_gain_loss",
         "class", "item", "sort_order", "summarize_column_by", "department", "vendor", "start_date"}
    ),
    "GeneralLedger": frozenset(
        {"customer", "account", "accounting_method", "source_account", "end_date", "date_macro",
         "account_type", "sort_by", "sort_order", "start_date", "summarize_column_by", "department",
         "vendor", "class", "columns"}
    ),
    "AgedPayables": frozenset(
        {"customer", "qzurl", "vendor", "date_macro", "department", "report_date", "sort_order", "aging_method"}
    ),
    "AgedReceivables": frozenset(
        {"customer", "qzurl", "date_macro", "aging_method", "report_date", "sort_order", "department"}
    ),
    "AgedPayableDetail": frozenset(
        {"shipvia", "term", "end_duedate", "accounting_method", "start_duedate", "custom1", "custom2",
         "custom3", "report_date", "num_periods", "vendor", "past_due", "aging_period", "columns"}
    ),
    "AgedReceivableDetail": frozenset(
        {"customer", "shipvia", "term", "end_duedate", "start_duedate", "custom1", "custom2", "custom3",
         "report_date", "num_periods", "aging_method", "past_due", "aging_period", "columns"}
    ),
    # run_report's reports, each with its documented parameters, keyed by the
    # live path.
    "AccountList": frozenset(
        {"account_type", "end_date", "start_moddate", "sort_by", "sort_order", "moddate_macro", "end_moddate",
         "account_status", "createdate_macro", "start_date", "columns"}
    ),
    "CashFlow": frozenset(
        {"customer", "vendor", "end_date", "date_macro", "class", "item", "sort_order", "summarize_column_by",
         "department", "start_date"}
    ),
    "CustomerBalance": frozenset(
        {"customer", "accounting_method", "date_macro", "arpaid", "report_date", "sort_order",
         "summarize_column_by", "department"}
    ),
    "CustomerBalanceDetail": frozenset(
        {"customer", "shipvia", "term", "end_duedate", "start_duedate", "custom1", "sort_by", "arpaid",
         "report_date", "sort_order", "aging_method", "department", "columns"}
    ),
    "CustomerIncome": frozenset(
        {"customer", "term", "accounting_method", "end_date", "date_macro", "class", "sort_order",
         "summarize_column_by", "department", "vendor", "start_date"}
    ),
    "InventoryValuationDetail": frozenset(
        {"end_date", "end_svcdate", "date_macro", "svcdate_macro", "start_svcdate", "group_by", "start_date",
         "columns"}
    ),
    "InventoryValuationSummary": frozenset(
        {"qzurl", "date_macro", "item", "report_date", "sort_order", "summarize_column_by"}
    ),
    "JournalReport": frozenset(
        {"end_date", "date_macro", "sort_by", "sort_order", "start_date", "columns"}
    ),
    "ProfitAndLossDetail": frozenset(
        {"customer", "account", "accounting_method", "end_date", "date_macro", "adjusted_gain_loss", "class",
         "sort_by", "payment_method", "sort_order", "employee", "department", "vendor", "account_type",
         "start_date", "columns"}
    ),
    "ClassSales": frozenset(
        {"customer", "accounting_method", "end_date", "date_macro", "class", "item", "summarize_column_by",
         "department", "start_date"}
    ),
    "CustomerSales": frozenset(
        {"customer", "qzurl", "accounting_method", "end_date", "date_macro", "class", "item", "sort_order",
         "summarize_column_by", "department", "start_date"}
    ),
    "DepartmentSales": frozenset(
        {"customer", "accounting_method", "end_date", "date_macro", "class", "item", "sort_order",
         "summarize_column_by", "department", "start_date"}
    ),
    "ItemSales": frozenset(
        {"customer", "end_duedate", "accounting_method", "end_date", "date_macro", "start_duedate", "class",
         "item", "sort_order", "summarize_column_by", "department", "start_date"}
    ),
    "TaxSummary": frozenset(
        {"agency_id", "accounting_method", "end_date", "date_macro", "sort_order", "start_date"}
    ),
    "TransactionListByCustomer": frozenset(
        {"date_macro", "payment_method", "duedate_macro", "arpaid", "bothamount", "transaction_type", "docnum",
         "start_moddate", "source_account_type", "group_by", "start_date", "department", "start_duedate",
         "columns", "end_duedate", "end_date", "memo", "appaid", "moddate_macro", "printed", "createdate_macro",
         "cleared", "customer", "qzurl", "term", "end_createdate", "name", "sort_by", "sort_order",
         "start_createdate", "end_moddate"}
    ),
    "TransactionListByVendor": frozenset(
        {"date_macro", "payment_method", "duedate_macro", "arpaid", "bothamount", "transaction_type", "docnum",
         "start_moddate", "source_account_type", "group_by", "start_date", "department", "start_duedate",
         "columns", "end_duedate", "vendor", "end_date", "memo", "appaid", "moddate_macro", "printed",
         "createdate_macro", "cleared", "qzurl", "term", "end_createdate", "name", "sort_by", "sort_order",
         "start_createdate", "end_moddate"}
    ),
    "TransactionListWithSplits": frozenset(
        {"docnum", "name", "end_date", "date_macro", "payment_method", "source_account_type", "transaction_type",
         "group_by", "sort_by", "sort_order", "start_date", "columns"}
    ),
    "TrialBalance": frozenset(
        {"accounting_method", "end_date", "date_macro", "sort_order", "summarize_column_by", "start_date"}
    ),
    "VendorBalance": frozenset(
        {"qzurl", "accounting_method", "date_macro", "appaid", "report_date", "sort_order",
         "summarize_column_by", "department", "vendor"}
    ),
    "VendorBalanceDetail": frozenset(
        {"term", "end_duedate", "accounting_method", "date_macro", "start_duedate", "duedate_macro", "sort_by",
         "report_date", "sort_order", "appaid", "department", "vendor", "columns"}
    ),
    "VendorExpenses": frozenset(
        {"customer", "vendor", "end_date", "date_macro", "class", "sort_order", "summarize_column_by",
         "department", "accounting_method", "start_date"}
    ),
}

#: The published enums: through json_schema_extra, never a Literal the SDK
#: would refuse by quoting the value. Each tool refuses a value outside one
#: with ``_check_enum`` before any request.
DateMacro = enum_of(DATE_MACROS)
SummarizeBy = enum_of(SUMMARIZE_COLUMN_BY)
AccountingMethod = enum_of(ACCOUNTING_METHODS)
AgingMethod = enum_of(AGING_METHODS)
SortOrder = enum_of(SORT_ORDERS)
GlAccountType = enum_of(GL_ACCOUNT_TYPES)
AgingSide = enum_of(("payables", "receivables"))
AgingDetail = enum_of(("summary", "detail"))

REPORT_NOTE: Final = (
    "These reports publish no row total and no documented cap signal, so "
    "completeness is 'unknown' unless something in this result establishes "
    "otherwise (an anomaly or a local trim make it 'partial'). Section totals "
    "are QuickBooks's stated figures, reported as stated; 'scope' names what "
    "each total covers. An empty rows array means this response carried no "
    "rows, never that the company has none. parameters_sent lists what this "
    "server sent; only some parameters are echoed by the report."
)

_EXCLUSIVE_TEXT: Final = (
    "date_macro and explicit dates are mutually exclusive: give a macro or a "
    "date range, not both."
)

_BS_ONE_OF_TEXT: Final = (
    "report_balance_sheet takes as_of or date_macro, one of them: as_of is the "
    "balance sheet date, date_macro a documented range whose end is that date."
)


# --------------------------------------------------------------------------
# Validation helpers. Every refusal is fixed text that never repeats the
# caller's value; names are this package's own parameter names.
# --------------------------------------------------------------------------


def _check_window(start: date, end: date, *, start_name: str, end_name: str, months: int | None) -> None:
    if end < start:
        raise QueryError(f"{end_name} is before {start_name}")
    if months is not None and end >= _add_months(start, months):
        raise QueryError(window_refusal_text(start_name, end_name, months))


def _check_days_window(summarize: str | None, start: date | None, end: date | None, *, end_name: str) -> None:
    """A Days window over 200 days is refused before any request.

    The window is either the explicit dates or a macro RESOLVED by
    :func:`_resolve_macro` and validated the same way. With neither there is
    nothing to check, and the request is refused rather than sent to a default
    window this server never saw.
    """
    if summarize != _DAYS:
        return
    if start is None or end is None:
        raise QueryError(
            f"summarize_column_by=Days needs a window to check against the Days "
            f"limit: give start_date and {end_name}, or a date_macro, which is "
            f"resolved here first."
        )
    if (end - start).days + 1 > MAX_DAYS_WINDOW:
        raise QueryError(
            f"summarize_column_by=Days over a window longer than {MAX_DAYS_WINDOW} days is "
            f"refused before any request: QuickBooks folds the excess columns into "
            f"'Others'. Narrow the window or summarise by Week or Month."
        )


def _columns_to_request(columns: Sequence[str] | None, report_name: str, default: Sequence[str]) -> list[str]:
    """The allowlist applied to the REQUEST: an unknown or withheld key is
    refused by position (never by value), 25 or more are refused, and the
    routing columns are included wherever the report's own vocabulary has
    them.

    The count is taken on the request as well as on the list: Intuit's 504
    warning is about the columns a request carries, and 23 keys without the
    routing columns become 25 once they are added.

    Routing keys are per endpoint, and the list that would go out -- a
    default one too -- is checked against the report's own vocabulary before
    any request: AccountListDetail documents neither tx_date nor txn_type, so
    adding them to every custom list would send columns outside its
    contract."""
    allowed = reports_mod.allowed_column_keys(report_name)
    if not columns:
        requested = list(default)
    else:
        if len(columns) >= MAX_COLUMNS:
            raise QueryError(
                f"columns lists of {MAX_COLUMNS} or more are refused before any request "
                f"(Intuit warns that so many columns risk a 504 timeout). {len(columns)} were given."
            )
        routing = tuple(key for key in ROUTING_COLUMNS if key in allowed)
        requested = list(dict.fromkeys([*columns, *routing]))
        if len(requested) >= MAX_COLUMNS:
            raise QueryError(
                f"with {' and '.join(routing)}, which are always requested, this "
                f"request would carry {len(requested)} columns, and requests of "
                f"{MAX_COLUMNS} or more are refused before any request (Intuit warns "
                f"that so many columns risk a 504 timeout). Ask for fewer columns."
            )
        unknown = [f"columns[{i}]" for i, key in enumerate(columns) if key not in allowed]
        if unknown:
            raise QueryError(
                f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not available "
                f"for this report. Available column keys: {', '.join(sorted(allowed))}"
            )
    # Every caller key has passed the allowlist above, so a key outside it
    # here is one this package added, and naming it names no caller value.
    outside = [key for key in requested if key not in allowed]
    if outside:
        raise QueryError(
            f"this request would carry {', '.join(outside)}, outside {report_name}'s own "
            f"column vocabulary, so it is refused before any request."
        )
    return requested


def _require_tracking(
    capabilities: Capabilities | None, entity_key: str, *, class_: str | None, department: str | None
) -> list[str]:
    """A class or department filter needs the company to track them. Without
    the capability the filter is refused with a fixed error, never dropped:
    QuickBooks ignores a filter it does not understand and would return the
    whole company under the filter's name."""
    checked: list[str] = []
    if class_ is not None:
        _capabilities(capabilities, entity_key).require("class_tracking", entity_key)
        checked.append("class_tracking")
    if department is not None:
        _capabilities(capabilities, entity_key).require("department_tracking", entity_key)
        checked.append("department_tracking")
    return checked


def _capabilities(capabilities: Capabilities | None, entity_key: str) -> Capabilities:
    if capabilities is None:  # pragma: no cover - ensure_ready fails closed first
        raise CapabilityUnavailable(
            f"the capabilities of entity '{entity_key}' are not established, so a "
            f"capability-gated filter cannot be checked. No request was made."
        )
    return capabilities


def _accounting_method(
    given: str | None, capabilities: Capabilities | None, *, accepted: bool
) -> tuple[str | None, str | None]:
    """What to send, and where it came from: the default is the company's
    ReportBasis. Nothing is sent to a report whose documented parameters have
    no accounting_method, whatever the company's basis."""
    if not accepted:
        return None, None
    if given is not None:
        return given, "caller"
    basis = capabilities.accounting_basis if capabilities is not None else None
    if basis in ACCOUNTING_METHODS:
        return basis, "company_report_basis"
    return None, None


def _resolve_macro(
    date_macro: str | None, capabilities: Capabilities | None, entity_key: str
) -> date_macros_mod.ResolvedWindow | None:
    """Resolve a macro in the company's book calendar, or refuse.

    The fiscal year start comes from the entity's own Preferences, so this
    runs after ``ensure_ready``. A macro that cannot be resolved cannot be
    validated like explicit dates, and is refused with fixed text rather
    than sent unchecked or resolved against a January nobody stated.
    """
    if date_macro is None:
        return None
    fiscal = capabilities.fiscal_year_start_month if capabilities is not None else None
    try:
        return date_macros_mod.resolve(date_macro, today=_today(), fiscal_year_start_month=fiscal)
    except date_macros_mod.UnresolvableMacro as exc:
        raise QueryError(f"For entity '{entity_key}': {exc}") from None


def _bounds(
    start: date | None, end: date | None, window: date_macros_mod.ResolvedWindow | None
) -> tuple[date | None, date | None]:
    """The window the policy checks read: explicit dates, or the resolution."""
    if window is None:
        return start, end
    return window.start, window.end


def _inventory_check(endpoint: str, params: Mapping[str, str]) -> None:
    """The send-side backstop: every parameter is in this report's documented
    list. The schema and the combination rules make this unreachable from a
    call; it is here so a future edit cannot send an unlisted parameter by
    accident."""
    inventory = REPORT_PARAMETER_INVENTORY[endpoint]
    foreign = sorted(key for key in params if key not in inventory)
    if foreign:
        raise QueryError(
            f"{', '.join(foreign)} {'is' if len(foreign) == 1 else 'are'} not in the "
            f"documented parameter list for {endpoint}, so this request is refused "
            f"before it is sent."
        )


def _ap_unavailable_text(entity_key: str, fault_code: str | None) -> str:
    return (
        f"QuickBooks refused the accounts payable feature for entity '{entity_key}' "
        f"with fault {fault_code or ACCOUNTS_PAYABLE_FEATURE_FAULT} (feature not "
        f"supported), so report_aging(payables) cannot answer: the company's plan has "
        f"no accounts payable. This is not an empty report. Recorded for this process "
        f"and asked again after a restart or a re-consent. No request was made."
    )


# --------------------------------------------------------------------------
# The shared request and envelope
# --------------------------------------------------------------------------


def _run(
    session: QboSession,
    target: Entity,
    *,
    tool: str,
    endpoint: str,
    params: dict[str, str],
    max_rows: int | None,
    accounting: tuple[str | None, str | None],
    capabilities_checked: Sequence[str],
    side: str | None = None,
    detail: str | None = None,
    accounts_payable: str | None = None,
    window: date_macros_mod.ResolvedWindow | None = None,
    entity_grouping: str | None = None,
    report: str | None = None,
    extra_disclosures: Sequence[str] = (),
) -> CallToolResult:
    _inventory_check(endpoint, params)
    client = session.client()
    result = client.report(target, endpoint, params)
    flat = reports_mod.flatten(
        result.payload, report_name=endpoint, upstream_partial=False, max_rows=max_rows,
        entity_grouping=entity_grouping,
    )
    # Not 'complete': nothing in the response establishes it. An anomaly or a
    # local trim makes it known-partial, the stronger statement.
    completeness = "partial" if (flat.anomalies or flat.client_truncated) else "unknown"
    sent_method, source = accounting
    if extra_disclosures:
        flat.disclosures = [*flat.disclosures, *extra_disclosures]
    return make_result(
        ReportEnvelope(
            **_identity(target, result.request_started_at, result.fetched_at),
            summary=reports_mod.disclosure_sentence(flat, completeness=completeness),
            tool=tool,
            report=report,
            report_name=endpoint,
            side=side,
            detail=detail,
            start_period=flat.start_period,
            end_period=flat.end_period,
            currency=flat.currency,
            report_basis=flat.report_basis,
            summarize_columns_by=flat.summarize_columns_by,
            date_macro=flat.date_macro,
            date_macro_sent=params.get("date_macro"),
            resolved_start=window.start.isoformat() if window else None,
            resolved_end=window.end.isoformat() if window else None,
            resolution_basis=window.basis() if window else None,
            columns=[
                ReportColumnBlock(
                    index=c.index, key=c.key, title=c.title, type=c.type, leaf_key=c.leaf_key,
                    parent_title=c.parent_title, period_start=c.period_start,
                    period_end=c.period_end, synthetic_key=c.synthetic_key,
                    entity_type=c.entity_type, entity_id=c.entity_id, entity_total=c.entity_total,
                    admitted_by=c.admitted_by,
                )
                for c in flat.columns
            ],
            excluded_column_keys=flat.excluded_column_keys,
            rows=[
                ReportRowBlock(
                    **{**row.to_json(), "cells": [ReportCellBlock(**cell.to_json()) for cell in row.cells]}
                )
                for row in flat.rows
            ],
            count=len(flat.rows),
            upstream_row_count=flat.upstream_row_count,
            upstream_cell_count=flat.upstream_cell_count,
            upstream_partial=flat.upstream_partial,
            upstream_no_report_data=flat.no_report_data,
            client_truncated=flat.client_truncated,
            completeness=completeness,
            anomalies=[ReportAnomalyBlock(**a.to_json()) for a in flat.anomalies],
            disclosures=flat.disclosures,
            parameters_sent=sorted(params),
            accounting_method_sent=sent_method,
            accounting_method_source=source,
            capabilities_checked=list(capabilities_checked),
            accounts_payable=accounts_payable,
            note=REPORT_NOTE,
        )
    )


def _put(params: dict[str, str], **values: Any) -> None:
    for name, value in values.items():
        if value is not None and value != "":
            params[name] = str(value)


MaxRows = Annotated[
    int | None,
    Field(ge=1, le=REPORT_MAX_ROWS, description="Rows returned after flattening; the rest are trimmed locally and disclosed."),
]


def register_report_tools(
    server: MCPServer, settings: Settings, registry: Registry, session: QboSession
) -> None:
    # -------------------------------------------- report_profit_and_loss ---
    @server.tool(
        name="report_profit_and_loss",
        title="Profit and loss for a period",
        annotations=read_only("Profit and loss for a period"),
        description=(
            "Read one company's ProfitAndLoss report for a date range or a date "
            "macro, flattened with its section tree kept: every header, child and "
            "total keeps its row_id and parent_row_id, and each total names its "
            "scope. Use this for income and expense totals; use report_balance_sheet "
            "for balances as of a date and report_general_ledger for the postings "
            "behind an account. accounting_method defaults to the company's report "
            "basis. class and department filters require the company to track them "
            "and are refused otherwise, never dropped. Read-only." + FREE_TEXT_DISCLOSURE
        ),
    )
    def report_profit_and_loss(
        entity: EntityKey,
        start_date: Annotated[IsoDate | None, Field(description="YYYY-MM-DD, inclusive. Not with date_macro.")] = None,
        end_date: Annotated[IsoDate | None, Field(description="YYYY-MM-DD, inclusive. Not with date_macro.")] = None,
        date_macro: Annotated[DateMacro | None, Field(description="A documented QuickBooks date range. Not with start_date or end_date.")] = None,
        accounting_method: Annotated[AccountingMethod | None, Field(description="Defaults to the company's ReportBasis.")] = None,
        summarize_column_by: Annotated[SummarizeBy | None, Field(description="Days needs a window of at most 200 days: explicit dates or a macro, which is resolved first. An entity grouping returns one column per entity keyed by its id.")] = None,
        # Id filters are lists: Intuit documents each as one or more comma
        # separated ids.
        customer: Annotated[IdList | None, Field(description="Customer ids; sent as one comma-separated value.")] = None,
        vendor: Annotated[IdList | None, Field(description="Vendor ids; sent as one comma-separated value.")] = None,
        class_: Annotated[IdList | None, Field(validation_alias="class", description="Class ids; requires class tracking.")] = None,
        department: Annotated[IdList | None, Field(description="Department ids; requires department tracking.")] = None,
        max_rows: MaxRows = REPORT_DEFAULT_ROWS,
    ) -> Annotated[CallToolResult, ReportEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            if date_macro is not None and (start_date is not None or end_date is not None):
                raise QueryError(_EXCLUSIVE_TEXT)
            _check_enum(date_macro, DATE_MACROS, "date_macro")
            _check_enum(accounting_method, ACCOUNTING_METHODS, "accounting_method")
            _check_enum(summarize_column_by, SUMMARIZE_COLUMN_BY, "summarize_column_by")
            start = _iso_date(start_date, "start_date") if start_date else None
            end = _iso_date(end_date, "end_date") if end_date else None
            if start and end:
                _check_window(start, end, start_name="start_date", end_name="end_date", months=None)
            if date_macro is None:
                # Explicit dates are checked before the guard; a macro is
                # resolved after it, because the fiscal calendar is the
                # company's (below).
                _check_days_window(summarize_column_by, start, end, end_name="end_date")

            ready = session.ensure_ready(target)
            window = _resolve_macro(date_macro, ready.state.capabilities, target.key)
            check_start, check_end = _bounds(start, end, window)
            _check_days_window(summarize_column_by, check_start, check_end, end_name="end_date")
            checked = _require_tracking(
                ready.state.capabilities, target.key, class_=class_, department=department
            )
            accounting = _accounting_method(accounting_method, ready.state.capabilities, accepted=True)

            params: dict[str, str] = {}
            _put(
                params,
                start_date=start.isoformat() if start else None,
                end_date=end.isoformat() if end else None,
                date_macro=date_macro,
                accounting_method=accounting[0],
                summarize_column_by=summarize_column_by,
                customer=joined_ids(customer), vendor=joined_ids(vendor),
                department=joined_ids(department),
            )
            _put(params, **{"class": joined_ids(class_)})
            return _run(
                session, target, tool="report_profit_and_loss", endpoint="ProfitAndLoss",
                params=params, max_rows=max_rows, accounting=accounting, capabilities_checked=checked,
                window=window, entity_grouping=summarize_column_by,
            )

    # ---------------------------------------------- report_balance_sheet ---
    @server.tool(
        name="report_balance_sheet",
        title="Balance sheet as of a date",
        annotations=read_only("Balance sheet as of a date"),
        description=(
            "Read one company's BalanceSheet as of a date, flattened with its "
            "section tree kept to every depth; each total names its scope. Use this "
            "for balances as of a date; use report_profit_and_loss for a period and "
            "list_accounts for account ids. Give as_of (the balance sheet date, sent "
            "as end_date) or date_macro (a documented range whose end is that date), "
            "one of them; start_date is optional with as_of. accounting_method "
            "defaults to the company's report basis. class and department filters "
            "require the company to track them and are refused otherwise. Read-only." + FREE_TEXT_DISCLOSURE
        ),
    )
    def report_balance_sheet(
        entity: EntityKey,
        as_of: Annotated[IsoDate | None, Field(description="YYYY-MM-DD: the balance sheet date (sent as end_date). Not with date_macro.")] = None,
        date_macro: Annotated[DateMacro | None, Field(description="A documented QuickBooks date range; its end is the balance sheet date. Not with as_of or start_date.")] = None,
        start_date: Annotated[IsoDate | None, Field(description="YYYY-MM-DD, optional with as_of; needed with summarize_column_by=Days unless a macro gives the window.")] = None,
        accounting_method: Annotated[AccountingMethod | None, Field(description="Defaults to the company's ReportBasis.")] = None,
        summarize_column_by: Annotated[SummarizeBy | None, Field(description="Days needs a window of at most 200 days: start_date with as_of, or a macro, which is resolved first. An entity grouping returns one column per entity keyed by its id.")] = None,
        # Id filters are lists: Intuit documents each as one or more comma
        # separated ids.
        class_: Annotated[IdList | None, Field(validation_alias="class", description="Class ids; requires class tracking.")] = None,
        department: Annotated[IdList | None, Field(description="Department ids; requires department tracking.")] = None,
        max_rows: MaxRows = REPORT_DEFAULT_ROWS,
    ) -> Annotated[CallToolResult, ReportEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            if (as_of is None) == (date_macro is None):
                raise QueryError(_BS_ONE_OF_TEXT if as_of is None else _EXCLUSIVE_TEXT)
            if date_macro is not None and start_date is not None:
                raise QueryError(_EXCLUSIVE_TEXT)
            _check_enum(date_macro, DATE_MACROS, "date_macro")
            _check_enum(accounting_method, ACCOUNTING_METHODS, "accounting_method")
            _check_enum(summarize_column_by, SUMMARIZE_COLUMN_BY, "summarize_column_by")
            end = _iso_date(as_of, "as_of") if as_of else None
            start = _iso_date(start_date, "start_date") if start_date else None
            if start and end:
                _check_window(start, end, start_name="start_date", end_name="as_of", months=None)
            if date_macro is None:
                _check_days_window(summarize_column_by, start, end, end_name="as_of")

            ready = session.ensure_ready(target)
            window = _resolve_macro(date_macro, ready.state.capabilities, target.key)
            check_start, check_end = _bounds(start, end, window)
            _check_days_window(summarize_column_by, check_start, check_end, end_name="as_of")
            checked = _require_tracking(
                ready.state.capabilities, target.key, class_=class_, department=department
            )
            accounting = _accounting_method(accounting_method, ready.state.capabilities, accepted=True)

            params: dict[str, str] = {}
            _put(
                params,
                start_date=start.isoformat() if start else None,
                end_date=end.isoformat() if end else None,
                date_macro=date_macro,
                accounting_method=accounting[0],
                summarize_column_by=summarize_column_by,
                department=joined_ids(department),
            )
            _put(params, **{"class": joined_ids(class_)})
            return _run(
                session, target, tool="report_balance_sheet", endpoint="BalanceSheet",
                params=params, max_rows=max_rows, accounting=accounting, capabilities_checked=checked,
                window=window, entity_grouping=summarize_column_by,
            )

    # --------------------------------------------- report_general_ledger ---
    @server.tool(
        name="report_general_ledger",
        title="General ledger postings by account",
        annotations=read_only("General ledger postings by account"),
        description=(
            "Read one company's GeneralLedger for a window of at most six calendar "
            "months: postings grouped by account with running balances, one section "
            "per account whose header cell carries the account id, and each posting "
            "row keeping its transaction type and id for get_transaction. Use this "
            "for the postings behind an account; use list_transactions for a flat "
            "list across types. account_type takes the AccountType enum with the "
            "spaces removed. columns is restricted to the allowlist; lists of 25 or "
            "more are refused. Read-only."
            + FREE_TEXT_DISCLOSURE
        ),
    )
    def report_general_ledger(
        entity: EntityKey,
        start_date: Annotated[IsoDate, Field(description="YYYY-MM-DD, inclusive.")],
        end_date: Annotated[IsoDate, Field(description="YYYY-MM-DD, inclusive; at most six calendar months after start_date.")],
        # Id filters are lists: Intuit documents each as one or more comma
        # separated ids.
        account: Annotated[IdList | None, Field(description="Account ids to include; sent as one comma-separated value.")] = None,
        account_type: Annotated[GlAccountType | None, Field(description="The AccountType enum WITH THE SPACES REMOVED, plus NonPosting.")] = None,
        columns: Annotated[list[str] | None, Field(description="Column keys to request; restricted to the allowlist. tx_date and txn_type are always included.")] = None,
        accounting_method: Annotated[AccountingMethod | None, Field(description="Defaults to the company's ReportBasis.")] = None,
        class_: Annotated[IdList | None, Field(validation_alias="class", description="Class ids; requires class tracking.")] = None,
        department: Annotated[IdList | None, Field(description="Department ids; requires department tracking.")] = None,
        max_rows: MaxRows = REPORT_DEFAULT_ROWS,
    ) -> Annotated[CallToolResult, ReportEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            _check_enum(account_type, GL_ACCOUNT_TYPES, "account_type")
            _check_enum(accounting_method, ACCOUNTING_METHODS, "accounting_method")
            start = _iso_date(start_date, "start_date")
            end = _iso_date(end_date, "end_date")
            _check_window(start, end, start_name="start_date", end_name="end_date", months=MAX_WINDOW_MONTHS)
            requested = _columns_to_request(columns, "GeneralLedger", GENERAL_LEDGER_DEFAULT_COLUMNS)
            if account is not None and (not account or any(not str(a).strip() for a in account)):
                raise QueryError("account must list at least one non-empty account id")

            ready = session.ensure_ready(target)
            checked = _require_tracking(
                ready.state.capabilities, target.key, class_=class_, department=department
            )
            accounting = _accounting_method(accounting_method, ready.state.capabilities, accepted=True)

            params: dict[str, str] = {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "columns": ",".join(requested),
            }
            _put(
                params,
                account=joined_ids(account),
                account_type=account_type,
                accounting_method=accounting[0],
                department=joined_ids(department),
            )
            _put(params, **{"class": joined_ids(class_)})
            return _run(
                session, target, tool="report_general_ledger", endpoint="GeneralLedger",
                params=params, max_rows=max_rows, accounting=accounting, capabilities_checked=checked,
            )

    # ------------------------------------------------------ report_aging ---
    @server.tool(
        name="report_aging",
        title="Aged payables or receivables, summary or detail",
        annotations=read_only("Aged payables or receivables, summary or detail"),
        description=(
            "Read one company's open payables or receivables by aging bucket, as a "
            "summary (one row per vendor or customer; bucket columns keyed "
            "current/0/1/2/3/total, the human label on each cell) or as detail (one "
            "row per open transaction, keeping its type and id). Use this for what "
            "is owed and how old it is; use list_vendors and list_customers for "
            "balances and summarize_vendor_payments for what was paid. The four "
            "underlying reports accept different parameters: the payables detail "
            "takes accounting_method and no aging_method, the receivables detail the "
            "reverse; both details take a due-date window of at most six calendar "
            "months, past_due, aging_period, num_periods and columns; the summaries "
            "take aging_method, report_date or date_macro, sort_order and department. "
            "A combination the endpoint does not accept is refused before any request. "
            "payables on a company without accounts payable is an error, never an "
            "empty report. Read-only."
            + FREE_TEXT_DISCLOSURE
        ),
    )
    def report_aging(
        entity: EntityKey,
        side: Annotated[AgingSide, Field(description="Accounts payable or accounts receivable.")],
        detail: Annotated[AgingDetail, Field(description="One row per party, or one row per open transaction.")] = "summary",
        aging_method: Annotated[AgingMethod | None, Field(description="Summaries and the receivables detail. Not the payables detail.")] = None,
        report_date: Annotated[IsoDate | None, Field(description="YYYY-MM-DD as-of date. Summaries: not with date_macro.")] = None,
        date_macro: Annotated[DateMacro | None, Field(description="Summaries only; not with report_date.")] = None,
        sort_order: Annotated[SortOrder | None, Field(description="Summaries only.")] = None,
        # Id filters are lists: Intuit documents each as one or more comma
        # separated ids.
        department: Annotated[IdList | None, Field(description="Summaries only: department ids; requires department tracking.")] = None,
        vendor: Annotated[IdList | None, Field(description="payables only: vendor ids; sent as one comma-separated value.")] = None,
        customer: Annotated[IdList | None, Field(description="receivables only: customer ids; sent as one comma-separated value.")] = None,
        accounting_method: Annotated[AccountingMethod | None, Field(description="payables detail only; defaults to the company's ReportBasis there.")] = None,
        start_duedate: Annotated[IsoDate | None, Field(description="Detail only. YYYY-MM-DD; with end_duedate, at most six calendar months.")] = None,
        end_duedate: Annotated[IsoDate | None, Field(description="Detail only. YYYY-MM-DD; with start_duedate.")] = None,
        past_due: Annotated[int | None, Field(ge=0, description="Detail only: minimum days past due.")] = None,
        aging_period: Annotated[int | None, Field(ge=1, description="Detail only: days per aging period.")] = None,
        num_periods: Annotated[int | None, Field(ge=1, description="Detail only: number of aging periods.")] = None,
        columns: Annotated[list[str] | None, Field(description="Detail only: column keys, restricted to the side's allowlist.")] = None,
        max_rows: MaxRows = REPORT_DEFAULT_ROWS,
    ) -> Annotated[CallToolResult, ReportEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            _check_enum(side, ("payables", "receivables"), "side")
            _check_enum(detail, ("summary", "detail"), "detail")
            _check_enum(aging_method, AGING_METHODS, "aging_method")
            _check_enum(date_macro, DATE_MACROS, "date_macro")
            _check_enum(sort_order, SORT_ORDERS, "sort_order")
            _check_enum(accounting_method, ACCOUNTING_METHODS, "accounting_method")

            endpoint = reports_mod.REPORT_ENDPOINTS[
                f"{'AP' if side == 'payables' else 'AR'}Aging{'Summary' if detail == 'summary' else 'Detail'}"
            ]

            def refuse(name: str) -> None:
                raise QueryError(
                    f"'{name}' is not accepted by the {detail} aging report for {side} "
                    f"(QuickBooks endpoint {endpoint}): its documented parameter list "
                    f"does not list it, so it is refused before any request. The four "
                    f"aging reports take different parameters; see the tool description."
                )

            # -- the four allowlists ---------------------------------------
            if side == "payables" and customer is not None:
                refuse("customer")
            if side == "receivables" and vendor is not None:
                refuse("vendor")
            if detail == "summary":
                for name, value in (
                    ("start_duedate", start_duedate), ("end_duedate", end_duedate),
                    ("past_due", past_due), ("aging_period", aging_period),
                    ("num_periods", num_periods), ("columns", columns),
                    ("accounting_method", accounting_method),
                ):
                    if value is not None:
                        refuse(name)
                if date_macro is not None and report_date is not None:
                    raise QueryError(
                        "date_macro and report_date are mutually exclusive on an aging "
                        "summary: give one as-of, not two."
                    )
            else:
                for name, value in (
                    ("date_macro", date_macro), ("sort_order", sort_order), ("department", department),
                ):
                    if value is not None:
                        refuse(name)
                if side == "payables" and aging_method is not None:
                    refuse("aging_method")
                if side == "receivables" and accounting_method is not None:
                    refuse("accounting_method")

            as_of = _iso_date(report_date, "report_date") if report_date else None
            due_start = _iso_date(start_duedate, "start_duedate") if start_duedate else None
            due_end = _iso_date(end_duedate, "end_duedate") if end_duedate else None
            if (due_start is None) != (due_end is None):
                raise QueryError(
                    "start_duedate and end_duedate must be given together: a one-sided "
                    "due-date window is unbounded, and the detail reports carry the "
                    "six-calendar-month limit. Give both, or neither for every open "
                    "item as of report_date."
                )
            if due_start and due_end:
                _check_window(
                    due_start, due_end, start_name="start_duedate", end_name="end_duedate",
                    months=MAX_WINDOW_MONTHS,
                )
            requested: list[str] | None = None
            if detail == "detail":
                requested = _columns_to_request(columns, endpoint, ()) if columns else None

            ready = session.ensure_ready(target)
            window = _resolve_macro(date_macro, ready.state.capabilities, target.key)
            checked = _require_tracking(
                ready.state.capabilities, target.key, class_=None, department=department
            )
            accounting = _accounting_method(
                accounting_method, ready.state.capabilities,
                accepted=(side == "payables" and detail == "detail"),
            )

            # -- payables on a company without accounts payable -------------
            ap_state: str | None = None
            if side == "payables":
                ap_state = session.accounts_payable(target)
                if ap_state == AP_UNAVAILABLE:
                    capabilities = ready.state.capabilities
                    fault = capabilities.accounts_payable_fault_code if capabilities else None
                    raise CapabilityUnavailable(_ap_unavailable_text(target.key, fault))

            params: dict[str, str] = {}
            _put(
                params,
                aging_method=aging_method,
                report_date=as_of.isoformat() if as_of else None,
                date_macro=date_macro,
                sort_order=sort_order,
                department=joined_ids(department),
                vendor=joined_ids(vendor),
                customer=joined_ids(customer),
                accounting_method=accounting[0],
                start_duedate=due_start.isoformat() if due_start else None,
                end_duedate=due_end.isoformat() if due_end else None,
                past_due=past_due,
                aging_period=aging_period,
                num_periods=num_periods,
                columns=",".join(requested) if requested else None,
            )

            try:
                result = _run(
                    session, target, tool="report_aging", endpoint=endpoint, params=params,
                    max_rows=max_rows, accounting=accounting, capabilities_checked=checked,
                    side=side, detail=detail,
                    accounts_payable="available" if side == "payables" else None,
                    window=window,
                )
            except UpstreamError as exc:
                if side == "payables" and exc.fault_code == ACCOUNTS_PAYABLE_FEATURE_FAULT:
                    # First use established it: recorded for this process,
                    # not retried, reported as a capability error.
                    session.note_accounts_payable(target, available=False, fault_code=exc.fault_code)
                    raise CapabilityUnavailable(_ap_unavailable_text(target.key, exc.fault_code)) from None
                raise
            if side == "payables":
                # An aging report that answered is a use of accounts payable
                # that succeeded: recorded as available, like a BillPayment
                # read that succeeded.
                session.note_accounts_payable(target, available=True)
            return result
