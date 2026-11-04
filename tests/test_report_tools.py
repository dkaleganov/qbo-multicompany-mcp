"""The four named report tools send only what each report documents, refuse
anything else before any request, and keep a report's structure intact.

Every test drives a tool through the registered server against the loopback
mock, so the client, the network policy and the envelope are the real ones.
Each parameter refusal happens BEFORE any request: ``mock_intuit.report_queries``
is empty afterwards.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from qbo_multicompany_mcp import reports as R
from qbo_multicompany_mcp.netpolicy import ACCOUNTING_QUERY_KEYS_BY_TAIL
from qbo_multicompany_mcp.query_grammar import QueryError

from .conftest import MockIntuit
from .fixtures import report_shapes as S
from .test_changes_and_tools import build, call

TOOLS = ("report_profit_and_loss", "report_balance_sheet", "report_general_ledger", "report_aging")

#: Enough that the refusal must have come from the tool, not the value.
HOSTILE = "PRIVATE-CANARY-9931 <script>"


def seed_reports(mock: MockIntuit) -> None:
    mock.reports["ProfitAndLoss"] = S.profit_and_loss_by_month()
    mock.reports["BalanceSheet"] = S.balance_sheet_by_quarter()
    mock.reports["GeneralLedger"] = S.general_ledger()
    mock.reports["AgedPayables"] = S.aged_payables()
    mock.reports["AgedReceivables"] = S.aged_receivables()
    mock.reports["AgedPayableDetail"] = S.aged_payable_detail()
    mock.reports["AgedReceivableDetail"] = S.aged_receivable_detail()


def sent(mock: MockIntuit, index: int = -1) -> tuple[str, dict[str, str]]:
    name, query = mock.report_queries[index]
    return name, {k: v for k, v in query.items() if k != "minorversion"}


def body_of(result) -> dict[str, Any]:
    return result.structured_content


def refused(server: MCPServer, mock: MockIntuit, name: str, args: dict[str, Any], match: str) -> str:
    before = len(mock.report_queries)
    with pytest.raises(ToolError, match=match) as caught:
        call(server, name, args)
    assert len(mock.report_queries) == before, "a refused call still sent a request"
    return str(caught.value)


@pytest.fixture
def server(mock_settings, registry, credentials, actor, mock_intuit: MockIntuit) -> MCPServer:
    seed_reports(mock_intuit)
    return build(mock_settings, registry, credentials, actor)


def with_tracking(mock_intuit: MockIntuit) -> None:
    mock_intuit.preferences["AccountingInfoPrefs"]["ClassTrackingPerTxn"] = True
    mock_intuit.preferences["AccountingInfoPrefs"]["TrackDepartments"] = True


# ==========================================================================
# The surface
# ==========================================================================


def test_the_four_report_tools_are_registered(server: MCPServer) -> None:
    names = {t.name for t in anyio.run(server.list_tools)}
    assert set(TOOLS) <= names


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("report_profit_and_loss", {"start_date": "2026-01-01", "end_date": "2026-03-31"}),
        ("report_balance_sheet", {"as_of": "2026-06-30"}),
        ("report_general_ledger", {"start_date": "2026-01-01", "end_date": "2026-06-30"}),
        ("report_aging", {"side": "receivables", "detail": "summary"}),
    ],
)
def test_every_success_carries_the_result_identity_and_leads_with_the_summary(server, tool, args) -> None:
    result = call(server, tool, {"entity": "acme_main", **args})
    body = body_of(result)
    for key in ("entity", "environment", "display_name_from_registry", "realm_id_last4",
                "request_started_at", "fetched_at"):
        assert key in body, key
    assert body["entity"] == "acme_main" and body["realm_id_last4"] == "0001"
    text = result.content[0].text
    assert text.startswith('{"summary":'), "the summary is the first field of the text"
    assert json.loads(text) == body, "text and structured content disagree"
    assert body["completeness"] in ("unknown", "partial", "complete")
    assert body["anomalies"] == []


# ==========================================================================
# report_profit_and_loss
# ==========================================================================


def test_pnl_sends_the_window_and_defaults_accounting_method_to_the_company_basis(server, mock_intuit) -> None:
    body = body_of(call(server, "report_profit_and_loss",
                        {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-31"}))
    name, query = sent(mock_intuit)
    assert name == "ProfitAndLoss"
    assert query == {"start_date": "2026-01-01", "end_date": "2026-03-31", "accounting_method": "Accrual"}
    assert body["accounting_method_sent"] == "Accrual"
    assert body["accounting_method_source"] == "company_report_basis"
    assert body["report_basis"] == "Accrual"
    assert sorted(body["parameters_sent"]) == ["accounting_method", "end_date", "start_date"]
    assert body["report_name"] == "ProfitAndLoss"


def test_pnl_keeps_the_section_tree_totals_scoped_and_period_columns(server) -> None:
    body = body_of(call(server, "report_profit_and_loss",
                        {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-31",
                         "summarize_column_by": "Month"}))
    rows = {r["row_id"]: r for r in body["rows"]}
    assert rows["0.header"]["row_type"] == "section_header" and rows["0.header"]["group"] == "Income"
    assert rows["0.summary"]["scope"] == "Income"
    assert rows["4.summary"]["scope"] == "NetIncome"
    assert rows["0.0.0.0"]["parent_row_id"] == "0.0.0"
    jan = next(c for c in body["columns"] if c["key"] == "Jan 2026")
    assert (jan["period_start"], jan["period_end"]) == ("2026-01-01", "2026-01-31")
    cell = next(c for c in rows["0.0.0.0"]["cells"] if c["column_key"] == "Jan 2026")
    assert cell["amount"] == "10.00" and cell["period"] == "2026-01-01/2026-01-31"
    assert body["summarize_columns_by"] == "Month"
    assert body["excluded_column_keys"] == []


@pytest.mark.parametrize("where", ["Currency", "ReportBasis", "group"])
def test_an_object_in_the_header_or_a_row_group_reaches_no_result(server, mock_intuit, where: str) -> None:
    """Its text would carry everything nested in it past the column and cell
    checks, so the report is refused whole, like any shape it can't read."""
    report = S.profit_and_loss_by_month()
    nested = {"PrimaryEmailAddr": {"Address": HOSTILE}}
    if where == "group":
        report["Rows"]["Row"][0]["group"] = nested
    else:
        report["Header"][where] = nested
    mock_intuit.reports["ProfitAndLoss"] = report
    with pytest.raises(ToolError, match="cannot read safely") as caught:
        call(server, "report_profit_and_loss",
             {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-31",
              "summarize_column_by": "Month"})
    assert HOSTILE not in str(caught.value)


def test_pnl_entity_grouping_columns_carry_routable_ids(server, mock_intuit) -> None:
    """Summarised by Customers, the Money columns are keyed by customer id
    (observed in sandbox 2026-09-25) and admitted under the request that
    named the grouping; each carries entity_id and entity_type on the column
    so a reader can route to list_customers."""
    mock_intuit.reports["ProfitAndLoss"] = S.profit_and_loss_by_customers()
    body = body_of(call(server, "report_profit_and_loss",
                        {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
                         "summarize_column_by": "Customers"}))
    assert body["excluded_column_keys"] == []
    columns = {c["key"]: c for c in body["columns"]}
    assert columns["2"]["entity_id"] == "2" and columns["2"]["entity_type"] == "Customer"
    assert columns["7 (total)"]["entity_total"] is True and columns["7 (total)"]["entity_id"] == "7"
    assert columns["account"]["entity_type"] is None
    assert body["anomalies"] == []
    row = next(r for r in body["rows"] if r["row_id"] == "0.0.0.0")
    cell = next(c for c in row["cells"] if c["column_key"] == "8")
    assert cell["amount"] == "275.00" and cell["label"] == "Example Sub B"


def test_pnl_refuses_a_macro_together_with_explicit_dates(server, mock_intuit) -> None:
    refused(server, mock_intuit, "report_profit_and_loss",
            {"entity": "acme_main", "start_date": "2026-01-01", "date_macro": "This Fiscal Year"},
            "mutually exclusive")


def test_pnl_sends_a_macro_alone(server, mock_intuit) -> None:
    call(server, "report_profit_and_loss", {"entity": "acme_main", "date_macro": "Last Fiscal Year"})
    _, query = sent(mock_intuit)
    assert query == {"date_macro": "Last Fiscal Year", "accounting_method": "Accrual"}


def test_pnl_refuses_a_macro_outside_the_documented_set(server, mock_intuit) -> None:
    """The schema publishes the 23 values Intuit documents. The tool refuses
    anything else before any request, and the refusal never echoes the
    caller's text."""
    from qbo_multicompany_mcp.tools_reports import DATE_MACROS, _check_enum

    refused(server, mock_intuit, "report_profit_and_loss",
            {"entity": "acme_main", "date_macro": HOSTILE}, "date_macro")
    with pytest.raises(QueryError) as caught:
        _check_enum(HOSTILE, DATE_MACROS, "date_macro")
    assert HOSTILE not in str(caught.value)
    assert len(DATE_MACROS) == 23


def test_pnl_refuses_an_end_before_its_start(server, mock_intuit) -> None:
    refused(server, mock_intuit, "report_profit_and_loss",
            {"entity": "acme_main", "start_date": "2026-03-31", "end_date": "2026-01-01"}, "before")


def test_the_days_window_counts_both_of_its_ends() -> None:
    """Both dates are in the report, so 200 days is the 1st of January through
    the 19th of July."""
    from datetime import date

    from qbo_multicompany_mcp.query_grammar import QueryError
    from qbo_multicompany_mcp.tools_reports import _check_days_window

    _check_days_window("Days", date(2026, 1, 1), date(2026, 7, 19), end_name="end_date")
    with pytest.raises(QueryError):
        _check_days_window("Days", date(2026, 1, 1), date(2026, 7, 20), end_name="end_date")


def test_pnl_refuses_days_over_200_days_with_explicit_dates_or_a_resolved_macro(server, mock_intuit) -> None:
    """A Days window over 200 days is refused before any request (excess
    columns fall into 'Others'). A macro is resolved and checked like explicit
    dates. Days with no window at all is refused: there is nothing to check."""
    refused(server, mock_intuit, "report_profit_and_loss",
            {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-07-31",
             "summarize_column_by": "Days"}, "200")
    refused(server, mock_intuit, "report_profit_and_loss",
            {"entity": "acme_main", "date_macro": "This Fiscal Year", "summarize_column_by": "Days"}, "200")
    refused(server, mock_intuit, "report_profit_and_loss",
            {"entity": "acme_main", "summarize_column_by": "Days"}, "window")
    call(server, "report_profit_and_loss",
         {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-04-30",
          "summarize_column_by": "Days"})
    assert sent(mock_intuit)[1]["summarize_column_by"] == "Days"
    body = body_of(call(server, "report_profit_and_loss",
                        {"entity": "acme_main", "date_macro": "This Month", "summarize_column_by": "Days"}))
    _, query = sent(mock_intuit)
    assert query["date_macro"] == "This Month" and "start_date" not in query, (
        "the macro is sent as a macro; the resolution is this server's check, not the request")
    assert body["date_macro_sent"] == "This Month"
    assert body["resolved_start"].endswith("-01") and body["resolved_end"] > body["resolved_start"]
    assert "operator" in body["resolution_basis"] and "QuickBooks" in body["resolution_basis"]


def test_a_macro_is_resolved_in_the_companys_book_calendar_and_disclosed(server, mock_intuit, monkeypatch) -> None:
    """The fiscal macros resolve with the company's FirstMonthOfFiscalYear
    (the mock's Preferences say January) and 'today' is the operator's local
    date, labelled as such on the result."""
    from datetime import date

    from qbo_multicompany_mcp import tools_reports

    monkeypatch.setattr(tools_reports, "_today", lambda: date(2026, 9, 25))
    body = body_of(call(server, "report_profit_and_loss",
                        {"entity": "acme_main", "date_macro": "Last Fiscal Quarter"}))
    assert (body["resolved_start"], body["resolved_end"]) == ("2026-04-01", "2026-06-30")
    assert "2026-09-25" in body["resolution_basis"] and "January" in body["resolution_basis"]
    assert sent(mock_intuit)[1] == {"date_macro": "Last Fiscal Quarter", "accounting_method": "Accrual"}
    body = body_of(call(server, "report_profit_and_loss",
                        {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-31"}))
    assert body["date_macro_sent"] is None and body["resolved_start"] is None


def test_a_fiscal_macro_is_refused_when_the_company_fiscal_year_start_is_unknown(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """Without FirstMonthOfFiscalYear a fiscal macro cannot be resolved, so it
    cannot be validated like explicit dates; refused with fixed text, never
    guessed as January. A calendar-anchored macro still resolves."""
    del mock_intuit.preferences["AccountingInfoPrefs"]["FirstMonthOfFiscalYear"]
    seed_reports(mock_intuit)
    server = build(mock_settings, registry, credentials, actor)
    text = refused(server, mock_intuit, "report_profit_and_loss",
                   {"entity": "acme_main", "date_macro": "This Fiscal Year"}, "fiscal")
    assert "acme_main" in text
    call(server, "report_profit_and_loss", {"entity": "acme_main", "date_macro": "This Month"})
    assert sent(mock_intuit)[1]["date_macro"] == "This Month"


def test_pnl_refuses_a_summarize_value_outside_the_documented_set(server, mock_intuit) -> None:
    from qbo_multicompany_mcp.tools_reports import SUMMARIZE_COLUMN_BY, _check_enum

    refused(server, mock_intuit, "report_profit_and_loss",
            {"entity": "acme_main", "summarize_column_by": HOSTILE}, "summarize_column_by")
    with pytest.raises(QueryError) as caught:
        _check_enum(HOSTILE, SUMMARIZE_COLUMN_BY, "summarize_column_by")
    assert HOSTILE not in str(caught.value)
    assert len(SUMMARIZE_COLUMN_BY) == 13, "12 documented values plus FiscalYear"


def test_pnl_accepts_fiscal_year_as_a_thirteenth_summarize_value(server, mock_intuit) -> None:
    """The 12 documented values plus FiscalYear."""
    call(server, "report_profit_and_loss",
         {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
          "summarize_column_by": "FiscalYear"})
    assert sent(mock_intuit)[1]["summarize_column_by"] == "FiscalYear"


@pytest.mark.parametrize(("filter_name", "feature"), [("class", "class tracking"), ("department", "department")])
def test_pnl_refuses_a_class_or_department_filter_the_company_cannot_track(server, mock_intuit, filter_name, feature) -> None:
    """Refused with a fixed error, never silently dropped: a filter QuickBooks
    would ignore would return the whole company under a filter's name."""
    text = refused(server, mock_intuit, "report_profit_and_loss",
                   {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-31", filter_name: ["7"]},
                   feature)
    assert "acme_main" in text and "No request was made" in text


def test_pnl_sends_class_and_department_when_the_company_tracks_them(mock_settings, registry, credentials, actor, mock_intuit) -> None:
    with_tracking(mock_intuit)
    seed_reports(mock_intuit)
    server = build(mock_settings, registry, credentials, actor)
    body = body_of(call(server, "report_profit_and_loss",
                        {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-31",
                         "class": ["7"], "department": ["3"], "customer": ["1"], "vendor": ["2"]}))
    _, query = sent(mock_intuit)
    assert query["class"] == "7" and query["department"] == "3"
    assert query["customer"] == "1" and query["vendor"] == "2"
    assert set(body["parameters_sent"]) >= {"class", "department", "customer", "vendor"}
    assert sorted(body["capabilities_checked"]) == ["class_tracking", "department_tracking"]


def test_pnl_an_explicit_accounting_method_is_sent_as_given(server, mock_intuit) -> None:
    body = body_of(call(server, "report_profit_and_loss",
                        {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-31",
                         "accounting_method": "Cash"}))
    assert sent(mock_intuit)[1]["accounting_method"] == "Cash"
    assert body["accounting_method_source"] == "caller"


def test_pnl_max_rows_trims_locally_and_says_so(server) -> None:
    body = body_of(call(server, "report_profit_and_loss",
                        {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-31",
                         "max_rows": 3}))
    assert body["client_truncated"] is True and body["count"] == 3
    assert body["upstream_row_count"] > 3
    assert body["completeness"] == "partial"


def test_pnl_fault_5020_is_a_fixed_actionable_error_not_an_empty_report(server, mock_intuit) -> None:
    mock_intuit.report_faults["ProfitAndLoss"] = (400, S.fault_body("5020"))
    with pytest.raises(ToolError) as caught:
        call(server, "report_profit_and_loss",
             {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-31"})
    text = str(caught.value)
    assert "fault=5020" in text and "endpoint=accounting/reports" in text
    assert "sign in" in text and "not an empty report" in text
    assert "should-not-appear" not in text


def test_pnl_never_sends_a_parameter_its_inventory_does_not_list() -> None:
    """Intuit's ProfitAndLoss page documents no `columns`, no `account` and no
    aging parameter; the tail's allowlist refuses them, and the documented
    ones the tool does not expose (qzurl, item, adjusted_gain_loss) are
    refused too."""
    keys = ACCOUNTING_QUERY_KEYS_BY_TAIL["reports/ProfitAndLoss"]
    assert not keys & {"columns", "account", "aging_method", "report_date", "qzurl", "item", "adjusted_gain_loss"}
    assert {"start_date", "end_date", "date_macro", "accounting_method", "summarize_column_by",
            "customer", "vendor", "class", "department"} <= keys


# ==========================================================================
# report_balance_sheet
# ==========================================================================


def test_bs_as_of_maps_to_end_date_and_start_date_is_optional(server, mock_intuit) -> None:
    call(server, "report_balance_sheet", {"entity": "acme_main", "as_of": "2026-06-30"})
    assert sent(mock_intuit) == ("BalanceSheet", {"end_date": "2026-06-30", "accounting_method": "Accrual"})
    call(server, "report_balance_sheet", {"entity": "acme_main", "as_of": "2026-06-30", "start_date": "2026-01-01"})
    assert sent(mock_intuit)[1]["start_date"] == "2026-01-01"


def test_bs_takes_as_of_or_date_macro_one_required_and_mutually_exclusive(server, mock_intuit) -> None:
    """`as_of` or `date_macro`, one required, mutually exclusive. The macro
    resolves like every other, and the network policy admits it."""
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    schema = tools["report_balance_sheet"].input_schema
    assert "date_macro" in schema["properties"] and "as_of" in schema["properties"]
    assert "as_of" not in schema.get("required", [])
    assert "date_macro" in ACCOUNTING_QUERY_KEYS_BY_TAIL["reports/BalanceSheet"]

    body = body_of(call(server, "report_balance_sheet", {"entity": "acme_main", "date_macro": "Last Fiscal Year-to-date"}))
    assert sent(mock_intuit) == ("BalanceSheet", {"date_macro": "Last Fiscal Year-to-date", "accounting_method": "Accrual"})
    assert body["date_macro_sent"] == "Last Fiscal Year-to-date" and body["resolved_end"]
    refused(server, mock_intuit, "report_balance_sheet",
            {"entity": "acme_main", "as_of": "2026-06-30", "date_macro": "Today"}, "mutually exclusive")
    refused(server, mock_intuit, "report_balance_sheet",
            {"entity": "acme_main", "start_date": "2026-01-01", "date_macro": "Today"}, "mutually exclusive")
    refused(server, mock_intuit, "report_balance_sheet", {"entity": "acme_main"}, "one of")


def test_bs_days_with_a_macro_is_checked_through_the_resolver(server, mock_intuit) -> None:
    refused(server, mock_intuit, "report_balance_sheet",
            {"entity": "acme_main", "date_macro": "This Fiscal Year", "summarize_column_by": "Days"}, "200")
    call(server, "report_balance_sheet",
         {"entity": "acme_main", "date_macro": "This Fiscal Quarter", "summarize_column_by": "Days"})
    assert sent(mock_intuit)[1]["summarize_column_by"] == "Days"


def test_bs_refuses_a_start_after_the_as_of_date(server, mock_intuit) -> None:
    refused(server, mock_intuit, "report_balance_sheet",
            {"entity": "acme_main", "as_of": "2026-01-31", "start_date": "2026-06-30"}, "before")


def test_bs_days_needs_a_start_date_and_at_most_200_days(server, mock_intuit) -> None:
    refused(server, mock_intuit, "report_balance_sheet",
            {"entity": "acme_main", "as_of": "2026-06-30", "summarize_column_by": "Days"}, "start_date")
    refused(server, mock_intuit, "report_balance_sheet",
            {"entity": "acme_main", "as_of": "2026-12-31", "start_date": "2026-01-01", "summarize_column_by": "Days"},
            "200")


def test_bs_keeps_four_levels_of_nesting_and_the_quarter_columns(server) -> None:
    body = body_of(call(server, "report_balance_sheet",
                        {"entity": "acme_main", "as_of": "2026-06-30", "summarize_column_by": "Quarter"}))
    rows = {r["row_id"]: r for r in body["rows"]}
    assert rows["1.0.0.0.0"]["depth"] == 4 and rows["1.0.0.0.0"]["parent_row_id"] == "1.0.0.0"
    assert rows["1.0.0.0.summary"]["scope"] == "AP"
    assert [c["key"] for c in body["columns"]] == ["account", "Jan - Mar, 2026", "Apr - Jun, 2026"]
    net_income = next(r for r in body["rows"] if r["group"] == "NetIncome")
    assert net_income["row_type"] == "data"


@pytest.mark.parametrize("filter_name", ["class", "department"])
def test_bs_refuses_an_untracked_class_or_department_filter(server, mock_intuit, filter_name) -> None:
    refused(server, mock_intuit, "report_balance_sheet",
            {"entity": "acme_main", "as_of": "2026-06-30", filter_name: ["1"]}, "not enabled")


# ==========================================================================
# report_general_ledger
# ==========================================================================


def test_gl_sends_the_window_the_default_columns_and_the_company_basis(server, mock_intuit) -> None:
    body = body_of(call(server, "report_general_ledger",
                        {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30"}))
    name, query = sent(mock_intuit)
    assert name == "GeneralLedger"
    assert query["columns"].split(",") == [
        "tx_date", "txn_type", "doc_num", "name", "memo", "split_acc", "subt_nat_amount", "rbal_nat_amount",
    ], "the observed default set, in the observed order"
    assert query["accounting_method"] == "Accrual"
    assert body["report_name"] == "GeneralLedger"


def test_gl_refuses_a_window_over_six_calendar_months(server, mock_intuit) -> None:
    """The six-month limit is on every detail report. Refused, never silently
    narrowed."""
    refused(server, mock_intuit, "report_general_ledger",
            {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-07-01"}, "six")
    call(server, "report_general_ledger", {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30"})
    assert sent(mock_intuit)[1]["end_date"] == "2026-06-30"


def test_gl_rows_keep_the_account_header_id_and_the_transaction_routing_ids(server) -> None:
    body = body_of(call(server, "report_general_ledger",
                        {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30"}))
    rows = {r["row_id"]: r for r in body["rows"]}
    header = next(c for c in rows["0.header"]["cells"] if c["column_key"] == "tx_date")
    assert header["entity_id"] == "1" and header["text"] == "Example Checking"
    typed = next(c for c in rows["0.1"]["cells"] if c["column_key"] == "txn_type")
    assert (typed["text"], typed["entity_id"]) == ("Deposit", "41")
    assert rows["1.0.0"]["parent_row_id"] == "1.0", "a sub-account section with no header still nests"


def test_gl_account_list_is_joined_and_account_type_is_pinned(server, mock_intuit) -> None:
    call(server, "report_general_ledger",
         {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
          "account": ["1", "2"], "account_type": "Bank"})
    from qbo_multicompany_mcp.tools_reports import GL_ACCOUNT_TYPES, _check_enum

    _, query = sent(mock_intuit)
    assert query["account"] == "1,2" and query["account_type"] == "Bank"
    refused(server, mock_intuit, "report_general_ledger",
            {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
             "account_type": HOSTILE}, "account_type")
    with pytest.raises(QueryError) as caught:
        _check_enum(HOSTILE, GL_ACCOUNT_TYPES, "account_type")
    assert HOSTILE not in str(caught.value)
    assert "Accounts Payable" not in GL_ACCOUNT_TYPES and "AccountsPayable" in GL_ACCOUNT_TYPES


def test_gl_columns_are_allowlisted_and_named_by_position_when_refused(server, mock_intuit) -> None:
    text = refused(server, mock_intuit, "report_general_ledger",
                   {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
                    "columns": ["memo", HOSTILE]}, r"columns\[1\]")
    assert HOSTILE not in text
    text = refused(server, mock_intuit, "report_general_ledger",
                   {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
                    "columns": ["account_num"]}, r"columns\[0\]")
    call(server, "report_general_ledger",
         {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30", "columns": ["memo", "name"]})
    assert set(sent(mock_intuit)[1]["columns"].split(",")) == {"memo", "name", "tx_date", "txn_type"}, (
        "the routing columns are always requested")


def test_gl_refuses_twenty_five_or_more_columns(server, mock_intuit) -> None:
    """Intuit warns that 25 or more columns in one request risk a 504 timeout."""
    many = sorted(R.allowed_column_keys("GeneralLedger"))
    assert len(many) >= 25, "the allowlist must be wide enough to hit the rule"
    refused(server, mock_intuit, "report_general_ledger",
            {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30", "columns": many[:25]}, "25")


def test_gl_counts_the_routing_columns_toward_the_limit(server, mock_intuit) -> None:
    """23 keys that omit tx_date and txn_type are a 25-column request once the
    routing columns are added, and Intuit's 504 warning is about the request."""
    keys = sorted(R.allowed_column_keys("GeneralLedger") - {"tx_date", "txn_type"})[:23]
    assert len(keys) == 23
    refused(server, mock_intuit, "report_general_ledger",
            {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30", "columns": keys},
            "would carry 25 columns")


def test_gl_refuses_a_list_of_twenty_five_even_when_it_repeats_keys(server, mock_intuit) -> None:
    """The count on the list stands beside the count on the request: a caller's
    list of 25 or more is refused whatever it would collapse to."""
    refused(server, mock_intuit, "report_general_ledger",
            {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
             "columns": ["memo"] * 25}, "25 or more")


@pytest.mark.parametrize("filter_name", ["class", "department"])
def test_gl_refuses_an_untracked_class_or_department_filter(server, mock_intuit, filter_name) -> None:
    refused(server, mock_intuit, "report_general_ledger",
            {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30", filter_name: ["1"]},
            "not enabled")


# ==========================================================================
# report_aging: four endpoints, four allowlists
# ==========================================================================


@pytest.mark.parametrize(
    ("side", "detail", "path"),
    [("payables", "summary", "AgedPayables"), ("receivables", "summary", "AgedReceivables"),
     ("payables", "detail", "AgedPayableDetail"), ("receivables", "detail", "AgedReceivableDetail")],
)
def test_aging_routes_each_combination_to_its_documented_endpoint(server, mock_intuit, side, detail, path) -> None:
    body = body_of(call(server, "report_aging", {"entity": "acme_main", "side": side, "detail": detail}))
    assert sent(mock_intuit)[0] == path
    assert body["report_name"] == path
    assert (body["side"], body["detail"]) == (side, detail)


def test_aging_summary_default_is_summary(server, mock_intuit) -> None:
    call(server, "report_aging", {"entity": "acme_main", "side": "receivables"})
    assert sent(mock_intuit)[0] == "AgedReceivables"


def test_aging_ap_detail_takes_accounting_method_and_refuses_aging_method(server, mock_intuit) -> None:
    """Intuit's APAgingDetail page lists accounting_method and no aging_method."""
    call(server, "report_aging", {"entity": "acme_main", "side": "payables", "detail": "detail", "accounting_method": "Cash"})
    _, query = sent(mock_intuit)
    assert query["accounting_method"] == "Cash" and "aging_method" not in query
    refused(server, mock_intuit, "report_aging",
            {"entity": "acme_main", "side": "payables", "detail": "detail", "aging_method": "Current"},
            "'aging_method' is not accepted by the detail aging report for payables")


def test_aging_ar_detail_takes_aging_method_and_refuses_accounting_method(server, mock_intuit) -> None:
    call(server, "report_aging", {"entity": "acme_main", "side": "receivables", "detail": "detail", "aging_method": "Current"})
    _, query = sent(mock_intuit)
    assert query["aging_method"] == "Current" and "accounting_method" not in query, (
        "the company basis must not be sent to a report whose inventory has no accounting_method")
    refused(server, mock_intuit, "report_aging",
            {"entity": "acme_main", "side": "receivables", "detail": "detail", "accounting_method": "Cash"},
            "'accounting_method' is not accepted by the detail aging report for receivables")


def test_aging_summaries_take_aging_method_report_date_or_macro_and_no_detail_parameters(server, mock_intuit) -> None:
    call(server, "report_aging", {"entity": "acme_main", "side": "payables", "aging_method": "Report_Date",
                                  "report_date": "2026-06-30", "sort_order": "descend"})
    _, query = sent(mock_intuit)
    assert query == {"aging_method": "Report_Date", "report_date": "2026-06-30", "sort_order": "descend"}
    call(server, "report_aging", {"entity": "acme_main", "side": "payables", "date_macro": "Today"})
    assert sent(mock_intuit)[1] == {"date_macro": "Today"}
    refused(server, mock_intuit, "report_aging",
            {"entity": "acme_main", "side": "payables", "report_date": "2026-06-30", "date_macro": "Today"},
            "mutually exclusive")
    for extra in ({"start_duedate": "2026-01-01", "end_duedate": "2026-03-31"}, {"past_due": 30},
                  {"aging_period": 30}, {"num_periods": 4}, {"columns": ["tx_date"]}, {"accounting_method": "Cash"}):
        refused(server, mock_intuit, "report_aging", {"entity": "acme_main", "side": "payables", **extra},
                "is not accepted by the summary aging report for payables")


def test_aging_vendor_filter_belongs_to_payables_and_customer_to_receivables(server, mock_intuit) -> None:
    call(server, "report_aging", {"entity": "acme_main", "side": "payables", "vendor": ["2"]})
    assert sent(mock_intuit)[1] == {"vendor": "2"}
    call(server, "report_aging", {"entity": "acme_main", "side": "receivables", "detail": "detail", "customer": ["1"]})
    assert sent(mock_intuit)[1]["customer"] == "1"
    refused(server, mock_intuit, "report_aging", {"entity": "acme_main", "side": "payables", "customer": ["1"]},
            "'customer' is not accepted by the summary aging report for payables")
    refused(server, mock_intuit, "report_aging", {"entity": "acme_main", "side": "receivables", "vendor": ["2"]},
            "'vendor' is not accepted by the summary aging report for receivables")


def test_aging_detail_carries_the_six_month_limit_on_its_due_date_window(server, mock_intuit) -> None:
    """Detail reports carry the six-month limit. The window here is the
    due-date range; a one-sided range is unbounded and is refused too; no
    range at all is the open-items-as-of-report_date report."""
    refused(server, mock_intuit, "report_aging",
            {"entity": "acme_main", "side": "receivables", "detail": "detail",
             "start_duedate": "2026-01-01", "end_duedate": "2026-07-01"}, "six")
    refused(server, mock_intuit, "report_aging",
            {"entity": "acme_main", "side": "receivables", "detail": "detail", "start_duedate": "2026-01-01"},
            "end_duedate")
    call(server, "report_aging", {"entity": "acme_main", "side": "receivables", "detail": "detail",
                                  "start_duedate": "2026-01-01", "end_duedate": "2026-06-30",
                                  "past_due": 30, "aging_period": 30, "num_periods": 4, "report_date": "2026-06-30"})
    _, query = sent(mock_intuit)
    assert query == {"start_duedate": "2026-01-01", "end_duedate": "2026-06-30", "past_due": "30",
                     "aging_period": "30", "num_periods": "4", "report_date": "2026-06-30"}


def test_aging_detail_columns_are_allowlisted_per_side(server, mock_intuit) -> None:
    text = refused(server, mock_intuit, "report_aging",
                   {"entity": "acme_main", "side": "payables", "detail": "detail", "columns": ["vend_pri_email"]},
                   r"columns\[0\]")
    assert "vend_pri_email" not in text or "columns[0]" in text
    refused(server, mock_intuit, "report_aging",
            {"entity": "acme_main", "side": "receivables", "detail": "detail", "columns": ["vend_name"]},
            r"columns\[0\]")
    call(server, "report_aging", {"entity": "acme_main", "side": "receivables", "detail": "detail", "columns": ["cust_name", "due_date"]})
    assert set(sent(mock_intuit)[1]["columns"].split(",")) == {"cust_name", "due_date", "tx_date", "txn_type"}


def test_aging_detail_rows_route_and_summary_rows_carry_bucket_labels(server) -> None:
    body = body_of(call(server, "report_aging", {"entity": "acme_main", "side": "payables", "detail": "detail"}))
    row = next(r for r in body["rows"] if r["row_type"] == "data")
    typed = next(c for c in row["cells"] if c["column_key"] == "txn_type")
    assert (typed["text"], typed["entity_id"]) == ("Bill", "51")

    body = body_of(call(server, "report_aging", {"entity": "acme_main", "side": "payables"}))
    row = next(r for r in body["rows"] if r["row_type"] == "data")
    cells = {c["column_key"]: c for c in row["cells"]}
    assert cells["vendor"]["entity_id"] == "1"
    assert cells["1"]["label"] == "31 - 60" and cells["1"]["amount"] == "5.00"
    grand = next(r for r in body["rows"] if r["scope"] == "GrandTotal")
    assert grand["covers"] == "upstream_section"


def test_aging_payables_on_a_company_without_accounts_payable_is_an_error_via_the_capability(server, mock_intuit) -> None:
    """The feature fault establishes 'unavailable' for this process; the error
    names it, and the next call makes no request."""
    mock_intuit.report_faults["AgedPayables"] = (400, S.fault_body("5030"))
    with pytest.raises(ToolError) as caught:
        call(server, "report_aging", {"entity": "acme_main", "side": "payables"})
    text = str(caught.value)
    assert "accounts payable" in text and "5030" in text and "not an empty report" in text
    assert "should-not-appear" not in text
    sent_before = len(mock_intuit.report_queries)
    with pytest.raises(ToolError, match="accounts payable"):
        call(server, "report_aging", {"entity": "acme_main", "side": "payables", "detail": "detail"})
    assert len(mock_intuit.report_queries) == sent_before, "a known-unavailable feature was asked again"
    # Receivables never consult accounts payable.
    call(server, "report_aging", {"entity": "acme_main", "side": "receivables"})


def test_aging_payables_success_records_accounts_payable_as_available(server, mock_intuit) -> None:
    body = body_of(call(server, "report_aging", {"entity": "acme_main", "side": "payables"}))
    assert body["accounts_payable"] == "available"
    body = body_of(call(server, "report_aging", {"entity": "acme_main", "side": "receivables"}))
    assert body["accounts_payable"] is None


@pytest.mark.parametrize("path", ["AgedPayables", "AgedReceivables", "AgedPayableDetail", "AgedReceivableDetail"])
def test_aging_fault_5020_is_mapped_on_every_endpoint(server, mock_intuit, path) -> None:
    mock_intuit.report_faults[path] = (400, S.fault_body("5020"))
    side = "payables" if "Payable" in path else "receivables"
    detail = "detail" if "Detail" in path else "summary"
    with pytest.raises(ToolError, match="fault=5020"):
        call(server, "report_aging", {"entity": "acme_main", "side": side, "detail": detail})


@pytest.mark.parametrize("filter_name", ["department"])
def test_aging_refuses_an_untracked_department_filter(server, mock_intuit, filter_name) -> None:
    refused(server, mock_intuit, "report_aging", {"entity": "acme_main", "side": "receivables", filter_name: ["1"]},
            "not enabled")


def test_aging_four_allowlists_are_distinct_at_the_network_boundary() -> None:
    ap_d = ACCOUNTING_QUERY_KEYS_BY_TAIL["reports/AgedPayableDetail"]
    ar_d = ACCOUNTING_QUERY_KEYS_BY_TAIL["reports/AgedReceivableDetail"]
    ap_s = ACCOUNTING_QUERY_KEYS_BY_TAIL["reports/AgedPayables"]
    ar_s = ACCOUNTING_QUERY_KEYS_BY_TAIL["reports/AgedReceivables"]
    assert "accounting_method" in ap_d and "aging_method" not in ap_d
    assert "aging_method" in ar_d and "accounting_method" not in ar_d
    assert {"start_duedate", "end_duedate", "past_due", "aging_period", "num_periods", "columns"} <= ap_d & ar_d
    assert {"aging_method", "report_date", "date_macro", "sort_order", "department"} <= ap_s & ar_s
    assert "vendor" in ap_s and "customer" not in ap_s
    assert "customer" in ar_s and "vendor" not in ar_s
    assert not ({"columns", "start_duedate"} & (ap_s | ar_s))


# ==========================================================================
# The send-side backstop behind every tool
# ==========================================================================


def test_the_inventory_backstop_refuses_a_parameter_its_row_does_not_list() -> None:
    """Unreachable from a call, which is the point: it exists so a future edit
    cannot send an unlisted parameter by accident. Names, never values."""
    from qbo_multicompany_mcp.tools_reports import REPORT_PARAMETER_INVENTORY, _inventory_check

    _inventory_check("ProfitAndLoss", {"start_date": "2026-01-01", "customer": "1"})
    with pytest.raises(QueryError, match="columns") as caught:
        _inventory_check("ProfitAndLoss", {"start_date": "2026-01-01", "columns": HOSTILE})
    assert HOSTILE not in str(caught.value)
    with pytest.raises(QueryError, match="aging_method"):
        _inventory_check("AgedPayableDetail", {"aging_method": "Current"})
    with pytest.raises(QueryError, match="accounting_method"):
        _inventory_check("AgedReceivableDetail", {"accounting_method": "Cash"})
    assert set(REPORT_PARAMETER_INVENTORY) == set(R.REPORT_ENDPOINTS.values())


# ==========================================================================
# Report id filters take lists
# ==========================================================================
#
# Intuit's report pages document these parameters as "One or more comma
# separated ... IDs", so a list goes out as that one comma-separated value.


def test_named_report_id_filters_take_lists_and_send_one_comma_separated_value(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    with_tracking(mock_intuit)
    seed_reports(mock_intuit)
    server = build(mock_settings, registry, credentials, actor)
    window = {"start_date": "2026-01-01", "end_date": "2026-03-31"}

    call(server, "report_profit_and_loss", {"entity": "acme_main", **window, "customer": ["1", "2"],
                                            "vendor": ["3"], "class": ["4", "5"], "department": ["6"]})
    query = sent(mock_intuit)[1]
    assert (query["customer"], query["vendor"], query["class"], query["department"]) == ("1,2", "3", "4,5", "6")

    call(server, "report_balance_sheet", {"entity": "acme_main", "as_of": "2026-03-31",
                                          "class": ["7"], "department": ["8", "9"]})
    query = sent(mock_intuit)[1]
    assert (query["class"], query["department"]) == ("7", "8,9")

    call(server, "report_general_ledger", {"entity": "acme_main", **window, "account": ["1", "2"],
                                           "class": ["3", "4"], "department": ["5"]})
    query = sent(mock_intuit)[1]
    assert (query["account"], query["class"], query["department"]) == ("1,2", "3,4", "5")

    call(server, "report_aging", {"entity": "acme_main", "side": "payables", "vendor": ["1", "2"],
                                  "department": ["3"]})
    query = sent(mock_intuit)[1]
    assert (query["vendor"], query["department"]) == ("1,2", "3")
    call(server, "report_aging", {"entity": "acme_main", "side": "receivables", "detail": "detail",
                                  "customer": ["4", "5"]})
    assert sent(mock_intuit)[1]["customer"] == "4,5"


@pytest.mark.parametrize(
    "bad",
    [["41,42"], ["43", " 44"], ["x45"], [""], []],
    ids=["a-comma-inside-one-id", "a-space", "not-digits", "an-empty-id", "an-empty-list"],
)
def test_a_report_id_filter_that_is_not_a_list_of_ids_is_refused_before_any_request(
    mock_settings, registry, credentials, actor, mock_intuit, bad
) -> None:
    """Each element is validated as an id before any request: once joined, a
    comma or a space inside one element would turn one id into two, or into
    another. The refusal names the parameter and never the value (through the
    same call boundary as the wire)."""
    from qbo_multicompany_mcp.server import QboMCPServer
    from qbo_multicompany_mcp.tools import register_tools

    from .conftest import REALM_MAIN, make_token_response

    mock_settings.open_store(credentials=credentials).commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )
    server = QboMCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    before = len(mock_intuit.report_queries)
    with pytest.raises(ToolError) as refused_call:
        anyio.run(lambda: server.call_tool("report_profit_and_loss", {
            "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-31", "customer": bad,
        }))
    text = str(refused_call.value)
    assert len(mock_intuit.report_queries) == before, "refused before any request"
    assert "customer" in text
    for element in bad:
        if element:
            assert element not in text

# ==========================================================================
# The final column list is checked against the report's own vocabulary
# ==========================================================================


def test_the_final_column_list_is_checked_against_the_reports_own_vocabulary() -> None:
    """Routing keys are added per endpoint -- only where the report's own
    vocabulary has them -- and the list that would go out, a default one
    included, is checked against that vocabulary before any request. A key
    outside it can only be one this package added, so it is named."""
    from qbo_multicompany_mcp.tools_reports import _columns_to_request

    assert _columns_to_request(["account_name"], "AccountList", ()) == ["account_name"]
    assert _columns_to_request(["memo"], "GeneralLedger", ()) == ["memo", "tx_date", "txn_type"]
    with pytest.raises(QueryError, match="tx_date") as caught:
        _columns_to_request(None, "AccountList", ("account_name", "tx_date"))
    assert "AccountList" in str(caught.value)
