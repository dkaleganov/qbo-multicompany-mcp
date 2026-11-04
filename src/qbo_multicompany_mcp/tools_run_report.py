"""``run_report``: the 21 documented reports without a named tool.

One tool, one generic parameter set, and a per-report allowlist from
:mod:`qbo_multicompany_mcp.report_catalog`: a parameter Intuit does not
document for the report -- or that this server chose not to expose for it --
is refused by name before any request, the network policy refuses it again
per path, and the send-side inventory backstop refuses it a third time.
CashFlow and TrialBalance are reachable only here and get the same treatment.

What every report gets, from the same code the named tools use:

* the identity fields on the result, its ``summary`` first, ``max_rows`` with
  completeness;
* the privacy allowlist on requested columns AND returned cells, keyed on
  column identifiers, with the sensitive keys refused twice;
* ``date_macro`` and explicit dates mutually exclusive, the macro resolved
  in the company's book calendar before any window rule reads it;
* the six-calendar-month limit on every detail report, on explicit dates and
  resolved macros alike; the 200-day ``Days`` rule; ``columns`` lists of 25
  or more refused; ``accounting_method`` defaulting to the company's report
  basis where the report takes one; class and department filters refused
  when the company does not track them, never dropped;
* routing and structural ids kept unconditionally: ``include_ids`` is not a
  parameter.

TaxSummary stays in the enum although the sandbox company refused it with
fault 5020 on 2026-09-24: another company may serve it, and the 5020 mapping
makes the refusal a fixed, actionable error rather than an empty report. Its
``agency_id`` is the one parameter any of Intuit's report pages marks Required,
and it is required here.

TransactionListByCustomer is sent no ``columns`` and refuses a caller's with
the reason (``report_catalog.BY_CUSTOMER_COLUMNS_REFUSAL``): in production
Intuit faults on any value (observed 2026-09-28), and without them the report
answers with its own default set.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Final

from mcp.server import MCPServer
from mcp_types import CallToolResult
from pydantic import Field

from . import reports as reports_mod
from .config import Settings
from .date_macros import ALL_DATE_MACROS
from .envelopes import ReportEnvelope
from .query_grammar import QueryError
from .registry import Registry
from .report_catalog import (
    CATALOG,
    NAMED_TOOL_REPORTS,
    REPORT_NAMES,
    WINDOW_AS_OF,
    WINDOW_DATE,
    WINDOW_DUEDATE,
)
from .session import QboSession
from .tools import FREE_TEXT_DISCLOSURE, EntityKey, IdList, IsoDate, enum_of, joined_ids, read_only, tool_errors
from .tools_data import CLEARED_VALUES, MAX_WINDOW_MONTHS, _iso_date
from .tools_reports import (
    ACCOUNTING_METHODS,
    AGING_METHODS,
    GL_ACCOUNT_TYPES,
    REPORT_DEFAULT_ROWS,
    SORT_ORDERS,
    SUMMARIZE_COLUMN_BY,
    AccountingMethod,
    AgingMethod,
    GlAccountType,
    MaxRows,
    SortOrder,
    SummarizeBy,
    _accounting_method,
    _bounds,
    _check_days_window,
    _check_enum,
    _check_window,
    _columns_to_request,
    _put,
    _require_tracking,
    _resolve_macro,
    _run,
)

#: The published enums, through json_schema_extra: the SDK would refuse a
#: Literal by quoting the value. Each is checked in the tool before any
#: request, with fixed text.
ReportName = enum_of(REPORT_NAMES)
#: The schema-level macro vocabulary is the union (33); each report's own
#: list is checked in the tool, because the enum differs per report.
AnyDateMacro = enum_of(ALL_DATE_MACROS)
ReportTransactionType = enum_of(reports_mod.REPORT_TRANSACTION_TYPES)
SourceAccountType = enum_of(reports_mod.REPORT_SOURCE_ACCOUNT_TYPES)
Cleared = enum_of(CLEARED_VALUES)

DESCRIPTION: Final = (
    "Run one of the 21 documented QuickBooks reports that has no named tool "
    "(AccountListDetail, CashFlow, CustomerBalance, CustomerBalanceDetail, "
    "CustomerIncome, InventoryValuationDetail, InventoryValuationSummary, "
    "JournalReport, ProfitAndLossDetail, SalesByClassSummary, SalesByCustomer, "
    "SalesByDepartment, SalesByProduct, TaxSummary, TransactionListByCustomer, "
    "TransactionListByVendor, TransactionListWithSplits, TrialBalance, "
    "VendorBalance, VendorBalanceDetail, VendorExpenses), flattened with its "
    "section tree kept and every total naming its scope. Use this for those; "
    "use report_profit_and_loss, report_balance_sheet, report_general_ledger "
    "and report_aging for their reports and list_transactions for "
    "TransactionList. Each report accepts only the parameters Intuit documents "
    "for it, and a parameter it does not take is refused by name "
    "before any request. Detail reports (ProfitAndLossDetail, JournalReport, "
    "the three TransactionList variants, InventoryValuationDetail, and the two "
    "balance details on their due-date window) need a window of at most six "
    "calendar months, given as dates or as a date_macro, which is resolved in "
    "the company's book calendar before the check. The two balance details "
    "take due dates only, and may omit them. Summary reports may omit "
    "the window. TaxSummary requires agency_id. columns is restricted to each "
    "report's allowlist; address, email, telephone, message and account-number "
    "columns can be neither requested nor returned. Read-only; requires an "
    "explicit 'entity'." + FREE_TEXT_DISCLOSURE
)

REPORT_NOTE: Final = (
    "This report publishes no row total and no documented cap signal, so "
    "completeness is 'unknown' unless something in this result establishes "
    "otherwise (an anomaly or a local trim make it 'partial'). Section totals "
    "are QuickBooks's stated figures, reported as stated; 'scope' names what "
    "each total covers. An empty rows array means this response carried no "
    "rows, never that the company has none. parameters_sent lists what this "
    "server sent; only some parameters are echoed by the report."
)

_DOCUMENTATION_DERIVED_DISCLOSURE: Final = (
    "this report's column shape is documentation-derived: it was never seen "
    "live (the sandbox company refused it), so the columns published here follow "
    "the sample on Intuit's page and a live column set may differ"
)

_ID_FILTERS: Final = ("customer", "vendor", "item", "employee", "payment_method", "agency_id", "department")


def _check_report(report: str) -> None:
    """The refusal behind the published enum (a string with the enum in its
    schema): the enum, and the named tools' reports pointed at their tool.
    Fixed text; the value is never repeated."""
    if report in CATALOG:
        return
    if report in NAMED_TOOL_REPORTS:
        raise QueryError(
            f"that report is served by {NAMED_TOOL_REPORTS[report]}, not by run_report: "
            f"run_report's enum is disjoint from the named tools."
        )
    raise QueryError(
        f"report is not one of the documented values. Accepted: {', '.join(REPORT_NAMES)}."
    )


def _refuse(name: str, report: str, endpoint: str) -> None:
    raise QueryError(
        f"'{name}' is not accepted by {report} (QuickBooks endpoint {endpoint}): its "
        f"documented parameter list does not name it, or this server does not send "
        f"it there, so it is refused before any request. See the tool description for "
        f"what each report takes."
    )


def register_run_report(
    server: MCPServer, settings: Settings, registry: Registry, session: QboSession
) -> None:
    # -------------------------------------------------------- run_report ---
    @server.tool(
        name="run_report",
        title="Run a documented report without a named tool",
        annotations=read_only("Run a documented report without a named tool"),
        description=DESCRIPTION,
    )
    def run_report(
        entity: EntityKey,
        report: Annotated[ReportName, Field(description="The report, by its documented name.")],
        start_date: Annotated[IsoDate | None, Field(description="YYYY-MM-DD, inclusive. Not with date_macro.")] = None,
        end_date: Annotated[IsoDate | None, Field(description="YYYY-MM-DD, inclusive. Not with date_macro. Detail reports: at most six calendar months after start_date.")] = None,
        date_macro: Annotated[AnyDateMacro | None, Field(description="A documented QuickBooks date range; each report admits its page's own list. Not with explicit dates. Resolved here before the window rules are checked.")] = None,
        report_date: Annotated[IsoDate | None, Field(description="YYYY-MM-DD as-of date, on the balance and valuation reports. Not with date_macro on an as-of report.")] = None,
        start_duedate: Annotated[IsoDate | None, Field(description="CustomerBalanceDetail and VendorBalanceDetail: with end_duedate, a due-date window of at most six calendar months.")] = None,
        end_duedate: Annotated[IsoDate | None, Field(description="With start_duedate.")] = None,
        accounting_method: Annotated[AccountingMethod | None, Field(description="Where the report takes it; defaults to the company's ReportBasis there.")] = None,
        summarize_column_by: Annotated[SummarizeBy | None, Field(description="Where the report takes it. Days needs a window of at most 200 days. An entity grouping returns one column per entity keyed by its id.")] = None,
        aging_method: Annotated[AgingMethod | None, Field(description="CustomerBalanceDetail only.")] = None,
        # Intuit's report pages document these id filters as one or more comma
        # separated ids, but agency_id as one required id and payment_method
        # as a payment method's NAME.
        customer: Annotated[IdList | None, Field(description="Customer ids, where the report takes them; sent as one comma-separated value.")] = None,
        vendor: Annotated[IdList | None, Field(description="Vendor ids, where the report takes them; sent as one comma-separated value.")] = None,
        class_: Annotated[IdList | None, Field(validation_alias="class", description="Class ids; requires class tracking.")] = None,
        department: Annotated[IdList | None, Field(description="Department ids; requires department tracking.")] = None,
        item: Annotated[IdList | None, Field(description="Item ids, where the report takes them.")] = None,
        employee: Annotated[IdList | None, Field(description="ProfitAndLossDetail: employee ids.")] = None,
        account: Annotated[IdList | None, Field(description="ProfitAndLossDetail: account ids to include.")] = None,
        account_type: Annotated[GlAccountType | None, Field(description="AccountListDetail and ProfitAndLossDetail: the AccountType enum WITH THE SPACES REMOVED, plus NonPosting.")] = None,
        payment_method: Annotated[str | None, Field(description="A payment method's name as the report pages list them (Cash, Check, Visa and the like), where the report takes it.")] = None,
        agency_id: Annotated[str | None, Field(description="TaxSummary: the tax agency id. Required there.")] = None,
        transaction_type: Annotated[ReportTransactionType | None, Field(description="The TransactionList variants: the report filter's own vocabulary, as list_transactions documents it.")] = None,
        source_account_type: Annotated[SourceAccountType | None, Field(description="The TransactionList variants: the AccountType enum with the spaces removed.")] = None,
        cleared: Annotated[Cleared | None, Field(description="TransactionListByCustomer and TransactionListByVendor.")] = None,
        docnum: Annotated[str | None, Field(description="The TransactionList variants: document number, free text.")] = None,
        memo: Annotated[str | None, Field(description="TransactionListByCustomer and TransactionListByVendor: memo, free text.")] = None,
        sort_order: Annotated[SortOrder | None, Field(description="Where the report takes it.")] = None,
        columns: Annotated[list[str] | None, Field(description="Column keys to request, where the report takes columns; restricted to the report's allowlist. tx_date and txn_type are added wherever the report's own vocabulary has them (not AccountListDetail's).")] = None,
        max_rows: MaxRows = REPORT_DEFAULT_ROWS,
    ) -> Annotated[CallToolResult, ReportEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            _check_report(report)
            spec = CATALOG[report]
            endpoint = spec.endpoint

            # -- the per-report allowlist: refused by NAME, before anything --
            given: dict[str, Any] = {
                "start_date": start_date, "end_date": end_date, "date_macro": date_macro,
                "report_date": report_date, "start_duedate": start_duedate, "end_duedate": end_duedate,
                "accounting_method": accounting_method, "summarize_column_by": summarize_column_by,
                "aging_method": aging_method, "customer": customer, "vendor": vendor, "class": class_,
                "department": department, "item": item, "employee": employee, "account": account,
                "account_type": account_type, "payment_method": payment_method, "agency_id": agency_id,
                "transaction_type": transaction_type, "source_account_type": source_account_type,
                "cleared": cleared, "docnum": docnum, "memo": memo, "sort_order": sort_order,
                "columns": columns,
            }
            # A report sent no columns because Intuit faults on them says so,
            # rather than refusing them as merely unlisted.
            if columns is not None and spec.columns_refusal:
                raise QueryError(spec.columns_refusal)
            for name, value in given.items():
                if value is not None and name not in spec.parameters:
                    _refuse(name, report, endpoint)

            # -- enumerations: the refusals behind the published enums --------
            # The macro vocabulary is THIS report's page's list, not the
            # schema's union: the enum differs per report. A report whose page
            # lists none has already refused the parameter by name above.
            _check_enum(date_macro, spec.date_macros or ALL_DATE_MACROS, "date_macro")
            _check_enum(accounting_method, ACCOUNTING_METHODS, "accounting_method")
            _check_enum(summarize_column_by, SUMMARIZE_COLUMN_BY, "summarize_column_by")
            _check_enum(aging_method, AGING_METHODS, "aging_method")
            _check_enum(sort_order, SORT_ORDERS, "sort_order")
            _check_enum(account_type, GL_ACCOUNT_TYPES, "account_type")
            _check_enum(transaction_type, reports_mod.REPORT_TRANSACTION_TYPES, "transaction_type")
            _check_enum(source_account_type, reports_mod.REPORT_SOURCE_ACCOUNT_TYPES, "source_account_type")
            _check_enum(cleared, CLEARED_VALUES, "cleared")
            if "agency_id" in spec.parameters and not (agency_id or "").strip():
                raise QueryError(
                    f"{report} requires agency_id, the tax agency to report on "
                    f"(the one parameter Intuit's report pages mark Required). "
                    f"No request was made."
                )
            if account is not None and (not account or any(not str(a).strip() for a in account)):
                raise QueryError("account must list at least one non-empty account id")

            # -- the window, by the report's kind ---------------------------
            start = _iso_date(start_date, "start_date") if start_date else None
            end = _iso_date(end_date, "end_date") if end_date else None
            as_of = _iso_date(report_date, "report_date") if report_date else None
            due_start = _iso_date(start_duedate, "start_duedate") if start_duedate else None
            due_end = _iso_date(end_duedate, "end_duedate") if end_duedate else None
            months = MAX_WINDOW_MONTHS if spec.detail else None

            if spec.window == WINDOW_DATE:
                if date_macro is not None and (start is not None or end is not None):
                    raise QueryError(
                        "date_macro and explicit dates are mutually exclusive: give a macro or a "
                        "date range, not both."
                    )
                if (start is None) != (end is None):
                    raise QueryError("start_date and end_date must be given together")
                if spec.detail and start is None and date_macro is None:
                    raise QueryError(
                        f"{report} is a detail report and needs a window: give start_date "
                        f"and end_date at most six calendar months apart, or a date_macro. "
                        f"No request was made."
                    )
                if start and end:
                    _check_window(start, end, start_name="start_date", end_name="end_date", months=months)
                if date_macro is None:
                    _check_days_window(summarize_column_by, start, end, end_name="end_date")
            elif spec.window == WINDOW_DUEDATE:
                if (due_start is None) != (due_end is None):
                    raise QueryError(
                        "start_duedate and end_duedate must be given together: a one-sided "
                        "due-date window is unbounded, and the balance details carry the "
                        "six-calendar-month limit. Give both, or neither for every open "
                        "item as of report_date."
                    )
                if due_start and due_end:
                    _check_window(due_start, due_end, start_name="start_duedate", end_name="end_duedate", months=months)
            elif spec.window == WINDOW_AS_OF:
                if date_macro is not None and as_of is not None:
                    raise QueryError(
                        "date_macro and report_date are mutually exclusive on an as-of report: "
                        "give one as-of, not two."
                    )

            requested: list[str] | None = None
            if "columns" in spec.parameters:
                requested = _columns_to_request(columns, endpoint, spec.default_columns)

            # -- the guard, the capabilities, the resolution -----------------
            ready = session.ensure_ready(target)
            window = _resolve_macro(date_macro, ready.state.capabilities, target.key)
            if window is not None and spec.window == WINDOW_DATE:
                check_start, check_end = _bounds(start, end, window)
                if spec.detail:
                    _check_window(check_start, check_end, start_name="start_date", end_name="end_date", months=months)
                _check_days_window(summarize_column_by, check_start, check_end, end_name="end_date")
            checked = _require_tracking(
                ready.state.capabilities, target.key, class_=class_, department=department
            )
            accounting = _accounting_method(
                accounting_method, ready.state.capabilities,
                accepted="accounting_method" in spec.parameters,
            )

            params: dict[str, str] = {}
            _put(
                params,
                start_date=start.isoformat() if start else None,
                end_date=end.isoformat() if end else None,
                date_macro=date_macro,
                report_date=as_of.isoformat() if as_of else None,
                start_duedate=due_start.isoformat() if due_start else None,
                end_duedate=due_end.isoformat() if due_end else None,
                accounting_method=accounting[0],
                summarize_column_by=summarize_column_by,
                aging_method=aging_method,
                customer=joined_ids(customer), vendor=joined_ids(vendor),
                department=joined_ids(department), item=joined_ids(item),
                employee=joined_ids(employee), account=joined_ids(account),
                account_type=account_type, payment_method=payment_method, agency_id=agency_id,
                transaction_type=transaction_type, source_account_type=source_account_type,
                cleared=cleared, docnum=docnum, memo=memo, sort_order=sort_order,
                columns=",".join(requested) if requested else None,
            )
            _put(params, **{"class": joined_ids(class_)})
            return _run(
                session, target, tool="run_report", endpoint=endpoint, params=params, max_rows=max_rows,
                accounting=accounting, capabilities_checked=checked, window=window,
                entity_grouping=summarize_column_by, report=report,
                extra_disclosures=([_DOCUMENTATION_DERIVED_DISCLOSURE] if spec.documentation_derived else []),
            )
