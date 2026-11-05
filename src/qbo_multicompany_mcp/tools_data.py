"""The seven data tools.

All entity-scoped, all read-only, all carrying the identity fields so a reader
can always say which company file an answer came from and how old it is.

Shared vocabulary throughout: a list returns ``count``, ``rows``, ``truncated``
and ``duplicates_dropped``. One idea, one set of names.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Final

from mcp.server import MCPServer
from mcp_types import CallToolResult
from pydantic import Field

from . import reports as reports_mod
from . import vendor_payments as vp
from .capabilities import ACCOUNTS_PAYABLE_FEATURE_FAULT, AP_AVAILABLE, AP_UNAVAILABLE
from .config import Settings
from .envelopes import (
    CandidatePaymentRow,
    CandidateVendorRow,
    ChangedObject,
    ChangesEnvelope,
    CustomerBlock,
    CustomerListEnvelope,
    JournalReviewLine,
    NeedsReview,
    QueryEnvelope,
    ReportAnomalyBlock,
    ReportCellBlock,
    ReportColumnBlock,
    ReportRowBlock,
    SourceWalked,
    TransactionBlock,
    TransactionEnvelope,
    TransactionListEnvelope,
    UnclassifiedRow,
    ZeroRowRecheck,
    VendorBlock,
    VendorListEnvelope,
    VendorPaymentsEnvelope,
)
from .errors import QboMcpError, UpstreamError
from .pagination import count_of
from .projections import (
    has_projection,
    is_scalar,
    project_customer,
    project_entity,
    project_transaction,
    project_vendor,
)
from .query_grammar import (
    TRANSACTION_TYPES,
    Condition,
    ParsedQuery,
    QueryError,
    escape_literal,
    parse,
    served_entities,
)
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

MAX_LIMIT: Final = 5000
DEFAULT_LIMIT: Final = 200

#: TransactionList windows are capped at six calendar months. A wider window is
#: refused rather than silently narrowed: a model that asked for a year and
#: received six months would report the six months as the year.
MAX_WINDOW_MONTHS: Final = 6

#: Change data capture looks back 30 days and no further (Intuit's
#: ChangeDataCapture page, which advises a shorter look-back).
CHANGES_LOOKBACK_DAYS: Final = 30

#: The cap applies to the WHOLE response across all types (Intuit's
#: ChangeDataCapture page: "a maximum of 1000 objects"). 600 of one type and
#: 400 of another saturates it with neither reaching 1000.
CHANGES_OBJECT_CAP: Final = 1000

#: Served entities that change data capture does not cover (Intuit's
#: ChangeDataCapture page: "supported for all objects except JournalCode,
#: TimeActivity, TaxAgency, TaxCode, and TaxRate"). A projection makes an
#: entity servable by run_query and list_reference_data, not by list_changes.
CDC_UNSUPPORTED: Final[frozenset[str]] = frozenset({"TaxCode", "TaxRate"})

#: The refusal for a change response that is not the documented shape. Fixed
#: text: what could be read is withheld too, because a count that skipped an
#: entry looks complete.
CDC_MALFORMED_TEXT: Final = (
    "the change response did not have the documented shape, so it cannot be "
    "shown complete. Refusing it whole rather than returning the part that "
    "could be read; do not advance a synchronization checkpoint from it."
)

#: What an entry of a change response holds beside a type's objects, as sandbox
#: responses showed (docs/api-findings.md, item 7): its paging fields.
CDC_PAGING_FIELDS: Final = frozenset({"startPosition", "maxResults"})

#: **Every candidate tried was accepted, 4 of 4** (live sandbox probe;
#: docs/api-findings.md section 8). An unaccepted value returns HTTP 400
#: Invalid Enumeration, so a 200 proves membership; it does not prove the set
#: is closed, and this pin may be narrower than what QuickBooks accepts.
CLEARED_VALUES: Final[tuple[str, ...]] = ("Cleared", "Uncleared", "Reconciled", "Deposited")

#: The refusal for rows of a type with no projection on this route. Fixed text.
TRANSACTION_ROWS_REFUSED: Final = (
    "run_query returns rows for list entities only. For transaction rows, use "
    "list_transactions and get_transaction. SELECT COUNT(*) works on every "
    "served entity."
)

#: list_transactions' two published enums, through json_schema_extra; the tool
#: refuses a value outside either.
ClearedFilter = enum_of(CLEARED_VALUES)
TransactionTypeFilter = enum_of(reports_mod.REPORT_TRANSACTION_TYPES)
SourceAccountTypeFilter = enum_of(reports_mod.REPORT_SOURCE_ACCOUNT_TYPES)

#: What ``list_transactions`` asks for when the caller names no columns: the
#: nine QuickBooks itself returns when TransactionList is sent no ``columns``,
#: in its order (observed 2026-09-24 on sandbox). Requested BY NAME, because
#: that no-parameter response names each column only in ``ColType``, with no
#: ``ColKey`` metadata -- a second shape the parser has no reason to learn.
#: Every allowlisted key (26) would exceed the limit of 24.
TRANSACTION_LIST_DEFAULT_COLUMNS: Final[tuple[str, ...]] = (
    "tx_date", "txn_type", "doc_num", "is_no_post", "name", "memo",
    "account_name", "other_account", "subt_nat_amount",
)

REPORT_NOTE: Final = (
    "TransactionList publishes no row total and no documented cap signal, so "
    "completeness cannot be established from the response. It is reported as "
    "'unknown' rather than assumed complete; an empty rows array means the "
    "response carried no rows, never that the company has no transactions. "
    "filters_sent lists the filter parameters this server sent; nothing in the "
    "response confirms that a filter was applied to it. Validation is not "
    "uniform across the filters: transaction_type, cleared and source_account_type are "
    "enumerated and refuse an unaccepted value with an error, so this server "
    "pins all three and a bad value cannot be sent; vendor, customer, docnum "
    "and memo are matched by id or free text, and a value that simply does not "
    "exist returns zero rows rather than an error. transaction_type is also a "
    "third vocabulary -- neither the display labels in the txn_type column nor "
    "the entity names used by run_query and get_transaction -- so do not pass a "
    "txn_type cell value back as a filter; see filterable_transaction_types."
)

CHANGES_UNSAFE_NOTE: Final = (
    "A possibly incomplete response is NOT a safe synchronization checkpoint. "
    "The 1000-object cap is shared across every requested type, so saturation "
    "makes every type in this response uncertain. Re-running a query on a "
    "modification timestamp cannot recover deletions beyond retention, so it is "
    "not an equivalent fallback."
)


def _bill_payments_not_walked(fault_code: str | None) -> SourceWalked:
    code = fault_code or ACCOUNTS_PAYABLE_FEATURE_FAULT
    return SourceWalked(
        source=vp.SOURCE_BILL_PAYMENT,
        walked=False,
        reason=(
            f"QuickBooks refused the BillPayment read for this company with fault "
            f"{code} (feature not supported), so accounts payable is recorded as "
            f"unavailable for this entity and this walk was Purchase-only. This is a "
            f"restriction on the scan, not an absence of data; it is asked again "
            f"after a re-consent."
        ),
    )


def _explain_zero_rows(
    filters_sent: Sequence[str], unfiltered_total: int, *, unfiltered_partial: bool = False
) -> str:
    """Say which of the two cases an empty filtered report is.

    Naming the id and free-text filters specifically matters: those are the
    only ones where a wrong value produces an empty report instead of an error,
    so they are where a reader should look first. Implying that any filter can
    silently mismatch would send them to check the enumerated ones, which
    cannot.

    ``unfiltered_partial`` says the re-read listed rows for what it couldn't
    read. Nothing says how many transactions each stands for, so the count may
    not be a count of transactions. A cell or column problem leaves one row per
    transaction, and the count exact.
    """
    if unfiltered_total == 0:
        # Not "the window holds no transactions": an empty rows array never
        # establishes that there is no data, and completeness is unknown in
        # this very result. What the re-read does establish is narrower, and
        # it is all this says.
        return (
            "The same window re-read without filters also returned no rows, so "
            "the filter is not what emptied this result. That is not evidence "
            "the window holds no transactions: an empty report only says this "
            "response carried none, and this report publishes no total or cap "
            "signal to say more."
        )

    applied = ", ".join(sorted(filters_sent))
    holds = f"the same window holds {unfiltered_total} transactions unfiltered."
    if unfiltered_partial:
        holds += " That re-read was itself partial, so the count may not be exact."
    unvalidated = sorted(set(filters_sent) & reports_mod.SILENT_MISMATCH_FILTERS)
    if unvalidated:
        return (
            f"No rows matched {applied}; {holds} "
            f"{', '.join(unvalidated)} "
            f"{'is' if len(unvalidated) == 1 else 'are'} matched by id or free "
            f"text, and a value that does not exist returns zero rows rather "
            f"than an error -- check "
            f"{'that' if len(unvalidated) == 1 else 'those'} first."
        )
    return (
        f"No rows matched {applied}; {holds} Every filter applied is enumerated, "
        f"and an unaccepted value would have been refused with an error, so this "
        f"is a genuine absence of matching rows rather than a bad filter value."
    )


def _limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if not 1 <= value <= MAX_LIMIT:
        raise QueryError(f"limit must be between 1 and {MAX_LIMIT}")
    return value


def _identity(entity: Entity, started: datetime, finished: datetime) -> dict[str, Any]:
    return {
        "entity": entity.key,
        "environment": entity.environment,
        "display_name_from_registry": entity.display_name,
        "realm_id_last4": entity.realm_id_last4,
        "request_started_at": started,
        "fetched_at": finished,
    }


def _iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise QueryError(f"{field_name} must be a date as YYYY-MM-DD") from None


def _add_months(moment: date, months: int) -> date:
    """``moment`` plus ``months`` calendar months, clamping the day.

    Clamping matters at month ends: 31 August plus one month is 30 September,
    not an invalid date.
    """
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def window_refusal_text(start_name: str, end_name: str, months: int) -> str:
    """The six-month refusal, shared by list_transactions and the report
    tools. It names the two parameters and the limit, never the dates: they
    are the caller's, and no refusal repeats a caller's value."""
    return (
        f"the window from {start_name} to {end_name} may span at most {months} calendar "
        f"months (the six-month limit on every detail report), and the one asked "
        f"for is wider. It is refused rather than silently narrowed: an answer covering "
        f"part of the window, reported as covering all of it, would be wrong in a way "
        f"nobody could see."
    )


#: The shapes Intuit writes its own timestamps in: a date, or a date and time
#: with an optional fraction of a second and an optional ``Z`` or offset.
_TIMESTAMP_SHAPE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})?)?"
)

#: What the refusal and the parameter both say is accepted.
_TIMESTAMP_HELP: Final = (
    "a date, or a date and time to the second such as 2026-09-01T00:00:00, with an optional "
    "fraction and Z or an offset"
)


def _check_updated_since(updated_since: str | None) -> str | None:
    """The caller's timestamp, refused before any request when it isn't one.

    Sent as written once it has the shape of Intuit's own timestamps and names
    a real moment. Python reads more ISO forms than that, a week date or a
    compact time, and none of those is known to work in a query, so the check
    keeps every value the input alone rules out from reaching the guard or
    Intuit.
    """
    if not updated_since:
        return None
    moment = updated_since.strip()
    if _TIMESTAMP_SHAPE.fullmatch(moment):
        try:
            datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return moment
    raise QueryError(f"updated_since must be {_TIMESTAMP_HELP}")


def _name_list_query(
    entity_name: str,
    *,
    active: bool | None,
    updated_since: str | None,
    extra: Sequence[Condition] = (),
) -> ParsedQuery:
    """Build a name-list query from validated pieces.

    Rendered through the parser, not string-concatenated, so the statement that
    runs is one the grammar accepted.
    """
    clauses: list[str] = []
    if active is None:
        # The documented way to include inactive rows.
        clauses.append("Active IN (true, false)")
    else:
        clauses.append(f"Active = {'true' if active else 'false'}")
    if updated_since:
        moment = updated_since.strip()
        clauses.append(f"MetaData.LastUpdatedTime >= '{escape_literal(moment)}'")
    for condition in extra:
        clauses.append(condition.render())

    statement = f"SELECT * FROM {entity_name} WHERE {' AND '.join(clauses)} ORDERBY DisplayName ASC"
    return parse(statement)


def register_data_tools(
    server: MCPServer, settings: Settings, registry: Registry, session: QboSession
) -> None:
    # ----------------------------------------------------------------- 1 ---
    @server.tool(
        name="list_vendors",
        title="List vendors",
        annotations=read_only("List vendors"),
        description=(
            "List a company's vendors with the 1099 flag, balance, and whether a "
            "tax identifier is on file (never the value). Use this for the vendor "
            "list and the 1099 flag; use summarize_vendor_payments for candidate "
            "totals of what was paid and report_aging for what is owed by age. "
            "Pass active=null to "
            "include inactive vendors. Read-only."
            + FREE_TEXT_DISCLOSURE
        ),
    )
    def list_vendors(
        entity: EntityKey,
        active: Annotated[
            bool | None,
            Field(description="null includes inactive vendors; true or false filters."),
        ] = True,
        vendor_1099: Annotated[
            bool | None, Field(description="Filter on the company's own 1099 flag.")
        ] = None,
        updated_since: Annotated[
            str | None, Field(description=f"Only rows changed since: {_TIMESTAMP_HELP}.")
        ] = None,
        limit: Annotated[int | None, Field(ge=1, le=MAX_LIMIT, description="Rows returned; the walk still runs to the end.")] = DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, VendorListEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            since = _check_updated_since(updated_since)
            session.ensure_ready(target)
            started = datetime.now(UTC)

            extra: list[Condition] = []
            if vendor_1099 is not None:
                extra.append(
                    Condition("Vendor1099", "=", (str(vendor_1099).lower(),), False)
                )
            query = _name_list_query(
                "Vendor", active=active, updated_since=since, extra=extra
            )
            walk = session.walk(target, query, client_limit=_limit(limit))

            return make_result(
                VendorListEnvelope(
                    **_identity(target, started, datetime.now(UTC)),
                    count=len(walk.rows),
                    rows=[VendorBlock(**project_vendor(row)) for row in walk.rows],
                    truncated=walk.truncated,
                    duplicates_dropped=walk.duplicates_dropped,
                    pages_walked=walk.pages_walked,
                    walk_started_at=walk.walk_started_at,
                    walk_finished_at=walk.walk_finished_at,
                )
            )

    # ----------------------------------------------------------------- 2 ---
    @server.tool(
        name="list_customers",
        title="List customers",
        annotations=read_only("List customers"),
        description=(
            "List a company's customers with their balances. Use this for the "
            "customer list; use report_aging for receivables by age and run_query "
            "for filters this tool lacks. Carries no "
            "tax ids, notes or contact fields. Pass active=null to include inactive. "
            "Read-only."
            + FREE_TEXT_DISCLOSURE
        ),
    )
    def list_customers(
        entity: EntityKey,
        active: Annotated[bool | None, Field(description="null includes inactive.")] = True,
        updated_since: Annotated[
            str | None, Field(description=f"Only rows changed since: {_TIMESTAMP_HELP}.")
        ] = None,
        limit: Annotated[int | None, Field(ge=1, le=MAX_LIMIT, description="Rows returned; the walk still runs to the end.")] = DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, CustomerListEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            since = _check_updated_since(updated_since)
            session.ensure_ready(target)
            started = datetime.now(UTC)

            query = _name_list_query(
                "Customer", active=active, updated_since=since
            )
            walk = session.walk(target, query, client_limit=_limit(limit))

            return make_result(
                CustomerListEnvelope(
                    **_identity(target, started, datetime.now(UTC)),
                    count=len(walk.rows),
                    rows=[CustomerBlock(**project_customer(row)) for row in walk.rows],
                    truncated=walk.truncated,
                    duplicates_dropped=walk.duplicates_dropped,
                    pages_walked=walk.pages_walked,
                    walk_started_at=walk.walk_started_at,
                    walk_finished_at=walk.walk_finished_at,
                )
            )

    # ----------------------------------------------------------------- 3 ---
    @server.tool(
        name="list_transactions",
        title="List transactions in a date window",
        annotations=read_only("List transactions in a date window"),
        description=(
            "Read TransactionList report rows for a date window of at most six "
            "calendar months, one flat row per transaction. Use this to find "
            "transactions and their ids; use report_general_ledger for postings by "
            "account and run_report for TransactionList grouped by customer or by "
            "vendor, or with splits; run_query only counts transactions. Each "
            "routable row "
            "keeps its transaction type and id, which is how you get from a row "
            "to get_transaction; see that tool for turning a label into a type. "
            "Read-only."
            + FREE_TEXT_DISCLOSURE
        ),
    )
    def list_transactions(
        entity: EntityKey,
        start_date: Annotated[IsoDate, Field(description="YYYY-MM-DD, inclusive.")],
        end_date: Annotated[IsoDate, Field(description="YYYY-MM-DD, inclusive.")],
        transaction_type: Annotated[
            TransactionTypeFilter | None,
            Field(
                description="A THIRD vocabulary -- not the display labels in the "
                "txn_type column, and not the entity names used by run_query and "
                "get_transaction. 'JournalEntry' works, 'Journal Entry' does not; "
                "'Purchase' and 'BillPayment' do not. QuickBooks refuses an "
                "unaccepted value with HTTP 400 (observed in sandbox), "
                "so the set is pinned here and a "
                "value outside it is refused before any request."
            ),
        ] = None,
        # Id filters, lists: Intuit's TransactionList page documents each as
        # "One or more comma separated ... IDs".
        vendor: Annotated[IdList | None, Field(description="Vendor ids; sent as one comma-separated value.")] = None,
        customer: Annotated[IdList | None, Field(description="Customer ids; sent as one comma-separated value.")] = None,
        cleared: Annotated[
            ClearedFilter | None,
            Field(description="One of Cleared, Uncleared, Reconciled, Deposited."),
        ] = None,
        source_account_type: Annotated[
            SourceAccountTypeFilter | None,
            Field(
                description="The AccountType enum WITH THE SPACES REMOVED. "
                "'AccountsPayable' filters; 'Accounts Payable' is refused. The "
                "chart of accounts -- including list_accounts here -- reports the "
                "SPACED form, so the value you read off an account is not the "
                "value that filters on it."
            ),
        ] = None,
        docnum: Annotated[
            str | None, Field(description="Document number, matched as free text.")
        ] = None,
        memo: Annotated[str | None, Field(description="Memo text, matched as free text.")] = None,
        columns: Annotated[
            list[str] | None,
            Field(
                description="Column keys to request, from the allowlist; tx_date and "
                "txn_type are always added, and a request of 25 or more columns is "
                "refused. Omitted: QuickBooks's own nine defaults."
            ),
        ] = None,
        max_rows: Annotated[
            int | None,
            Field(ge=1, le=MAX_LIMIT, description="Rows returned after flattening; the rest are trimmed locally and disclosed."),
        ] = DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, TransactionListEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            start = _iso_date(start_date, "start_date")
            end = _iso_date(end_date, "end_date")
            if end < start:
                raise QueryError("end_date is before start_date")
            if end >= _add_months(start, MAX_WINDOW_MONTHS):
                raise QueryError(window_refusal_text("start_date", "end_date", MAX_WINDOW_MONTHS))

            if cleared is not None and cleared not in CLEARED_VALUES:
                raise QueryError(
                    f"cleared must be one of: {', '.join(CLEARED_VALUES)}"
                )

            # The refusals behind the two published enums: the SDK accepts any
            # string, so these are the gate, and neither repeats the value.
            if transaction_type is not None and (
                transaction_type not in reports_mod.REPORT_TRANSACTION_TYPES
            ):
                raise QueryError(
                    reports_mod.explain_rejected_transaction_type(transaction_type)
                )
            if source_account_type is not None and (
                source_account_type not in reports_mod.REPORT_SOURCE_ACCOUNT_TYPES
            ):
                raise QueryError(
                    reports_mod.explain_rejected_source_account_type(
                        source_account_type
                    )
                )

            # The report tools' helper: the allowlist (unknown keys named by
            # position, never by value), the routing columns, and the refusal
            # of 25 or more -- counted on the request.
            from .tools_reports import _columns_to_request

            requested = _columns_to_request(
                columns, "TransactionList", TRANSACTION_LIST_DEFAULT_COLUMNS
            )

            session.ensure_ready(target)
            params: dict[str, str] = {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "columns": ",".join(requested),
            }
            filters_sent: list[str] = []
            for name, value in (
                ("transaction_type", transaction_type),
                ("vendor", joined_ids(vendor)),
                ("customer", joined_ids(customer)),
                ("cleared", cleared),
                ("source_account_type", source_account_type),
                ("docnum", docnum),
                ("memo", memo),
            ):
                if value:
                    params[name] = str(value)
                    filters_sent.append(name)

            client = session.client()
            result = client.report(target, "TransactionList", params)
            flat = reports_mod.flatten(
                result.payload,
                report_name="TransactionList",
                upstream_partial=False,
                max_rows=max_rows,
            )

            # A filtered request returning zero rows is re-read unfiltered
            # before anything reports "none". The re-check earns its keep on
            # the id and free-text filters, where a value that does not exist
            # returns an empty report rather than an error; the enumerated
            # filters cannot get here through a bad value, because they 400.
            recheck = None
            if not flat.rows and filters_sent:
                bare = {
                    key: value
                    for key, value in params.items()
                    if key not in filters_sent
                }
                unfiltered = reports_mod.flatten(
                    client.report(target, "TransactionList", bare).payload,
                    report_name="TransactionList",
                    upstream_partial=False,
                )
                total = unfiltered.upstream_row_count
                recheck = ZeroRowRecheck(
                    performed=True,
                    unfiltered_row_count=total,
                    interpretation=_explain_zero_rows(
                        filters_sent,
                        total,
                        unfiltered_partial=any(
                            a.kind == reports_mod.ANOMALY_UNRECOGNISED_ROW for a in unfiltered.anomalies
                        ),
                    ),
                )

            # Not 'complete': absence of a cap signal does not prove
            # completeness, and this report publishes no total. An anomaly or
            # a local trim makes it known-partial, which is the stronger
            # statement -- as the report tools say (tools_reports._run).
            completeness = "partial" if (flat.anomalies or flat.client_truncated) else "unknown"

            return make_result(
                TransactionListEnvelope(
                    **_identity(target, result.request_started_at, result.fetched_at),
                    summary=reports_mod.disclosure_sentence(flat, completeness=completeness),
                    report_name=flat.report_name,
                    start_period=flat.start_period,
                    end_period=flat.end_period,
                    currency=flat.currency,
                    columns=[
                        ReportColumnBlock(
                            index=c.index, key=c.key, title=c.title, type=c.type,
                            admitted_by=c.admitted_by,
                        )
                        for c in flat.columns
                    ],
                    excluded_column_keys=flat.excluded_column_keys,
                    rows=[
                        ReportRowBlock(
                            **{
                                **row.to_json(),
                                "cells": [
                                    ReportCellBlock(**cell.to_json())
                                    for cell in row.cells
                                ],
                            }
                        )
                        for row in flat.rows
                    ],
                    count=len(flat.rows),
                    upstream_row_count=flat.upstream_row_count,
                    upstream_cell_count=flat.upstream_cell_count,
                    upstream_partial=flat.upstream_partial,
                    client_truncated=flat.client_truncated,
                    completeness=completeness,
                    anomalies=[
                        ReportAnomalyBlock(**anomaly.to_json()) for anomaly in flat.anomalies
                    ],
                    filters_sent=filters_sent,
                    filterable_transaction_types=list(
                        reports_mod.REPORT_TRANSACTION_TYPES
                    ),
                    filterable_source_account_types=list(
                        reports_mod.REPORT_SOURCE_ACCOUNT_TYPES
                    ),
                    zero_row_recheck=recheck,
                    note=REPORT_NOTE,
                )
            )

    # ----------------------------------------------------------------- 4 ---
    @server.tool(
        name="get_transaction",
        title="Get one transaction with its lines",
        annotations=read_only("Get one transaction with its lines"),
        description=(
            "Read one transaction and its projected lines by type and id. Both come "
            "from a list_transactions row, whose label is a display name: Expense, "
            "Cash Expense, Check and the Credit Card labels are Purchase, the Bill "
            "Payment labels are BillPayment, Refund is RefundReceipt, and the rest "
            "drop their spaces. Estimate, Charge and Time Charge rows have no route. "
            "Use this for one transaction's lines and the report tools for totals "
            "across transactions. "
            "Read-only."
            + FREE_TEXT_DISCLOSURE
        ),
    )
    def get_transaction(
        entity: EntityKey,
        transaction_type: Annotated[
            str, Field(description=f"One of: {', '.join(TRANSACTION_TYPES)}")
        ],
        id: Annotated[str, Field(description="The transaction's QuickBooks id.")],
    ) -> Annotated[CallToolResult, TransactionEnvelope]:
        with tool_errors():
            target = session.resolve(entity)

            canonical = None
            for candidate in TRANSACTION_TYPES:
                if candidate.casefold() == transaction_type.casefold():
                    canonical = candidate
                    break
            if canonical is None:
                raise QueryError(
                    f"transaction_type must be one of: {', '.join(TRANSACTION_TYPES)}"
                )

            session.ensure_ready(target)
            statement = parse(
                f"SELECT * FROM {canonical} WHERE Id = '{escape_literal(str(id))}'"
            )
            result = session.client().query(target, statement.canonical())
            from .pagination import rows_of

            rows = rows_of(result.payload, canonical)
            if not rows:
                raise QboMcpError(
                    f"no {canonical} with that id in this company. An empty result "
                    f"here means the record was not found, not that it has no data."
                )

            return make_result(
                TransactionEnvelope(
                    **_identity(target, result.request_started_at, result.fetched_at),
                    transaction_type=canonical,
                    transaction=TransactionBlock(**project_transaction(rows[0])),
                )
            )

    # ----------------------------------------------------------------- 5 ---
    @server.tool(
        name="run_query",
        title="Run a read-only SELECT",
        annotations=read_only("Run a read-only SELECT"),
        description=(
            "Run one parsed SELECT against a served entity. Use this for filters "
            "the list tools lack and for COUNT(*); for plain lists use "
            "list_accounts, list_vendors, list_customers, list_reference_data or "
            "list_attachments. "
            "Supports "
            "SELECT * and SELECT COUNT(*), WHERE with AND, ORDERBY, "
            "STARTPOSITION and MAXRESULTS. No field lists: the documented grammar "
            "offers only SELECT * and COUNT(*). Served entities: "
            f"{', '.join(served_entities())}. The transaction entities (Bill, "
            "BillPayment, CreditMemo, Deposit, Invoice, JournalEntry, Payment, "
            "Purchase, PurchaseOrder, RefundReceipt, SalesReceipt, Transfer, "
            "VendorCredit) answer COUNT(*) only, and SELECT * on one is refused: "
            "for their rows, use list_transactions and get_transaction. "
            "Read-only."
            + FREE_TEXT_DISCLOSURE
        ),
    )
    def run_query(
        entity: EntityKey,
        statement: Annotated[str, Field(description="The SELECT to run.")],
        with_count: Annotated[
            bool | None,
            Field(description="Also return COUNT(*) for the same WHERE."),
        ] = False,
    ) -> Annotated[CallToolResult, QueryEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            # Parse first. A grammar error should cost nothing: no credential
            # read, no guard fetch, no request. Validating after the guard
            # would answer 'no usable credential' to a malformed statement,
            # which sends a model to fix the wrong thing.
            parsed = parse(statement)
            # The same goes for rows nothing can project: refused here, before a
            # walk reads every page. COUNT(*) returns no rows, so it stays served.
            if not parsed.count_only and not has_projection(parsed.entity):
                raise QueryError(TRANSACTION_ROWS_REFUSED)

            session.ensure_ready(target)
            started = datetime.now(UTC)
            client = session.client()

            if parsed.count_only:
                payload = client.query(target, parsed.canonical()).payload
                total = count_of(payload)
                finished = datetime.now(UTC)
                return make_result(
                    QueryEnvelope(
                        **_identity(target, started, finished),
                        statement=parsed.canonical(),
                        query_entity=parsed.entity,
                        count_only=True,
                        total_count=total,
                        count=0,
                        rows=[],
                        truncated=False,
                        duplicates_dropped=0,
                        pages_walked=1,
                        walk_started_at=started,
                        walk_finished_at=finished,
                    )
                )

            if parsed.start_position is not None or parsed.max_results is not None:
                # An explicit page is honoured as asked, and is not a walk: no
                # completeness claim is made about one page of many.
                payload = client.query(target, parsed.canonical()).payload
                from .pagination import rows_of

                raw = rows_of(payload, parsed.entity)
                finished = datetime.now(UTC)
                rows = [
                    project_entity(parsed.entity, row) for row in raw
                ]
                return make_result(
                    QueryEnvelope(
                        **_identity(target, started, finished),
                        statement=parsed.canonical(),
                        query_entity=parsed.entity,
                        count_only=False,
                        total_count=count_of(
                            client.query(target, parsed.as_count().canonical()).payload
                        )
                        if with_count
                        else None,
                        count=len(rows),
                        rows=rows,
                        truncated=True,
                        duplicates_dropped=0,
                        pages_walked=1,
                        walk_started_at=started,
                        walk_finished_at=finished,
                    )
                )

            walk = session.walk(target, parsed)
            return make_result(
                QueryEnvelope(
                    **_identity(target, started, datetime.now(UTC)),
                    statement=parsed.canonical(),
                    query_entity=parsed.entity,
                    count_only=False,
                    total_count=walk.count_anchor if with_count else None,
                    count=len(walk.rows),
                    rows=[project_entity(parsed.entity, row) for row in walk.rows],
                    truncated=walk.truncated,
                    duplicates_dropped=walk.duplicates_dropped,
                    pages_walked=walk.pages_walked,
                    walk_started_at=walk.walk_started_at,
                    walk_finished_at=walk.walk_finished_at,
                )
            )

    # ----------------------------------------------------------------- 6 ---
    @server.tool(
        name="summarize_vendor_payments",
        title="Candidate vendor payment totals for a year",
        annotations=read_only("Candidate vendor payment totals for a year"),
        description=(
            "Per-vendor CANDIDATE payment totals for a calendar year, for "
            "cross-checking against a bank ledger. Use this for candidate totals of "
            "what was paid; use list_vendors for the 1099 flag and report_aging for "
            "what is owed by age. "
            "Walks Purchase and "
            "BillPayment; JournalEntry lines touching a vendor are returned for "
            "review and never summed. Bills are never added to their payments. "
            "No threshold is applied and no upper bound is claimed -- payment "
            "type is an accounting signal, not evidence of settlement or of any "
            "tax obligation. Read-only."
            + FREE_TEXT_DISCLOSURE
        ),
    )
    def summarize_vendor_payments(
        entity: EntityKey,
        year: Annotated[int, Field(ge=1900, le=2200, description="The calendar year, such as 2025.")],
        vendor: Annotated[str | None, Field(description="Restrict to one vendor id.")] = None,
        include_card: Annotated[
            bool | None,
            Field(description="Show card detail rows. Card totals are reported either way."),
        ] = True,
        detail: Annotated[str | None, Field(description="'summary' or 'rows'.")] = "summary",
    ) -> Annotated[CallToolResult, VendorPaymentsEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            if detail not in (None, "summary", "rows"):
                raise QueryError("detail must be 'summary' or 'rows'")

            ready = session.ensure_ready(target)
            started = datetime.now(UTC)

            # Refused before any request: amounts in different currencies are
            # never added, and there is no validated per-currency path yet.
            vp.require_single_currency(ready.state.capabilities)

            window_start = date(year, 1, 1).isoformat()
            window_end = date(year, 12, 31).isoformat()
            vendor_clause = (
                f" AND EntityRef = '{escape_literal(str(vendor))}'" if vendor else ""
            )

            classification = vp.Classification()
            sources: list[SourceWalked] = []

            purchases = session.walk(
                target,
                parse(
                    f"SELECT * FROM Purchase WHERE TxnDate >= '{window_start}' "
                    f"AND TxnDate <= '{window_end}'{vendor_clause} ORDERBY TxnDate ASC"
                ),
            )
            for row in purchases.rows:
                vp.classify_purchase(row, classification)
            sources.append(SourceWalked(source=vp.SOURCE_PURCHASE, walked=True))

            # Accounts payable is established by first use: the BillPayment
            # read is attempted unless an earlier one for this consent was
            # refused with the feature fault. Never guessed from the chart of
            # accounts.
            accounts_payable = session.accounts_payable(target)
            if accounts_payable == AP_UNAVAILABLE:
                capabilities = ready.state.capabilities
                fault = capabilities.accounts_payable_fault_code if capabilities else None
                sources.append(_bill_payments_not_walked(fault))
            else:
                vendor_bp = (
                    f" AND VendorRef = '{escape_literal(str(vendor))}'" if vendor else ""
                )
                try:
                    bill_payments = session.walk(
                        target,
                        parse(
                            f"SELECT * FROM BillPayment WHERE TxnDate >= '{window_start}' "
                            f"AND TxnDate <= '{window_end}'{vendor_bp} ORDERBY TxnDate ASC"
                        ),
                    )
                except UpstreamError as exc:
                    # Only the feature fault means "no accounts payable". A
                    # subscription fault (6190), a throttle or a 5xx is a
                    # failed call, and fails the call.
                    if exc.fault_code != ACCOUNTS_PAYABLE_FEATURE_FAULT:
                        raise
                    session.note_accounts_payable(target, available=False, fault_code=exc.fault_code)
                    accounts_payable = AP_UNAVAILABLE
                    sources.append(_bill_payments_not_walked(exc.fault_code))
                else:
                    session.note_accounts_payable(target, available=True)
                    accounts_payable = AP_AVAILABLE
                    for row in bill_payments.rows:
                        vp.classify_bill_payment(row, classification)
                    sources.append(SourceWalked(source=vp.SOURCE_BILL_PAYMENT, walked=True))

            journals = session.walk(
                target,
                parse(
                    f"SELECT * FROM JournalEntry WHERE TxnDate >= '{window_start}' "
                    f"AND TxnDate <= '{window_end}' ORDERBY TxnDate ASC"
                ),
            )
            for row in journals.rows:
                vp.classify_journal_entry(row, classification)
            sources.append(
                SourceWalked(
                    source=vp.SOURCE_JOURNAL_ENTRY,
                    walked=True,
                    reason="review only; never summed",
                )
            )

            currency = vp.assert_single_currency(classification.rows)
            index = session.vendor_index(target)
            totals = vp.aggregate(classification.rows, vendor_index=index)

            if vendor:
                totals = [t for t in totals if t.vendor_id == str(vendor)]

            show_card = include_card is not False
            rows_payload = None
            if detail == "rows":
                selected = [
                    row
                    for row in classification.rows
                    if (show_card or not row.is_card)
                    and (vendor is None or row.vendor_id == str(vendor))
                ]
                rows_payload = [CandidatePaymentRow(**r.to_json()) for r in selected]

            return make_result(
                VendorPaymentsEnvelope(
                    **_identity(target, started, datetime.now(UTC)),
                    year=year,
                    currency_basis=currency,
                    sources=sources,
                    accounts_payable=accounts_payable,
                    excluded_payment_types=[],
                    vendors=[
                        CandidateVendorRow(**t.to_json(include_card=show_card))
                        for t in totals
                    ],
                    needs_review=NeedsReview(
                        no_payee=classification.no_payee,
                        journal_entry_vendor_lines=[
                            JournalReviewLine(**line.to_json())
                            for line in classification.review_lines
                            if vendor is None or line.vendor_id == str(vendor)
                        ],
                    ),
                    unclassified=[
                        UnclassifiedRow(**u.to_json()) for u in classification.unclassified
                    ],
                    excluded_summary=classification.excluded,
                    rows=rows_payload,
                    deduplication_note=vp.DEDUPE_NOTE,
                    interpretation_note=vp.NOT_A_CLASSIFIER_NOTE,
                    walk_started_at=started,
                    walk_finished_at=datetime.now(UTC),
                )
            )

    # ----------------------------------------------------------------- 7 ---
    @server.tool(
        name="list_changes",
        title="List recent changes and deletions",
        annotations=read_only("List recent changes and deletions"),
        description=(
            "List objects changed or deleted since a timestamp within the last "
            "30 days, for supported types. Use this for changes and deletions on "
            "list objects, and the list tools for the current objects; run_query on a "
            "timestamp cannot see deletions, and transactions, tax codes and tax "
            "rates are outside this tool. The "
            "1000-object cap is shared across "
            "ALL requested types, so saturation makes every requested type "
            "uncertain. A possibly incomplete response is not a safe "
            "synchronization checkpoint. Read-only."
            + FREE_TEXT_DISCLOSURE
        ),
    )
    def list_changes(
        entity: EntityKey,
        since: Annotated[str, Field(description="ISO timestamp, within 30 days.")],
        types: Annotated[
            list[str] | None, Field(description="Account, Attachable, Class, Customer, Department, Item, PaymentMethod, Term or Vendor; defaults to Vendor, Customer and Account.")
        ] = None,
        full: Annotated[
            bool | None, Field(description="Include the projected object.")
        ] = False,
        limit: Annotated[
            int | None,
            Field(ge=1, le=MAX_LIMIT, description="Changed objects returned; the rest are trimmed locally and disclosed."),
        ] = DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, ChangesEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            moment = _parse_since(since)
            requested = _resolve_change_types(types)

            session.ensure_ready(target)
            result = session.client().changes(
                target, entities=requested, changed_since=moment
            )
            parsed_objects, upstream_total = _parse_cdc(result.payload, requested, bool(full))

            possibly_truncated = upstream_total >= CHANGES_OBJECT_CAP
            client_limit = _limit(limit)
            returned = parsed_objects[:client_limit]
            client_truncated = len(parsed_objects) > len(returned)

            if possibly_truncated:
                completeness = "unknown"
                reason = (
                    f"upstream returned {upstream_total} objects across all "
                    f"requested types, reaching the shared {CHANGES_OBJECT_CAP}-object "
                    f"cap. Objects may have been withheld, and the cap is shared, so "
                    f"no requested type can be shown complete."
                )
                uncertain = list(requested)
            elif client_truncated:
                completeness = "partial"
                reason = (
                    f"upstream returned {upstream_total} objects and this server "
                    f"returned the first {len(returned)}. Nothing upstream was "
                    f"withheld; raise limit to see the rest."
                )
                uncertain = []
            else:
                completeness = "complete"
                reason = (
                    f"upstream returned {upstream_total} objects, below the shared "
                    f"{CHANGES_OBJECT_CAP}-object cap, and all were returned."
                )
                uncertain = []

            return make_result(
                ChangesEnvelope(
                    **_identity(target, result.request_started_at, result.fetched_at),
                    since=moment,
                    types=list(requested),
                    objects=returned,
                    upstream_object_count=upstream_total,
                    returned_count=len(returned),
                    truncated=client_truncated,
                    possibly_truncated=possibly_truncated,
                    completeness=completeness,
                    completeness_reason=reason,
                    uncertain_types=uncertain,
                    checkpoint_safe=(completeness == "complete"),
                    note=CHANGES_UNSAFE_NOTE,
                )
            )


# --------------------------------------------------------------------------
# changes helpers
# --------------------------------------------------------------------------


def _parse_since(since: str) -> str:
    try:
        moment = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise QueryError("since must be an ISO timestamp, e.g. 2026-09-01T00:00:00Z") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    oldest = datetime.now(UTC) - timedelta(days=CHANGES_LOOKBACK_DAYS)
    if moment < oldest:
        raise QueryError(
            f"change data capture looks back {CHANGES_LOOKBACK_DAYS} days and no "
            f"further. A query on a modification timestamp is not an equivalent "
            f"fallback: it cannot recover deletions, which leave no row to match."
        )
    if moment > datetime.now(UTC):
        raise QueryError("since is in the future")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _resolve_change_types(types: Sequence[str] | None) -> tuple[str, ...]:
    from .projections import has_projection

    default = ("Vendor", "Customer", "Account")
    if not types:
        return default

    resolved: list[str] = []
    for position, name in enumerate(types):
        match = None
        for served in served_entities():
            if served.casefold() == str(name).casefold():
                match = served
                break
        if match is None:
            # Named by position, never by value.
            raise QueryError(
                f"types[{position}] is not a supported change type. Supported: "
                f"{', '.join(served_entities())}"
            )
        if not has_projection(match):
            raise QueryError(
                f"'{match}' has no completed projection, so its changes cannot be "
                f"returned. A type needs a reviewed projection before any route "
                f"can reach it."
            )
        if match in CDC_UNSUPPORTED:
            raise QueryError(
                f"'{match}' is outside change data capture: Intuit documents it for all "
                f"objects except JournalCode, TimeActivity, TaxAgency, TaxCode and TaxRate. "
                f"Use list_reference_data or run_query for it. No request was made."
            )
        resolved.append(match)
    return tuple(dict.fromkeys(resolved))


def _parse_cdc(
    payload: Mapping[str, Any], requested: Sequence[str], full: bool
) -> tuple[list[ChangedObject], int]:
    """Parse a CDC response, counting objects across ALL types.

    The cap is an aggregate, so the count that matters is the total across
    every type in the response -- 600 of one and 400 of another saturates it
    with neither reaching 1000.

    Every level has to have its documented shape, and one that doesn't is
    refused, never skipped: a skipped entry leaves a count that looks complete,
    and a complete count is what makes the result a safe checkpoint. In sandbox,
    the response was one block holding one entry per requested type, and a type
    with no changes arrived as an empty entry (docs/api-findings.md, item 7),
    which is not a malformed one. A type no entry accounts for would read as
    unchanged, so the entries must account for each requested type once and
    hold nothing but a type's objects and :data:`CDC_PAGING_FIELDS`. A response
    with no block, or a block with no entries, says nothing about the requested
    types, so it is refused too. So is an object with no id, or with a
    ``MetaData`` that isn't an object, and one whose id, status or timestamp is
    not a single value, since each of those becomes text.
    """
    blocks = payload.get("CDCResponse")
    if not isinstance(blocks, list) or not blocks:
        raise QboMcpError(CDC_MALFORMED_TEXT)

    objects: list[ChangedObject] = []
    total = 0
    entries = 0
    named: set[str] = set()

    for block in blocks:
        responses = block.get("QueryResponse") if isinstance(block, Mapping) else None
        if not isinstance(responses, list) or not responses:
            raise QboMcpError(CDC_MALFORMED_TEXT)
        for response in responses:
            if not isinstance(response, Mapping):
                raise QboMcpError(CDC_MALFORMED_TEXT)
            entries += 1
            types_here = [key for key in response if key in requested]
            if len(types_here) > 1 or named.intersection(types_here):
                raise QboMcpError(CDC_MALFORMED_TEXT)
            named.update(types_here)
            for key, value in response.items():
                if key in CDC_PAGING_FIELDS:
                    if not is_scalar(value):
                        raise QboMcpError(CDC_MALFORMED_TEXT)
                    continue
                if key not in requested:
                    if isinstance(value, (list, Mapping)):
                        # Fail closed: a type nobody asked for has not been
                        # reviewed for this response either.
                        raise QboMcpError(
                            "the change response carried a type that was not "
                            "requested; refusing to return unreviewed objects."
                        )
                    # Anything else could change what the entry means.
                    raise QboMcpError(CDC_MALFORMED_TEXT)
                if not isinstance(value, list):
                    raise QboMcpError(CDC_MALFORMED_TEXT)
                for item in value:
                    if not isinstance(item, Mapping):
                        raise QboMcpError(CDC_MALFORMED_TEXT)
                    total += 1
                    metadata = item.get("MetaData")
                    if metadata is not None and not isinstance(metadata, Mapping):
                        raise QboMcpError(CDC_MALFORMED_TEXT)
                    last_updated = metadata.get("LastUpdatedTime") if metadata is not None else None
                    status = item.get("status")
                    item_id = item.get("Id")
                    if item_id is None or item_id == "":
                        # Every change Intuit documents names its object, a
                        # deletion included.
                        raise QboMcpError(CDC_MALFORMED_TEXT)
                    if any(
                        part is not None and not is_scalar(part)
                        for part in (item_id, status, last_updated)
                    ):
                        # Each becomes text below, and the text of an object
                        # carries every field nested in it past the projector.
                        raise QboMcpError(CDC_MALFORMED_TEXT)
                    row = None
                    if full and not status:
                        row = project_entity(key, item, include_metadata=True)
                    objects.append(
                        ChangedObject(
                            entity_type=key,
                            id=str(item_id),
                            status=str(status) if status else None,
                            last_updated_time=(
                                str(last_updated) if last_updated else None
                            ),
                            row=row,
                        )
                    )
    if entries != len(requested):
        raise QboMcpError(CDC_MALFORMED_TEXT)
    return objects, total
