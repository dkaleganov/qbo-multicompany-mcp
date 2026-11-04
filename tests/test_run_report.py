"""`run_report`.

One tool over the 21 documented reports without a named tool. Every test
drives it through the registered server against the loopback mock, so the
client, the network policy and the envelope are the real ones; a refusal
happens BEFORE any report request (``mock_intuit.report_queries`` unchanged).
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from qbo_multicompany_mcp import reports as R
from qbo_multicompany_mcp.netpolicy import ACCOUNTING_PATH_TAILS, ACCOUNTING_QUERY_KEYS_BY_TAIL
from qbo_multicompany_mcp.query_grammar import QueryError
from qbo_multicompany_mcp.report_catalog import CATALOG

from .conftest import MockIntuit
from .fixtures import report_shapes as S
from .fixtures import report_shapes_run_report as Q
from .test_changes_and_tools import build, call
from .test_report_tools import HOSTILE, body_of, refused, sent

#: The run_report enum: 21 tool-facing names, disjoint from the reports the
#: named tools serve.
ENUM = (
    "AccountListDetail", "CashFlow", "CustomerBalance", "CustomerBalanceDetail", "CustomerIncome",
    "InventoryValuationDetail", "InventoryValuationSummary", "JournalReport", "ProfitAndLossDetail",
    "SalesByClassSummary", "SalesByCustomer", "SalesByDepartment", "SalesByProduct", "TaxSummary",
    "TransactionListByCustomer", "TransactionListByVendor", "TransactionListWithSplits", "TrialBalance",
    "VendorBalance", "VendorBalanceDetail", "VendorExpenses",
)

#: Tool-facing name -> the live path Intuit's page names (nine differ from
#: their page titles, confirmed live on 2026-09-24).
PATHS = {
    "AccountListDetail": "AccountList", "SalesByClassSummary": "ClassSales", "SalesByCustomer": "CustomerSales",
    "SalesByDepartment": "DepartmentSales", "SalesByProduct": "ItemSales",
}

#: Detail members under the six-month limit, and the window each carries.
DETAIL_DATE_WINDOW = ("ProfitAndLossDetail", "TransactionListByCustomer", "TransactionListByVendor",
                      "TransactionListWithSplits", "JournalReport", "InventoryValuationDetail")
DETAIL_DUEDATE_WINDOW = ("CustomerBalanceDetail", "VendorBalanceDetail")

FIXTURES = {
    "AccountList": Q.account_list, "CashFlow": Q.cash_flow, "CustomerBalance": Q.customer_balance,
    "CustomerBalanceDetail": Q.customer_balance_detail, "CustomerIncome": Q.customer_income,
    "InventoryValuationDetail": S.inventory_valuation_detail_with_rows,
    "InventoryValuationSummary": Q.inventory_valuation_summary, "JournalReport": Q.journal_report,
    "ProfitAndLossDetail": Q.profit_and_loss_detail, "ClassSales": Q.class_sales,
    "CustomerSales": Q.customer_sales, "DepartmentSales": Q.department_sales, "ItemSales": S.item_sales,
    "TaxSummary": Q.tax_summary, "TransactionListByCustomer": Q.transaction_list_by_customer,
    "TransactionListByVendor": Q.transaction_list_by_vendor,
    "TransactionListWithSplits": Q.transaction_list_with_splits, "TrialBalance": S.trial_balance,
    "VendorBalance": Q.vendor_balance, "VendorBalanceDetail": Q.vendor_balance_detail,
    "VendorExpenses": Q.vendor_expenses,
}

#: One accepted argument set per enum value, enough to make a request.
MINIMAL: dict[str, dict[str, Any]] = {
    **{name: {} for name in ENUM},
    **{name: {"start_date": "2026-01-01", "end_date": "2026-06-30"} for name in DETAIL_DATE_WINDOW},
    "TaxSummary": {"agency_id": "1"},
}


def path_of(name: str) -> str:
    return PATHS.get(name, name)


def seed_all(mock: MockIntuit) -> None:
    for path, builder in FIXTURES.items():
        mock.reports[path] = builder()


@pytest.fixture
def server(mock_settings, registry, credentials, actor, mock_intuit: MockIntuit) -> MCPServer:
    seed_all(mock_intuit)
    return build(mock_settings, registry, credentials, actor)


def run(server, mock, name: str, **args) -> dict[str, Any]:
    return body_of(call(server, "run_report", {"entity": "acme_main", "report": name, **MINIMAL[name], **args}))


# ==========================================================================
# The surface and the map
# ==========================================================================


def test_run_report_is_registered_with_the_21_report_enum(server: MCPServer) -> None:
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert "run_report" in tools
    schema = tools["run_report"].input_schema
    assert set(schema["properties"]["report"]["enum"]) == set(ENUM)
    assert len(ENUM) == 21
    assert "report" in schema["required"] and "entity" in schema["required"]


def test_the_enum_is_disjoint_from_the_named_tools_reports() -> None:
    """run_report's enum is disjoint from the named tools, which serve
    ProfitAndLoss, BalanceSheet, GeneralLedger, the four Aged* reports and
    TransactionList."""
    named = {"ProfitAndLoss", "BalanceSheet", "GeneralLedger", "APAgingSummary", "ARAgingSummary",
             "APAgingDetail", "ARAgingDetail", "TransactionList"}
    assert not named & set(ENUM)
    assert not {path_of(n) for n in ENUM} & {R.REPORT_ENDPOINTS[n] for n in named if n in R.REPORT_ENDPOINTS}


@pytest.mark.parametrize("name", ENUM)
def test_every_enum_value_resolves_to_a_path_intuits_page_names(name: str) -> None:
    """Readable tool-facing names, the paths Intuit's pages name underneath.
    Each path is on the network allowlist by exact name with its own
    query-key set, in the send-side inventory, and has a column shape."""
    from qbo_multicompany_mcp.report_catalog import CATALOG
    from qbo_multicompany_mcp.tools_reports import REPORT_PARAMETER_INVENTORY

    path = path_of(name)
    assert R.REPORT_ENDPOINTS[name] == path
    assert f"reports/{path}" in ACCOUNTING_PATH_TAILS
    assert f"reports/{path}" in ACCOUNTING_QUERY_KEYS_BY_TAIL
    assert path in REPORT_PARAMETER_INVENTORY
    assert path in R.REPORT_SHAPES
    spec = CATALOG[name]
    assert spec.endpoint == path


@pytest.mark.parametrize("name", ENUM)
def test_the_parameters_each_report_exposes_are_inside_its_documented_parameters(name: str) -> None:
    """CashFlow and TrialBalance are not exempt. The tool's set is a subset
    of the documented parameters, and the network policy's per-tail set is
    exactly that subset plus minorversion."""
    from qbo_multicompany_mcp.report_catalog import CATALOG
    from qbo_multicompany_mcp.tools_reports import REPORT_PARAMETER_INVENTORY

    spec = CATALOG[name]
    inventory = REPORT_PARAMETER_INVENTORY[spec.endpoint]
    assert spec.parameters <= inventory, sorted(spec.parameters - inventory)
    assert ACCOUNTING_QUERY_KEYS_BY_TAIL[f"reports/{spec.endpoint}"] == spec.parameters | {"minorversion"}
    for never in ("qzurl", "custom1", "custom2", "custom3", "shipvia", "adjusted_gain_loss",
                  "bothamount", "printed", "start_moddate", "moddate_macro", "createdate_macro", "name"):
        assert never not in spec.parameters, never


@pytest.mark.parametrize("name", ENUM)
def test_every_report_answers_with_the_report_contract(server, mock_intuit, name: str) -> None:
    """The identity fields, the summary first, max_rows with completeness, on
    every success; `report` is the tool-facing name and `report_name` the
    path that answered."""
    body = run(server, mock_intuit, name)
    assert body["tool"] == "run_report"
    assert body["report"] == name and body["report_name"] == path_of(name)
    assert sent(mock_intuit)[0] == path_of(name)
    for key in ("entity", "environment", "display_name_from_registry", "realm_id_last4",
                "request_started_at", "fetched_at", "completeness", "anomalies", "columns", "rows"):
        assert key in body, key
    assert body["anomalies"] == [], "the observed shape of a report is never an anomaly"
    assert body["excluded_column_keys"] == [], "the observed columns are the allowlist"
    assert body["completeness"] in ("unknown", "partial")
    assert body["count"] == len(body["rows"]) and body["count"] > 0


def test_max_rows_trims_locally_on_run_report(server, mock_intuit) -> None:
    body = run(server, mock_intuit, "JournalReport", max_rows=1)
    assert body["client_truncated"] is True and body["count"] == 1 and body["completeness"] == "partial"


def test_a_report_outside_the_enum_is_refused_without_being_echoed(server, mock_intuit) -> None:
    from qbo_multicompany_mcp.tools_run_report import _check_report

    refused(server, mock_intuit, "run_report", {"entity": "acme_main", "report": HOSTILE}, "report")
    with pytest.raises(QueryError) as caught:
        _check_report(HOSTILE)
    assert HOSTILE not in str(caught.value)
    # A NAMED tool's report cannot be reached through run_report, and the
    # refusal names the tool that serves it.
    refused(server, mock_intuit, "run_report", {"entity": "acme_main", "report": "ProfitAndLoss"}, "report")
    with pytest.raises(QueryError, match="report_profit_and_loss"):
        _check_report("ProfitAndLoss")
    with pytest.raises(QueryError, match="list_transactions"):
        _check_report("TransactionList")


# ==========================================================================
# Per-report parameter allowlists (the documented parameters)
# ==========================================================================


@pytest.mark.parametrize(
    ("name", "parameter", "value"),
    [
        ("CashFlow", "accounting_method", "Cash"),          # CashFlow has no accounting_method
        ("CashFlow", "columns", ["total"]),                  # nor columns
        ("VendorBalance", "customer", ["1"]),                # a vendor report takes no customer
        ("CustomerBalance", "vendor", ["1"]),
        ("TrialBalance", "columns", ["debit"]),              # TrialBalance has no columns
        ("SalesByClassSummary", "sort_order", "ascend"),     # ClassSales lists no sort_order
        ("JournalReport", "accounting_method", "Cash"),      # JournalReport lists none
        ("CustomerBalanceDetail", "date_macro", "Today"),    # CBD lists no date_macro
        ("AccountListDetail", "date_macro", "Today"),        # AccountList lists none
        ("TransactionListWithSplits", "memo", "x"),          # WithSplits lists no memo
        ("TransactionListWithSplits", "cleared", "Cleared"),
        ("InventoryValuationSummary", "accounting_method", "Cash"),
    ],
)
def test_a_parameter_the_reports_inventory_does_not_list_is_refused_by_name(server, mock_intuit, name, parameter, value) -> None:
    text = refused(server, mock_intuit, "run_report",
                   {"entity": "acme_main", "report": name, **MINIMAL[name], parameter: value},
                   f"'{parameter}' is not accepted by {name}")
    assert "PRIVATE" not in text


def test_trial_balance_takes_accounting_method_and_cash_flow_does_not(server, mock_intuit) -> None:
    """Both are reachable only here and neither is exempt from the parameter
    allowlist."""
    body = run(server, mock_intuit, "TrialBalance", accounting_method="Cash")
    assert sent(mock_intuit)[1]["accounting_method"] == "Cash"
    assert body["accounting_method_source"] == "caller"
    body = run(server, mock_intuit, "TrialBalance")
    assert sent(mock_intuit)[1]["accounting_method"] == "Accrual"
    assert body["accounting_method_source"] == "company_report_basis"
    body = run(server, mock_intuit, "CashFlow")
    assert "accounting_method" not in sent(mock_intuit)[1]
    assert body["accounting_method_sent"] is None


def test_id_filters_and_summarise_go_out_where_the_inventory_lists_them(mock_settings, registry, credentials, actor, mock_intuit) -> None:
    from .test_report_tools import with_tracking

    with_tracking(mock_intuit)
    seed_all(mock_intuit)
    server = build(mock_settings, registry, credentials, actor)
    body = run(server, mock_intuit, "SalesByCustomer", customer=["1"], item=["2"], **{"class": ["3"]},
               department=["4"], summarize_column_by="Month", sort_order="descend", accounting_method="Cash")
    _, query = sent(mock_intuit)
    assert query == {"customer": "1", "item": "2", "class": "3", "department": "4", "summarize_column_by": "Month",
                     "sort_order": "descend", "accounting_method": "Cash"}
    assert sorted(body["capabilities_checked"]) == ["class_tracking", "department_tracking"]
    body = run(server, mock_intuit, "ProfitAndLossDetail", account=["5", "6"], account_type="Bank",
               payment_method="Visa", employee=["8"], vendor=["9"])
    _, query = sent(mock_intuit)
    assert query["account"] == "5,6" and query["account_type"] == "Bank" and query["payment_method"] == "Visa"
    assert query["employee"] == "8" and query["vendor"] == "9"


@pytest.mark.parametrize("filter_name", ["class", "department"])
def test_an_untracked_class_or_department_filter_is_refused_not_dropped(server, mock_intuit, filter_name) -> None:
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": "SalesByCustomer", filter_name: ["1"]}, "not enabled")


def test_tax_summary_requires_agency_id_and_maps_fault_5020(server, mock_intuit) -> None:
    """Intuit's TaxSummary page marks agency_id Required -- the only required
    parameter on any of the 29 report pages. It stays in the enum: the sandbox
    company refused it with 5020 (2026-09-24) and another company may serve
    it. The refusal is the fixed, actionable 5020 error, never an empty
    report."""
    refused(server, mock_intuit, "run_report", {"entity": "acme_main", "report": "TaxSummary"}, "agency_id")
    body = run(server, mock_intuit, "TaxSummary")
    assert sent(mock_intuit)[1]["agency_id"] == "1"
    assert body["disclosures"] and any("documentation" in d for d in body["disclosures"]), (
        "TaxSummary's column shape is documentation-derived and says so")
    mock_intuit.report_faults["TaxSummary"] = (400, S.fault_body("5020"))
    with pytest.raises(ToolError) as caught:
        call(server, "run_report", {"entity": "acme_main", "report": "TaxSummary", "agency_id": "1"})
    assert "fault=5020" in str(caught.value) and "not an empty report" in str(caught.value)


# ==========================================================================
# Windows: the six-month limit on every detail member, dates and macros
# ==========================================================================


@pytest.mark.parametrize("name", DETAIL_DATE_WINDOW)
def test_every_date_windowed_detail_member_carries_the_six_month_limit(server, mock_intuit, name) -> None:
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": name, "start_date": "2026-01-01", "end_date": "2026-07-01"}, "six")
    refused(server, mock_intuit, "run_report", {"entity": "acme_main", "report": name}, "window")
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": name, "start_date": "2026-01-01", "date_macro": "This Month"},
            "mutually exclusive")
    body = run(server, mock_intuit, name)
    assert sent(mock_intuit)[1]["end_date"] == "2026-06-30"
    assert body["completeness"] == "unknown"


@pytest.mark.parametrize("name", DETAIL_DATE_WINDOW)
def test_a_macro_on_a_detail_member_is_resolved_and_held_to_the_six_month_limit(server, mock_intuit, name) -> None:
    """The six-month limit covers every detail report here, on a resolved
    macro as on explicit dates. A fiscal year is refused; a fiscal quarter
    goes."""
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": name, "date_macro": "This Fiscal Year"}, "six")
    body = body_of(call(server, "run_report", {"entity": "acme_main", "report": name, "date_macro": "Last Fiscal Quarter"}))
    _, query = sent(mock_intuit)
    assert query["date_macro"] == "Last Fiscal Quarter" and "start_date" not in query
    assert body["date_macro_sent"] == "Last Fiscal Quarter" and body["resolved_start"] and body["resolved_end"]


@pytest.mark.parametrize("name", ["TransactionListByCustomer", "TransactionListByVendor"])
def test_the_two_transaction_list_pages_admit_the_calendar_macros_and_the_others_do_not(server, mock_intuit, name) -> None:
    """Intuit's TransactionListByCustomer and TransactionListByVendor pages
    list 33 values; every other page with a macro lists 23. The enum differs
    per report, so 'This Calendar Quarter' goes on these two and is refused
    on the profit and loss detail."""
    body = body_of(call(server, "run_report", {"entity": "acme_main", "report": name, "date_macro": "Last Calendar Quarter"}))
    assert body["date_macro_sent"] == "Last Calendar Quarter"
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": "ProfitAndLossDetail", "date_macro": "Last Calendar Quarter"}, "date_macro")
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": name, "date_macro": "Next Calendar Year"}, "six")


@pytest.mark.parametrize("name", DETAIL_DUEDATE_WINDOW)
def test_the_balance_details_carry_the_limit_on_their_due_date_window(server, mock_intuit, name) -> None:
    """Like the aging details: the range parameters are start_duedate and
    end_duedate; both or neither; at most six months; report_date is the
    as-of date."""
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": name, "start_duedate": "2026-01-01", "end_duedate": "2026-07-01"}, "six")
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": name, "start_duedate": "2026-01-01"}, "end_duedate")
    run(server, mock_intuit, name, start_duedate="2026-01-01", end_duedate="2026-06-30", report_date="2026-06-30")
    _, query = sent(mock_intuit)
    assert (query["start_duedate"], query["end_duedate"], query["report_date"]) == ("2026-01-01", "2026-06-30", "2026-06-30")
    assert query["columns"].startswith("tx_date,txn_type,doc_num,due_date"), "the observed default columns"
    # VendorBalanceDetail lists accounting_method and gets the company basis;
    # CustomerBalanceDetail lists none and gets nothing.
    assert ("accounting_method" in query) == (name == "VendorBalanceDetail")
    run(server, mock_intuit, name)


def test_summary_reports_take_a_window_or_a_macro_with_no_six_month_limit(server, mock_intuit) -> None:
    run(server, mock_intuit, "VendorExpenses", start_date="2025-01-01", end_date="2025-12-31")
    assert sent(mock_intuit)[1]["end_date"] == "2025-12-31"
    run(server, mock_intuit, "VendorExpenses", date_macro="Last Fiscal Year")
    assert sent(mock_intuit)[1]["date_macro"] == "Last Fiscal Year"
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": "VendorExpenses", "date_macro": "Last Fiscal Year", "end_date": "2025-12-31"},
            "mutually exclusive")


def test_as_of_reports_take_report_date_or_a_macro_not_both(server, mock_intuit) -> None:
    run(server, mock_intuit, "CustomerBalance", report_date="2026-06-30")
    assert sent(mock_intuit)[1] == {"report_date": "2026-06-30", "accounting_method": "Accrual"}
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": "CustomerBalance", "report_date": "2026-06-30", "date_macro": "Today"},
            "mutually exclusive")


def test_days_is_checked_through_the_resolver_on_run_report(server, mock_intuit) -> None:
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": "CashFlow", "date_macro": "This Fiscal Year", "summarize_column_by": "Days"}, "200")
    run(server, mock_intuit, "CashFlow", date_macro="This Month", summarize_column_by="Days")


# ==========================================================================
# Columns: the privacy allowlist on the request AND the returned cells
# ==========================================================================


@pytest.mark.parametrize(
    ("name", "column"),
    [
        # TransactionListByCustomer takes no columns at all: its cust_msg
        # case is in the refusal test at the end of this file.
        ("TransactionListByVendor", "cust_msg"),
        ("CustomerBalanceDetail", "cust_bill_email"),
        ("CustomerBalanceDetail", "bill_addr"),
        ("CustomerBalanceDetail", "cust_tel"),
        ("VendorBalanceDetail", "vend_pri_email"),
        ("VendorBalanceDetail", "vend_bill_addr"),
        ("JournalReport", "acct_num_with_extn"),
        ("AccountListDetail", "create_by"),
    ],
)
def test_a_sensitive_or_unreviewed_column_cannot_be_requested(server, mock_intuit, name, column) -> None:
    """Intuit's aging and detail column lists include address, email,
    telephone and account-number columns, and none may be requested. Refused
    by position, never by value."""
    text = refused(server, mock_intuit, "run_report",
                   {"entity": "acme_main", "report": name, **MINIMAL[name], "columns": [column]}, r"columns\[0\]")
    assert column not in R.allowed_column_keys(path_of(name))


@pytest.mark.parametrize(
    ("name", "column"),
    [("TransactionListByCustomer", "cust_msg"), ("VendorBalanceDetail", "vend_pri_tel"),
     ("JournalReport", "acct_num_with_extn"), ("CustomerBalanceDetail", "ship_addr")],
)
def test_a_sensitive_column_that_arrives_anyway_is_withheld_and_named(server, mock_intuit, name, column) -> None:
    """The same allowlist on the returned cells: a column the request never
    asked for, arriving in the response, is excluded and named, and its
    cells never leave."""
    from .test_report_shapes import coldata_lists

    payload = FIXTURES[path_of(name)]()
    payload["Columns"]["Column"].append(S.col(column, "String", "Mystery"))
    for cells in coldata_lists(payload["Rows"]):
        cells.append(S.cell("PRIVATE-CELL-CANARY"))
    mock_intuit.reports[path_of(name)] = payload
    body = run(server, mock_intuit, name)
    assert body["excluded_column_keys"] == [column]
    assert "PRIVATE-CELL-CANARY" not in str(body)
    assert body["anomalies"] == []


def test_requested_columns_are_allowlisted_and_routing_columns_always_included(server, mock_intuit) -> None:
    run(server, mock_intuit, "JournalReport", columns=["memo", "account_name"])
    assert set(sent(mock_intuit)[1]["columns"].split(",")) == {"memo", "account_name", "tx_date", "txn_type"}
    text = refused(server, mock_intuit, "run_report",
                   {"entity": "acme_main", "report": "JournalReport", **MINIMAL["JournalReport"], "columns": ["memo", HOSTILE]},
                   r"columns\[1\]")
    assert HOSTILE not in text
    many = [f"k{i}" for i in range(25)]
    refused(server, mock_intuit, "run_report",
            {"entity": "acme_main", "report": "JournalReport", **MINIMAL["JournalReport"], "columns": many}, "25")


def test_a_detail_report_sends_its_default_columns_when_none_are_asked(server, mock_intuit) -> None:
    run(server, mock_intuit, "ProfitAndLossDetail")
    assert sent(mock_intuit)[1]["columns"].split(",") == [
        "tx_date", "txn_type", "doc_num", "name", "memo", "split_acc", "subt_nat_amount", "rbal_nat_amount",
    ], "the observed default set, in the observed order"
    run(server, mock_intuit, "CashFlow")
    assert "columns" not in sent(mock_intuit)[1], "a report without a columns parameter is sent none"


# ==========================================================================
# Shapes: TrialBalance by title; routing ids; the grouping columns
# ==========================================================================


def test_trial_balance_admits_debit_and_credit_by_title_under_synthetic_keys(server, mock_intuit) -> None:
    """TrialBalance has no ColKey on any column. The label is admitted by
    position; the two Money columns are Debit and Credit by ColTitle, admitted
    by title under `debit` and `credit`, and the column says so."""
    body = run(server, mock_intuit, "TrialBalance")
    columns = {c["key"]: c for c in body["columns"]}
    assert set(columns) == {"account", "debit", "credit"}
    assert columns["debit"]["synthetic_key"] is True and columns["debit"]["admitted_by"] == "title"
    assert columns["debit"]["title"] == "Debit" and columns["debit"]["leaf_key"] is None
    assert columns["account"]["admitted_by"] == "label_position"
    assert body["excluded_column_keys"] == []
    row = next(r for r in body["rows"] if r["row_type"] == "data")
    cells = {c["column_key"]: c for c in row["cells"]}
    assert cells["account"]["entity_id"] and cells["debit"]["amount"]


def test_detail_rows_route_and_summary_labels_carry_ids(server, mock_intuit) -> None:
    body = run(server, mock_intuit, "JournalReport")
    row = next(r for r in body["rows"] if r["row_type"] == "data")
    typed = next(c for c in row["cells"] if c["column_key"] == "txn_type")
    assert typed["entity_id"], "the transaction id rides on the type cell for get_transaction"
    body = run(server, mock_intuit, "VendorExpenses")
    labelled = next(r for r in body["rows"] if r["row_type"] == "data")
    assert labelled["cells"][0]["column_key"] == "vendor" and labelled["cells"][0]["entity_id"]


def test_an_entity_grouping_on_run_report_admits_the_id_columns(server, mock_intuit) -> None:
    payload = S.profit_and_loss_by_customers()
    payload["Header"]["ReportName"] = "CustomerSales"
    payload["Columns"]["Column"][0] = S.col(None, "Customer", "")
    mock_intuit.reports["CustomerSales"] = payload
    body = run(server, mock_intuit, "SalesByCustomer", summarize_column_by="Customers")
    columns = {c["key"]: c for c in body["columns"]}
    assert columns["2"]["entity_type"] == "Customer" and body["excluded_column_keys"] == []


def test_run_report_carries_the_free_text_disclosure() -> None:
    from qbo_multicompany_mcp.tools import FREE_TEXT_DISCLOSURE
    from qbo_multicompany_mcp.tools_run_report import DESCRIPTION

    assert FREE_TEXT_DISCLOSURE in DESCRIPTION


# ==========================================================================
# Percent columns declared Money (observed 2026-09-25)
# ==========================================================================


def test_sales_by_product_percent_columns_carry_unit_percent_and_make_no_anomaly(server, mock_intuit) -> None:
    """Observed 2026-09-25 on both sandbox companies: ItemSales declares
    '% of Sales' and 'Gross Margin %' as Money and their cells read '30.69 %'.
    Read as money, every SalesByProduct result is PARTIAL. Over the wire a
    percent cell says ``unit: percent`` with no currency; a money cell says no
    unit and keeps its currency; the result is not PARTIAL."""
    body = run(server, mock_intuit, "SalesByProduct")
    assert body["anomalies"] == []
    assert not body["summary"].startswith("PARTIAL")
    cells = {c["column_key"]: c for c in body["rows"][0]["cells"]}
    assert cells["total/PercentSales"]["unit"] == "percent"
    assert cells["total/PercentSales"]["amount"] == "50.00"
    assert cells["total/PercentSales"]["currency"] is None
    assert cells["total/GrossMarginPerc"]["unit"] == "percent"
    assert cells["total/Amount"]["unit"] is None
    assert cells["total/Amount"]["currency"] == "USD"


# ==========================================================================
# Quantity columns declared Money
# ==========================================================================


@pytest.mark.parametrize(
    ("report", "key", "money_key"),
    [
        pytest.param("InventoryValuationSummary", "Quantity", "HomeAmount", id="InventoryValuationSummary"),
        pytest.param("SalesByProduct", "total/Quantity", "total/Amount", id="SalesByProduct"),
    ],
)
def test_a_quantity_cell_carries_its_number_and_no_currency(
    server, mock_intuit, report: str, key: str, money_key: str
) -> None:
    """Both reports declare their quantity column ColType Money (observed in
    sandbox on 2026-09-24). Decided by type, ten units go out as
    {"amount": "10.00", "currency": "USD"}. Over the tool: the number, no
    currency, no unit, no anomaly; the money column beside it keeps its
    currency."""
    body = run(server, mock_intuit, report)
    assert body["anomalies"] == []
    assert not body["summary"].startswith("PARTIAL")
    cells = {c["column_key"]: c for c in body["rows"][0]["cells"]}
    assert cells[key]["amount"] is not None
    assert cells[key]["currency"] is None
    assert cells[key]["unit"] is None
    assert cells[money_key]["currency"] == "USD"


# ==========================================================================
# Report id filters take lists
# ==========================================================================


def test_every_run_report_id_filter_takes_a_list_and_sends_one_comma_separated_value(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """Each of Intuit's report pages that lists customer, vendor, class,
    department, item, employee or account documents it as one or more comma
    separated ids, so each takes a list."""
    from .test_report_tools import with_tracking

    with_tracking(mock_intuit)
    seed_all(mock_intuit)
    server = build(mock_settings, registry, credentials, actor)
    run(server, mock_intuit, "SalesByCustomer", customer=["1", "2"], item=["3", "4"],
        **{"class": ["5", "6"]}, department=["7", "8"])
    _, query = sent(mock_intuit)
    assert (query["customer"], query["item"], query["class"], query["department"]) == ("1,2", "3,4", "5,6", "7,8")
    run(server, mock_intuit, "ProfitAndLossDetail", employee=["8", "9"], vendor=["1", "2"], account=["5", "6"])
    _, query = sent(mock_intuit)
    assert (query["employee"], query["vendor"], query["account"]) == ("8,9", "1,2", "5,6")


# ==========================================================================
# A report's request carries only its own columns
# ==========================================================================


def test_account_list_detail_custom_columns_send_only_its_own_vocabulary(server, mock_intuit) -> None:
    """AccountListDetail's vocabulary has neither routing key (Intuit's page
    lists no tx_date or txn_type, and its defaults omit them). Added to every
    non-empty custom column list, they send ["account_name"] out as
    account_name,tx_date,txn_type."""
    run(server, mock_intuit, "AccountListDetail", columns=["account_name", "account_bal"])
    _, query = sent(mock_intuit)
    assert query["columns"].split(",") == ["account_name", "account_bal"]


@pytest.mark.parametrize("name", [n for n, spec in CATALOG.items() if "columns" in spec.parameters])
def test_every_custom_column_request_stays_inside_its_reports_vocabulary(server, mock_intuit, name) -> None:
    """For every report that takes columns: a one-key custom list goes out
    with the routing keys only where the report's own vocabulary has them,
    and nothing else."""
    endpoint = CATALOG[name].endpoint
    allowed = R.allowed_column_keys(endpoint)
    first = sorted(allowed)[0]
    run(server, mock_intuit, name, columns=[first])
    _, query = sent(mock_intuit)
    sent_columns = query["columns"].split(",")
    assert set(sent_columns) <= allowed, sorted(set(sent_columns) - allowed)
    routing = {"tx_date", "txn_type"} & allowed
    assert set(sent_columns) == {first} | routing


# ==========================================================================
# TransactionListByCustomer is sent no columns
# ==========================================================================
#
# Observed in production, 2026-09-28: Intuit answered this report with HTTP
# 200 carrying SystemFault 10000 whenever `columns` was sent -- any value,
# even a single column tried alone -- and answered it cleanly without
# `columns`. The sandbox companies accept the same columns, and
# TransactionListByVendor takes its columns in production.

#: The report's own default column set, in its order: what QuickBooks returns
#: when no `columns` is sent, observed on both sandbox companies on 2026-09-28
#: as on the sandbox company on 2026-09-24.
BY_CUSTOMER_DEFAULT_COLUMNS = (
    ("tx_date", "Date"), ("txn_type", "String"), ("doc_num", "String"), ("is_no_post", "Boolean"),
    ("memo", "String"), ("account_name", "String"), ("amount", "Money"),
)


def test_transaction_list_by_customer_is_sent_no_columns(server, mock_intuit) -> None:
    """Whatever else the call carries, the request names no columns, so
    QuickBooks answers with the report's own default set."""
    body = run(server, mock_intuit, "TransactionListByCustomer")
    name, query = sent(mock_intuit)
    assert name == "TransactionListByCustomer"
    assert "columns" not in query, "no columns go out, not even the observed defaults"
    assert "columns" not in body["parameters_sent"]
    run(server, mock_intuit, "TransactionListByCustomer", customer=["1"], memo="x",
        transaction_type="Invoice", sort_order="ascend")
    _, query = sent(mock_intuit)
    assert {"customer", "memo", "transaction_type", "sort_order"} <= set(query)
    assert "columns" not in query


@pytest.mark.parametrize(
    "columns",
    [
        pytest.param(["amount"], id="one-default-column"),
        pytest.param(["memo"], id="one-allowlisted-column"),
        pytest.param([key for key, _ in BY_CUSTOMER_DEFAULT_COLUMNS], id="the-report-default-set"),
        pytest.param(["cust_msg"], id="a-withheld-column"),
        pytest.param([HOSTILE], id="a-hostile-value"),
        pytest.param([], id="an-empty-list"),
    ],
)
def test_a_callers_columns_for_transaction_list_by_customer_is_refused_before_any_request(
    server, mock_intuit, columns
) -> None:
    """Refused with a fixed text that says why -- fault 10000 in production,
    observed 2026-09-28 -- and names no caller value; refused() asserts that
    no request reached the mock. An empty list is refused like any other
    value of a parameter the report does not take."""
    text = refused(server, mock_intuit, "run_report",
                   {"entity": "acme_main", "report": "TransactionListByCustomer",
                    **MINIMAL["TransactionListByCustomer"], "columns": columns}, "fault 10000")
    assert "TransactionListByCustomer" in text and "2026-09-28" in text
    assert "No request was made" in text
    for key in columns:
        assert key not in text, key


def test_transaction_list_by_customers_own_default_columns_parse_without_anomalies(server, mock_intuit) -> None:
    """The parser takes the report's own default set: each of the seven
    columns published by its key and type, none withheld, no anomaly; every
    data row's amount is read as money and its type cell routes. The fixture
    is the observed set, structure only, so it is pinned here too."""
    declared = [
        ({m["Name"]: m["Value"] for m in c["MetaData"]}["ColKey"], c["ColType"])
        for c in Q.transaction_list_by_customer()["Columns"]["Column"]
    ]
    assert declared == list(BY_CUSTOMER_DEFAULT_COLUMNS), "the fixture is the observed default set"
    body = run(server, mock_intuit, "TransactionListByCustomer")
    assert [(c["key"], c["type"]) for c in body["columns"]] == list(BY_CUSTOMER_DEFAULT_COLUMNS)
    assert {c["admitted_by"] for c in body["columns"]} == {"key"}
    assert body["excluded_column_keys"] == []
    assert body["anomalies"] == [] and body["completeness"] == "unknown"
    data = [r for r in body["rows"] if r["row_type"] == "data"]
    assert len(data) == 6
    for row in data:
        cells = {c["column_key"]: c for c in row["cells"]}
        assert cells["amount"]["amount"] and cells["amount"]["currency"] == "USD"
        assert cells["txn_type"]["entity_id"], "the transaction id rides on the type cell"


def test_a_report_whose_columns_are_refused_takes_none_and_sends_no_default() -> None:
    """The catalogue says it once: TransactionListByCustomer carries its
    refusal, takes no columns and has no default set to send, and the
    network policy refuses columns on its path. TransactionListByVendor, the
    control, still takes them."""
    spec = CATALOG["TransactionListByCustomer"]
    assert spec.columns_refusal and "10000" in spec.columns_refusal
    assert "columns" not in spec.parameters and spec.default_columns == ()
    assert "columns" not in ACCOUNTING_QUERY_KEYS_BY_TAIL["reports/TransactionListByCustomer"]
    assert "columns" in CATALOG["TransactionListByVendor"].parameters
    assert "columns" in ACCOUNTING_QUERY_KEYS_BY_TAIL["reports/TransactionListByVendor"]
    for name, other in CATALOG.items():
        if other.columns_refusal:
            assert "columns" not in other.parameters and not other.default_columns, name


# ==========================================================================
# A report's fault reaches the client named by its code
# ==========================================================================


def test_transaction_list_by_customers_fault_10000_reaches_the_client_with_its_hint(server, mock_intuit) -> None:
    """The production failure as a client reads it: fault 10000 inside a
    200, named with its hint and the company the call was bound to, and none
    of the fault's own text."""
    mock_intuit.report_faults["TransactionListByCustomer"] = (200, S.fault_body("10000"))
    with pytest.raises(ToolError) as caught:
        call(server, "run_report", {"entity": "acme_main", "report": "TransactionListByCustomer",
                                    **MINIMAL["TransactionListByCustomer"]})
    text = str(caught.value)
    assert "entity=acme_main" in text and "status=200" in text
    assert "fault=10000" in text and "internal error" in text
    assert "should-not-appear" not in text


def test_a_reports_unknown_fault_reaches_the_client_named_by_its_code(server, mock_intuit) -> None:
    mock_intuit.report_faults["TransactionListByVendor"] = (200, S.fault_body("6000"))
    with pytest.raises(ToolError) as caught:
        call(server, "run_report", {"entity": "acme_main", "report": "TransactionListByVendor",
                                    **MINIMAL["TransactionListByVendor"]})
    text = str(caught.value)
    assert "entity=acme_main" in text and "status=200" in text
    assert "fault=6000" in text and "no hint for this fault" in text
    assert "should-not-appear" not in text
