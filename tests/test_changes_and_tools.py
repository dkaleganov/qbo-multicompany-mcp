"""The list, query, transaction, change-capture and vendor-payment tools, driven
end to end against a real loopback server: what each sends, refuses and
discloses."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from qbo_multicompany_mcp.projections import has_projection
from qbo_multicompany_mcp.query_grammar import TRANSACTION_TYPES, served_entities
from qbo_multicompany_mcp.tools import register_tools

from .conftest import REALM_MAIN, MockIntuit, make_token_response


def build(mock_settings, registry, credentials, actor) -> MCPServer:
    store = mock_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(), actor=actor,
    )
    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    return server


def call(server: MCPServer, name: str, args: dict[str, Any]):
    return anyio.run(lambda: server.call_tool(name, args))


def vendor(id_: str, name: str, **extra) -> dict:
    base = {"Id": id_, "DisplayName": name, "Active": True}
    base.update(extra)
    return base


# --------------------------------------------------------------------------
# list_vendors / list_customers
# --------------------------------------------------------------------------


def test_list_vendors_reports_the_tax_id_boolean_never_the_value(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.tables["Vendor"] = [
        vendor("1", "Acme Supplies", Vendor1099=True, TaxIdentifier="12-3456789",
               PrimaryEmailAddr={"Address": "x@y.z"}, BillAddr={"Line1": "somewhere"}),
        vendor("2", "No Tax Co"),
    ]
    server = build(mock_settings, registry, credentials, actor)
    result = call(server, "list_vendors", {"entity": "acme_main"})

    assert result.is_error is False
    body = result.structured_content
    assert body["count"] == 2
    first = body["rows"][0]
    assert first["tax_id_on_file"] is True
    assert first["vendor_1099"] is True
    second = body["rows"][1]
    assert second["tax_id_on_file"] is None
    assert second["tax_id_on_file_reason"]

    import json

    dumped = json.dumps(body)
    for leaked in ("12-3456789", "x@y.z", "somewhere"):
        assert leaked not in dumped


def test_list_vendors_uses_the_shared_list_vocabulary(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.tables["Vendor"] = [vendor("1", "A")]
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_vendors", {"entity": "acme_main"}).structured_content
    assert {"count", "rows", "truncated", "duplicates_dropped", "pages_walked"} <= set(body)
    assert body["consistency"] == "non_snapshot"
    assert body["walk_started_at"] and body["walk_finished_at"]


def test_active_null_includes_inactive_rows(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.tables["Vendor"] = [vendor("1", "A"), vendor("2", "B", Active=False)]
    server = build(mock_settings, registry, credentials, actor)
    call(server, "list_vendors", {"entity": "acme_main", "active": None})
    sent = [q for _, path, q, _ in mock_intuit.requests if path.endswith("/query")]
    assert any("Active IN (true, false)" in q.get("query", "") for q in sent)


@pytest.mark.parametrize("tool", ["list_vendors", "list_customers"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("last tuesday", id="words"),
        # ISO forms Python reads but Intuit's own timestamps never take, so
        # nothing says a query would accept them.
        pytest.param("2026-W36-1", id="week-date"),
        pytest.param("20260901T000000Z", id="compact"),
        pytest.param("2026-09-01 00:00:00", id="space-separated"),
        pytest.param("2026-09-01T00", id="hour-only"),
        pytest.param("2026-13-01", id="no-such-month"),
        pytest.param("２０２６-０９-０１", id="fullwidth-digits"),
    ],
)
def test_an_updated_since_that_is_no_timestamp_is_refused_before_any_request(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, tool: str, value: str
) -> None:
    """The input alone decides it, so neither the guard's reads nor the query
    are sent."""
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="updated_since must be a date, or a date and time to the second"):
        call(server, tool, {"entity": "acme_main", "updated_since": value})
    assert mock_intuit.requests == []


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("２０２６-０９-０１", id="fullwidth-date"),
        pytest.param("٢٠٢٦-٠٩-٠١", id="arabic-indic-date"),
        pytest.param("2026-09-01T00:00:00.１", id="fullwidth-fraction"),
        pytest.param("2026-09-01T00:00:00+０１:00", id="fullwidth-offset"),
    ],
)
def test_the_timestamp_shape_takes_ascii_digits_only(value: str) -> None:
    """CPython's fromisoformat refuses these already, so the refusal above
    can't tell whether the shape did. A pure-Python datetime reads them
    through int(), so the shape must refuse them itself."""
    from qbo_multicompany_mcp import tools_data

    assert tools_data._TIMESTAMP_SHAPE.fullmatch(value) is None


@pytest.mark.parametrize("tool", ["list_vendors", "list_customers"])
def test_an_updated_since_timestamp_is_sent_as_written(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, tool: str
) -> None:
    server = build(mock_settings, registry, credentials, actor)
    call(server, tool, {"entity": "acme_main", "updated_since": " 2026-09-01T00:00:00-07:00 "})
    sent = [q.get("query", "") for _, path, q, _ in mock_intuit.requests if path.endswith("/query")]
    assert any("MetaData.LastUpdatedTime >= '2026-09-01T00:00:00-07:00'" in q for q in sent)


def test_list_customers_carries_no_contact_fields(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.tables["Customer"] = [
        {"Id": "1", "DisplayName": "A Customer", "Balance": 10.0,
         "PrimaryEmailAddr": {"Address": "c@d.e"}, "Notes": "private",
         "BillAddr": {"Line1": "addr"}, "ResaleNum": "R1"},
    ]
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_customers", {"entity": "acme_main"}).structured_content

    import json

    dumped = json.dumps(body)
    for leaked in ("c@d.e", "private", "addr", "R1"):
        assert leaked not in dumped
    assert body["rows"][0]["balance"] == 10.0


# --------------------------------------------------------------------------
# list_accounts: the active, account_type and classification filters
# --------------------------------------------------------------------------


def account_where(row, statement: str) -> bool:
    """Make the mock honour list_accounts' WHERE the way QuickBooks would."""
    active = row.get("Active", True)
    if "Active = true" in statement and active is not True:
        return False
    if "Active = false" in statement and active is not False:
        return False
    for field in ("AccountType", "Classification"):
        marker = f"{field} = '"
        if marker in statement:
            wanted = statement.split(marker, 1)[1].split("'", 1)[0]
            if row.get(field) != wanted:
                return False
    return True


def seed_accounts(mock: MockIntuit) -> None:
    mock.accounts = [
        {"Id": "1", "Name": "Checking", "AccountType": "Bank", "Classification": "Asset",
         "Active": True, "CurrentBalance": 1.0},
        {"Id": "2", "Name": "Payables", "AccountType": "Accounts Payable",
         "Classification": "Liability", "Active": True, "CurrentBalance": 2.0},
        {"Id": "3", "Name": "Old card", "AccountType": "Credit Card",
         "Classification": "Liability", "Active": False, "CurrentBalance": 0.0},
        {"Id": "4", "Name": "Rent", "AccountType": "Expense", "Classification": "Expense",
         "Active": True, "CurrentBalance": 3.0},
    ]
    mock.filters["Account"] = [account_where]


def account_queries(mock: MockIntuit) -> list[str]:
    return [
        q.get("query", "") for _, path, q, _ in mock.requests
        if path.endswith("/query") and " FROM Account" in q.get("query", "")
    ]


def account_ids(server: MCPServer, **args: Any) -> list[str]:
    body = call(server, "list_accounts", {"entity": "acme_main", **args}).structured_content
    return sorted(row["id"] for row in body["rows"])


def test_list_accounts_sends_active_true_by_default_and_null_includes_inactive(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    seed_accounts(mock_intuit)
    server = build(mock_settings, registry, credentials, actor)

    assert account_ids(server) == ["1", "2", "4"]
    assert all("Active = true" in q for q in account_queries(mock_intuit))

    mock_intuit.requests.clear()
    assert account_ids(server, active=None) == ["1", "2", "3", "4"]
    assert all("Active IN (true, false)" in q for q in account_queries(mock_intuit))

    assert account_ids(server, active=False) == ["3"]


def test_list_accounts_filters_on_the_entitys_spaced_account_type(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    seed_accounts(mock_intuit)
    server = build(mock_settings, registry, credentials, actor)

    assert account_ids(server, account_type="Accounts Payable") == ["2"]
    assert all("AccountType = 'Accounts Payable'" in q for q in account_queries(mock_intuit))


def test_list_accounts_refuses_the_report_filter_spelling_before_any_request(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """'AccountsPayable' is the report filters' vocabulary (source_account_type),
    not the Account entity's."""
    seed_accounts(mock_intuit)
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError):
        call(server, "list_accounts", {"entity": "acme_main", "account_type": "AccountsPayable"})
    assert account_queries(mock_intuit) == []


def test_list_accounts_filters_on_classification(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    seed_accounts(mock_intuit)
    server = build(mock_settings, registry, credentials, actor)

    assert account_ids(server, classification="Liability") == ["2"]
    assert all("Classification = 'Liability'" in q for q in account_queries(mock_intuit))
    assert account_ids(server, classification="Liability", active=None) == ["2", "3"]


def test_list_accounts_count_anchor_carries_the_same_where(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """The shared walker's contract: the COUNT(*) that anchors the walk filters
    exactly as the pages do, or a filtered walk fails its own count check."""
    seed_accounts(mock_intuit)
    server = build(mock_settings, registry, credentials, actor)
    call(server, "list_accounts", {
        "entity": "acme_main", "active": None,
        "account_type": "Credit Card", "classification": "Liability",
    })

    queries = account_queries(mock_intuit)
    counts = [q for q in queries if "COUNT(*)" in q]
    pages = [q for q in queries if "COUNT(*)" not in q]
    assert counts and pages

    def where(statement: str) -> str:
        return statement.split(" WHERE ", 1)[1].split(" ORDERBY", 1)[0].split(" STARTPOSITION", 1)[0]

    assert {where(q) for q in counts} == {where(q) for q in pages} == {
        "Active IN (true, false) AND AccountType = 'Credit Card' AND Classification = 'Liability'"
    }


def test_the_account_type_description_states_both_vocabularies(
    mock_settings, registry, credentials, actor
) -> None:
    """A caller who reads one spelling and passes it to the other filter gets a
    refusal, so the parameter says which is which."""
    server = build(mock_settings, registry, credentials, actor)
    tool = next(t for t in anyio.run(server.list_tools) if t.name == "list_accounts")
    description = tool.input_schema["properties"]["account_type"]["description"]
    assert "Accounts Payable" in description and "AccountsPayable" in description
    assert "\n" not in description


def test_list_accounts_types_are_the_report_vocabulary_spelled_with_spaces() -> None:
    """Both vocabularies hold the same 15 account types, spelled differently.
    Case-folded, because the entity writes 'Cost of Goods Sold' and the filter
    'CostOfGoodsSold'."""
    from qbo_multicompany_mcp.reports import REPORT_SOURCE_ACCOUNT_TYPES
    from qbo_multicompany_mcp.tools import ACCOUNT_TYPES

    assert len(ACCOUNT_TYPES) == len(set(ACCOUNT_TYPES)) == 15
    assert [t.replace(" ", "").casefold() for t in ACCOUNT_TYPES] == [
        t.casefold() for t in REPORT_SOURCE_ACCOUNT_TYPES
    ]


# --------------------------------------------------------------------------
# run_query / get_transaction
# --------------------------------------------------------------------------


def test_run_query_sends_the_canonical_statement(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.tables["Vendor"] = [vendor("1", "A")]
    server = build(mock_settings, registry, credentials, actor)
    body = call(
        server, "run_query",
        {"entity": "acme_main", "statement": "select * from vendor where active = true"},
    ).structured_content
    assert body["statement"] == "SELECT * FROM Vendor WHERE Active = true"
    assert body["query_entity"] == "Vendor"


def test_run_query_rejects_an_unserved_entity(
    mock_settings, registry, credentials, actor
) -> None:
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="not served"):
        call(server, "run_query", {"entity": "acme_main", "statement": "SELECT * FROM Employee"})


def test_run_query_count_only(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.tables["Vendor"] = [vendor(str(i), f"V{i}") for i in range(1, 8)]
    server = build(mock_settings, registry, credentials, actor)
    body = call(
        server, "run_query",
        {"entity": "acme_main", "statement": "SELECT COUNT(*) FROM Vendor"},
    ).structured_content
    assert body["count_only"] is True and body["total_count"] == 7 and body["rows"] == []


@pytest.mark.parametrize("entity_name", TRANSACTION_TYPES)
def test_run_query_refuses_rows_of_a_transaction_type_before_any_request(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, entity_name: str
) -> None:
    """Nothing projects these rows on this route, so asking is refused up front
    and names the tools that serve them, before a walk reads every page."""
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError) as refused:
        call(server, "run_query", {"entity": "acme_main", "statement": f"SELECT * FROM {entity_name}"})
    assert "list_transactions" in str(refused.value) and "get_transaction" in str(refused.value)
    assert mock_intuit.requests == [], "refused before any request"


def test_an_unreadable_journal_stops_the_data_tools_before_any_request(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """A stopped row is refused before its access token is looked at, valid or
    not, and the refusal says what to run."""
    server = build(mock_settings, registry, credentials, actor)
    store = mock_settings.open_store(credentials=credentials)
    store.journal_path.write_bytes(b"not even encrypted")
    with pytest.raises(ToolError, match="verify acme_main"):
        call(server, "list_vendors", {"entity": "acme_main"})
    assert mock_intuit.requests == [], "refused before any request"


def test_run_query_still_counts_a_transaction_type(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.tables["Invoice"] = [{"Id": str(i)} for i in range(1, 4)]
    server = build(mock_settings, registry, credentials, actor)
    body = call(
        server, "run_query",
        {"entity": "acme_main", "statement": "SELECT COUNT(*) FROM Invoice"},
    ).structured_content
    assert body["count_only"] is True and body["total_count"] == 3 and body["rows"] == []


def test_every_served_entity_has_rows_or_is_a_transaction_type() -> None:
    """The refusal covers exactly the transaction types: a list entity that lost
    its projection would otherwise read as refused for the wrong reason."""
    for name in served_entities():
        assert has_projection(name) != (name in TRANSACTION_TYPES), name


def test_get_transaction_projects_lines_and_drops_card_holder_data(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.tables["Purchase"] = [{
        "Id": "42", "TxnDate": "2026-02-01", "TotalAmt": 100.0, "PaymentType": "CreditCard",
        "EntityRef": {"value": "7", "name": "Acme", "type": "Vendor"},
        "CreditChargeInfo": {"NameOnAcct": "LEAKED", "CcExpiryMonth": 1},
        "CustomerMemo": {"value": "customer facing"}, "SyncToken": "3",
        "Line": [{"Id": "1", "Amount": 100.0, "Description": "paper",
                  "DetailType": "AccountBasedExpenseLineDetail",
                  "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "55", "name": "Office"}}}],
    }]
    server = build(mock_settings, registry, credentials, actor)
    body = call(
        server, "get_transaction",
        {"entity": "acme_main", "transaction_type": "Purchase", "id": "42"},
    ).structured_content

    assert body["transaction_type"] == "Purchase"
    assert body["transaction"]["lines"][0]["description"] == "paper"
    assert body["transaction"]["entity_ref"]["type"] == "Vendor"

    import json

    dumped = json.dumps(body)
    for leaked in ("LEAKED", "customer facing", "SyncToken"):
        assert leaked not in dumped


def test_get_transaction_rejects_an_unknown_type(
    mock_settings, registry, credentials, actor
) -> None:
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="transaction_type must be one of"):
        call(server, "get_transaction",
             {"entity": "acme_main", "transaction_type": "Estimate", "id": "1"})


def test_a_missing_transaction_says_not_found_not_no_data(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.tables["Purchase"] = []
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="not found, not that it has no data"):
        call(server, "get_transaction",
             {"entity": "acme_main", "transaction_type": "Purchase", "id": "999"})


# --------------------------------------------------------------------------
# list_transactions
# --------------------------------------------------------------------------


REPORT = {
    "Header": {"ReportName": "TransactionList", "StartPeriod": "2026-01-01",
               "EndPeriod": "2026-06-30", "Currency": "USD"},
    "Columns": {"Column": [
        {"ColTitle": "Date", "ColType": "Date", "MetaData": [{"Name": "ColKey", "Value": "tx_date"}]},
        {"ColTitle": "Type", "ColType": "String", "MetaData": [{"Name": "ColKey", "Value": "txn_type"}]},
        {"ColTitle": "Num", "ColType": "String", "MetaData": [{"Name": "ColKey", "Value": "doc_num"}]},
        {"ColTitle": "Amount", "ColType": "Money", "MetaData": [{"Name": "ColKey", "Value": "subt_nat_amount"}]},
    ]},
    "Rows": {"Row": [
        {"ColData": [{"value": "2026-02-01"}, {"value": "Purchase", "id": "42"},
                     {"value": "00123"}, {"value": "1,234.50"}]},
    ]},
}


def test_list_transactions_keeps_routing_information(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.reports["TransactionList"] = REPORT
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_transactions", {
        "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
    }).structured_content

    cells = {c["column_key"]: c for c in body["rows"][0]["cells"]}
    assert cells["txn_type"]["text"] == "Purchase"
    assert cells["txn_type"]["entity_id"] == "42"
    assert cells["doc_num"]["text"] == "00123"
    assert cells["subt_nat_amount"]["amount"] == "1234.50"


def test_every_column_of_list_transactions_says_how_it_got_in(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.reports["TransactionList"] = REPORT
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_transactions", {
        "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-01",
    }).structured_content
    assert body["columns"]
    assert all(column["admitted_by"] for column in body["columns"])


def test_list_transactions_reports_unknown_completeness(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """The report publishes no total and no cap signal, so completeness cannot
    be established -- and absence of a signal does not prove completeness."""
    mock_intuit.reports["TransactionList"] = REPORT
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_transactions", {
        "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
    }).structured_content
    assert body["completeness"] == "unknown"
    assert "does not prove" in body["note"] or "cannot be established" in body["note"]
    assert "withheld" not in body["summary"], "nothing was trimmed, so nothing was withheld"


def test_a_local_trim_makes_list_transactions_partial_and_its_first_sentence_says_so(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """QuickBooks returns two rows and max_rows keeps one. A trim is known loss,
    as the report tools say: partial, and the first sentence says rows were
    withheld. Looking at anomalies alone would replace the flattener's
    `partial` with `unknown`, and the summary would say "completeness unknown"
    while client_truncated is true."""
    import copy

    report = copy.deepcopy(REPORT)
    report["Rows"]["Row"].append(copy.deepcopy(report["Rows"]["Row"][0]))
    mock_intuit.reports["TransactionList"] = report
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_transactions", {
        "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30", "max_rows": 1,
    }).structured_content
    assert (body["upstream_row_count"], body["count"], body["client_truncated"]) == (2, 1, True)
    assert body["completeness"] == "partial"
    first = body["summary"].split(". ")[0]
    assert "1 withheld" in first and "max_rows" in first
    assert "completeness partial" in body["summary"]


@pytest.mark.parametrize(
    ("start", "end"),
    [("2026-01-01", "2026-07-01"), ("2026-01-01", "2026-12-31"), ("2026-01-15", "2026-07-16")],
)
def test_a_window_wider_than_six_months_is_refused(
    mock_settings, registry, credentials, actor, start, end
) -> None:
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="at most 6 calendar months"):
        call(server, "list_transactions",
             {"entity": "acme_main", "start_date": start, "end_date": end})


def test_a_six_month_window_is_allowed(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.reports["TransactionList"] = REPORT
    server = build(mock_settings, registry, credentials, actor)
    result = call(server, "list_transactions",
                  {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30"})
    assert result.is_error is False


def test_an_unavailable_column_is_refused_with_the_available_list(
    mock_settings, registry, credentials, actor
) -> None:
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="not available"):
        call(server, "list_transactions", {
            "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-01",
            "columns": ["email"],
        })


def test_an_invalid_cleared_value_is_refused(
    mock_settings, registry, credentials, actor
) -> None:
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="cleared must be one of"):
        call(server, "list_transactions", {
            "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-01",
            "cleared": "Pending",
        })


def test_only_allowlisted_columns_are_requested(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """The allowlist applies to the request, so a withheld column is never
    fetched in the first place."""
    from qbo_multicompany_mcp.reports import SENSITIVE_COLUMN_KEYS

    mock_intuit.reports["TransactionList"] = REPORT
    server = build(mock_settings, registry, credentials, actor)
    call(server, "list_transactions",
         {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-01"})

    _, params = mock_intuit.report_queries[-1]
    requested = set(params["columns"].split(","))
    assert not (requested & SENSITIVE_COLUMN_KEYS)


def test_accounting_method_is_never_sent(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """Absent from the documented parameter table, and a live sandbox probe saw
    no effect on the rows in one window, so it is not sent. If a probe later
    shows an effect, this test changes with the code."""
    mock_intuit.reports["TransactionList"] = REPORT
    server = build(mock_settings, registry, credentials, actor)
    call(server, "list_transactions",
         {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-01"})
    _, params = mock_intuit.report_queries[-1]
    assert "accounting_method" not in params


#: The nine columns QuickBooks returns when TransactionList is sent no
#: ``columns`` (observed in sandbox 2026-09-24), in its order. Spelled out here
#: rather than imported so a change to the constant cannot also change what the
#: test expects.
INTUIT_TRANSACTION_LIST_DEFAULT = [
    "tx_date", "txn_type", "doc_num", "is_no_post", "name", "memo",
    "account_name", "other_account", "subt_nat_amount",
]


def test_list_transactions_requests_intuits_nine_default_columns_explicitly(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """No ``columns`` means Intuit's own nine, named in the request, not every
    allowlisted key (26, over the limit of 24)."""
    mock_intuit.reports["TransactionList"] = REPORT
    server = build(mock_settings, registry, credentials, actor)
    call(server, "list_transactions",
         {"entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-01"})

    _, params = mock_intuit.report_queries[-1]
    assert params["columns"].split(",") == INTUIT_TRANSACTION_LIST_DEFAULT


def test_list_transactions_refuses_twenty_five_columns_before_any_request(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """Intuit's 504 warning applies here as it does to the report tools."""
    from qbo_multicompany_mcp.reports import allowed_column_keys

    many = sorted(allowed_column_keys("TransactionList"))[:25]
    assert len(many) == 25
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="25 or more"):
        call(server, "list_transactions", {
            "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-01",
            "columns": many,
        })
    assert mock_intuit.report_queries == []


def test_list_transactions_counts_the_routing_columns_it_adds(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """23 keys without tx_date and txn_type become a 25-column request once
    the routing columns are added. The rule's reason is the request (Intuit's
    504 warning), so that request is refused too."""
    from qbo_multicompany_mcp.reports import allowed_column_keys

    keys = sorted(allowed_column_keys("TransactionList") - {"tx_date", "txn_type"})[:23]
    assert len(keys) == 23
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="would carry 25 columns"):
        call(server, "list_transactions", {
            "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-01",
            "columns": keys,
        })
    assert mock_intuit.report_queries == []


def test_list_transactions_still_sends_a_caller_list_under_the_limit(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.reports["TransactionList"] = REPORT
    server = build(mock_settings, registry, credentials, actor)
    call(server, "list_transactions", {
        "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-01",
        "columns": ["memo", "klass_name"],
    })
    _, params = mock_intuit.report_queries[-1]
    assert set(params["columns"].split(",")) == {"memo", "klass_name", "tx_date", "txn_type"}


def test_list_transactions_takes_customer_and_vendor_as_lists_of_ids(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """Intuit documents TransactionList's customer and vendor as "One or more
    comma separated ... IDs", so each takes a list and sends one
    comma-separated value."""
    mock_intuit.reports["TransactionList"] = REPORT
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_transactions", {
        "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-03-01",
        "vendor": ["1", "2"], "customer": ["3"],
    }).structured_content
    _, params = mock_intuit.report_queries[-1]
    assert (params["vendor"], params["customer"]) == ("1,2", "3")
    assert set(body["filters_sent"]) == {"vendor", "customer"}


# --------------------------------------------------------------------------
# list_changes
# --------------------------------------------------------------------------


def cdc_payload(blocks: list[dict]) -> dict:
    return {"CDCResponse": [{"QueryResponse": blocks}]}


def since_now() -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")


def test_the_cap_is_aggregate_across_all_types(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """600 of one type and 400 of another saturates it with neither reaching
    1000."""
    mock_intuit.cdc = cdc_payload([
        {"Vendor": [{"Id": str(i), "MetaData": {"LastUpdatedTime": "2026-09-01"}}
                    for i in range(600)]},
        {"Customer": [{"Id": str(i), "MetaData": {"LastUpdatedTime": "2026-09-01"}}
                      for i in range(400)]},
    ])
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_changes", {
        "entity": "acme_main", "since": since_now(),
        "types": ["Vendor", "Customer"], "limit": 5000,
    }).structured_content

    assert body["upstream_object_count"] == 1000
    assert body["possibly_truncated"] is True
    assert body["completeness"] == "unknown"
    assert body["checkpoint_safe"] is False
    assert set(body["uncertain_types"]) == {"Vendor", "Customer"}


def test_uncertainty_propagates_to_every_requested_type(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """The cap is shared, so a type with two changes is uncertain too."""
    mock_intuit.cdc = cdc_payload([
        {"Vendor": [{"Id": str(i)} for i in range(998)]},
        {"Customer": [{"Id": "x"}, {"Id": "y"}]},
    ])
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_changes", {
        "entity": "acme_main", "since": since_now(),
        "types": ["Vendor", "Customer"], "limit": 5000,
    }).structured_content
    assert "Customer" in body["uncertain_types"]


def test_deletions_are_marked_and_carry_no_row(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.cdc = cdc_payload([
        {"Vendor": [
            {"Id": "1", "status": "Deleted", "MetaData": {"LastUpdatedTime": "2026-09-02"}},
            vendor("2", "Still Here"),
        ]},
    ])
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_changes", {
        "entity": "acme_main", "since": since_now(), "types": ["Vendor"], "full": True,
    }).structured_content

    deleted = [o for o in body["objects"] if o["status"] == "Deleted"]
    assert len(deleted) == 1 and deleted[0]["id"] == "1"
    assert deleted[0]["row"] is None, "a deleted object has no projected row"
    live = [o for o in body["objects"] if not o["status"]][0]
    assert live["row"]["display_name"] == "Still Here"


def test_client_trimming_is_reported_separately_from_upstream_truncation(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.cdc = cdc_payload([{"Vendor": [{"Id": str(i)} for i in range(10)]}])
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_changes", {
        "entity": "acme_main", "since": since_now(), "types": ["Vendor"], "limit": 3,
    }).structured_content

    assert body["returned_count"] == 3
    assert body["upstream_object_count"] == 10
    assert body["truncated"] is True
    assert body["possibly_truncated"] is False
    assert body["completeness"] == "partial"
    assert body["checkpoint_safe"] is False


def test_a_complete_response_is_a_safe_checkpoint(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.cdc = cdc_payload([{"Vendor": [{"Id": "1"}]}])
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_changes", {
        "entity": "acme_main", "since": since_now(), "types": ["Vendor"],
    }).structured_content
    assert body["completeness"] == "complete"
    assert body["checkpoint_safe"] is True


def test_an_empty_entry_is_a_type_with_no_changes(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """The live shape: one entry per type, empty for a type with no changes,
    and paging fields beside a type's objects. None of that is malformed."""
    mock_intuit.cdc = cdc_payload([
        {},
        {"Account": [{"Id": "1"}], "maxResults": 1, "startPosition": 1},
        {},
    ])
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_changes", {"entity": "acme_main", "since": since_now()}).structured_content
    assert body["upstream_object_count"] == 1
    assert body["completeness"] == "complete"
    assert body["checkpoint_safe"] is True


#: A response that is not the documented shape, one level at a time.
MALFORMED_CHANGES = {
    "no envelope": {},
    "envelope not a list": {"CDCResponse": {"QueryResponse": [{}]}},
    "no block": {"CDCResponse": []},
    "block not an object": {"CDCResponse": ["x"]},
    "block without entries": {"CDCResponse": [{}]},
    "entries not a list": {"CDCResponse": [{"QueryResponse": {}}]},
    "no entries": {"CDCResponse": [{"QueryResponse": []}]},
    # Beside a whole block, so the count of entries can't be what refuses it.
    "a second block without entries": {"CDCResponse": [{"QueryResponse": [{"Vendor": [{"Id": "1"}]}]}, {}]},
    "entry not an object": cdc_payload([{"Vendor": [{"Id": "1"}]}, "x"]),
    "objects not a list": cdc_payload([{"Vendor": {"Id": "1"}}]),
    "object not an object": cdc_payload([{"Vendor": [{"Id": "1"}, "x"]}]),
    # Each of these becomes text, and the text of an object would carry what
    # is nested in it past the projector.
    "id an object": cdc_payload([{"Vendor": [{"Id": {"Email": "nested"}}]}]),
    "status a list": cdc_payload([{"Vendor": [{"Id": "1", "status": ["Deleted", "nested"]}]}]),
    "timestamp an object": cdc_payload(
        [{"Vendor": [{"Id": "1", "MetaData": {"LastUpdatedTime": {"Email": "nested"}}}]}]
    ),
    # Every change Intuit documents names its object, a deletion included, and
    # carries its metadata as an object.
    "id missing": cdc_payload([{"Vendor": [{"status": "Deleted"}]}]),
    "id empty": cdc_payload([{"Vendor": [{"Id": "", "status": "Deleted"}]}]),
    "metadata not an object": cdc_payload([{"Vendor": [{"Id": "1", "MetaData": "2026-11-01"}]}]),
}


@pytest.mark.parametrize("payload", list(MALFORMED_CHANGES.values()), ids=list(MALFORMED_CHANGES))
def test_a_malformed_change_response_is_refused_never_called_complete(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, payload
) -> None:
    """Skipping what cannot be read leaves a count that looks complete, and a
    complete count is what makes a result a safe checkpoint. So a response
    that is not the documented shape, at any level, is refused whole."""
    mock_intuit.cdc = payload
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="documented shape"):
        call(server, "list_changes",
             {"entity": "acme_main", "since": since_now(), "types": ["Vendor"]})


#: Entries that don't account for each type of a Vendor and Customer request
#: once.
UNACCOUNTED_CHANGES = {
    "a type missing": cdc_payload([{"Vendor": [{"Id": "1"}]}]),
    "one empty entry for two types": cdc_payload([{}]),
    "an entry too many": cdc_payload([{"Vendor": [{"Id": "1"}]}, {}, {}]),
    "a type twice": cdc_payload([{"Vendor": [{"Id": "1"}]}, {"Vendor": [{"Id": "2"}]}]),
    "two types in one entry": cdc_payload([{"Vendor": [{"Id": "1"}], "Customer": [{"Id": "2"}]}, {}]),
}

#: Entries of the same request holding anything but a type's objects and its
#: paging fields as single values.
UNREAD_CHANGE_MEMBERS = {
    "an unknown member": cdc_payload([{"Vendor": [{"Id": "1"}], "moreChanges": True}, {}]),
    "an entry of unknown members": cdc_payload([{"Vendor": [{"Id": "1"}]}, {"status": "pending"}]),
    "a paging field that is an object": cdc_payload(
        [{"Vendor": [{"Id": "1"}], "startPosition": {"next": 2}}, {}]
    ),
}


@pytest.mark.parametrize("payload", list(UNACCOUNTED_CHANGES.values()), ids=list(UNACCOUNTED_CHANGES))
def test_a_change_response_must_account_for_each_requested_type_once(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, payload
) -> None:
    """A type no entry accounts for would read as unchanged, and the result as
    a safe checkpoint. In sandbox a response held one entry per requested type,
    so anything else is refused whole."""
    mock_intuit.cdc = payload
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="documented shape"):
        call(server, "list_changes",
             {"entity": "acme_main", "since": since_now(), "types": ["Vendor", "Customer"]})


@pytest.mark.parametrize("payload", list(UNREAD_CHANGE_MEMBERS.values()), ids=list(UNREAD_CHANGE_MEMBERS))
def test_an_entry_holding_anything_but_objects_and_scalar_paging_fields_is_refused(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, payload
) -> None:
    """Anything else an entry holds could change what it means, so the
    response is refused whole rather than read without it."""
    mock_intuit.cdc = payload
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="documented shape"):
        call(server, "list_changes",
             {"entity": "acme_main", "since": since_now(), "types": ["Vendor", "Customer"]})


def test_two_types_each_accounted_for_are_a_safe_checkpoint(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """The control: the same request, each type in an entry of its own."""
    mock_intuit.cdc = cdc_payload([{}, {"Vendor": [{"Id": "1"}], "startPosition": 1, "maxResults": 1}])
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_changes", {
        "entity": "acme_main", "since": since_now(), "types": ["Vendor", "Customer"],
    }).structured_content
    assert body["upstream_object_count"] == 1
    assert body["completeness"] == "complete"
    assert body["checkpoint_safe"] is True


def test_the_lookback_is_thirty_days_and_says_why_a_query_is_no_substitute(
    mock_settings, registry, credentials, actor
) -> None:
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError) as caught:
        call(server, "list_changes", {"entity": "acme_main", "since": old})
    text = str(caught.value)
    assert "30 days" in text
    assert "cannot recover deletions" in text


def test_an_unsupported_change_type_is_refused(
    mock_settings, registry, credentials, actor
) -> None:
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="not a supported change type"):
        call(server, "list_changes",
             {"entity": "acme_main", "since": since_now(), "types": ["Employee"]})


@pytest.mark.parametrize(
    "objects", [[{"Id": "9"}], {"Id": "9"}], ids=["a list", "a single object"]
)
def test_an_unrequested_type_in_the_response_fails_closed(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, objects
) -> None:
    mock_intuit.cdc = cdc_payload([{"Vendor": [{"Id": "1"}], "Purchase": objects}])
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError, match="not requested"):
        call(server, "list_changes",
             {"entity": "acme_main", "since": since_now(), "types": ["Vendor"]})


def test_the_note_says_an_incomplete_response_is_not_a_checkpoint(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.cdc = cdc_payload([{"Vendor": [{"Id": "1"}]}])
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "list_changes", {
        "entity": "acme_main", "since": since_now(), "types": ["Vendor"],
    }).structured_content
    assert "not a safe synchronization checkpoint" in body["note"].replace("NOT", "not")


# --------------------------------------------------------------------------
# summarize_vendor_payments, end to end
# --------------------------------------------------------------------------


def account_type_filter(row, statement: str) -> bool:
    """Make the mock honour `WHERE AccountType = '...'` where a test needs it."""
    if "AccountType" not in statement:
        return True
    wanted = statement.split("AccountType = '", 1)[1].split("'", 1)[0]
    return row.get("AccountType") == wanted


def seed_payment_tables(mock: MockIntuit, *, with_ap: bool) -> None:
    """``with_ap=False`` makes the mock answer a BillPayment read the way Intuit
    answers one on a QuickBooks plan without the feature: fault 5030. The chart
    of accounts is seeded the same either way, because it is not what decides."""
    mock.filters["Account"] = [account_type_filter]
    mock.tables["Account"] = [
        {"Id": "90", "Name": "Accounts Payable", "AccountType": "Accounts Payable"},
        {"Id": "1", "Name": "Checking", "AccountType": "Bank"},
    ]
    if not with_ap:
        mock.faults["BillPayment"] = (
            400,
            {"Fault": {"Error": [{"Message": "x", "Detail": "y", "code": "5030"}],
                       "type": "ValidationFault"}},
        )
    mock.tables["Vendor"] = [
        vendor("7", "Acme Supplies", Active=False),
        vendor("8", "Other Co"),
    ]
    mock.tables["Purchase"] = [
        {"Id": "1", "TxnDate": "2026-02-01", "PaymentType": "Check", "TotalAmt": "100.00",
         "EntityRef": {"value": "7", "name": "Acme Supplies", "type": "Vendor"}},
        {"Id": "2", "TxnDate": "2026-03-01", "PaymentType": "CreditCard", "TotalAmt": "40.00",
         "EntityRef": {"value": "7", "name": "Acme Supplies", "type": "Vendor"}},
        {"Id": "3", "TxnDate": "2026-03-02", "PaymentType": "Check", "TotalAmt": "25.00",
         "EntityRef": {"value": "9", "name": "A Worker", "type": "Employee"}},
        {"Id": "4", "TxnDate": "2026-03-03", "PaymentType": "Check", "TotalAmt": "11.00"},
    ]
    mock.tables["BillPayment"] = [
        {"Id": "20", "TxnDate": "2026-04-01", "PayType": "Check", "TotalAmt": "200.00",
         "VendorRef": {"value": "7", "name": "Acme Supplies"}},
    ]
    mock.tables["JournalEntry"] = [
        {"Id": "30", "TxnDate": "2026-05-01", "TotalAmt": 0, "Line": [
            {"Id": "1", "Amount": "75.00", "JournalEntryLineDetail": {
                "PostingType": "Debit",
                "Entity": {"Type": "Vendor",
                           "EntityRef": {"value": "7", "name": "Acme Supplies"}}}},
        ]},
    ]


def test_candidate_totals_keep_card_apart_and_exclude_non_vendor_payees(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    seed_payment_tables(mock_intuit, with_ap=True)
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "summarize_vendor_payments",
                {"entity": "acme_main", "year": 2026}).structured_content

    acme = body["vendors"][0]
    assert acme["candidate_non_card_amount"] == "300.00"   # 100 purchase + 200 bill payment
    assert acme["candidate_card_amount"] == "40.00"
    assert acme["vendor_id"] == "7"
    assert acme["active"] is False, "an inactive vendor is still matched and named"
    assert acme["display_name"] == "Acme Supplies"
    assert body["excluded_summary"]["payee_is_employee"] == 1
    assert body["needs_review"]["no_payee"] == ["4"]


def test_journal_lines_are_review_only_and_absent_from_totals(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    seed_payment_tables(mock_intuit, with_ap=True)
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "summarize_vendor_payments",
                {"entity": "acme_main", "year": 2026}).structured_content

    review = body["needs_review"]["journal_entry_vendor_lines"]
    assert len(review) == 1
    assert review[0]["direction"] == "Debit"
    assert review[0]["signed_amount"] == "75.00"
    # 75 appears in neither candidate amount.
    assert body["vendors"][0]["candidate_non_card_amount"] == "300.00"
    assert all("75" not in v["candidate_non_card_amount"] for v in body["vendors"])
    assert body["vendors"][0]["candidate_card_amount"] == "40.00"
    journal = [s for s in body["sources"] if s["source"] == "JournalEntry"][0]
    assert journal["reason"] == "review only; never summed"


def test_a_company_without_accounts_payable_states_the_restriction(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """Not an error, and not an empty answer dressed as a complete one."""
    seed_payment_tables(mock_intuit, with_ap=False)
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "summarize_vendor_payments",
                {"entity": "acme_main", "year": 2026}).structured_content

    bill_payment = [s for s in body["sources"] if s["source"] == "BillPayment"][0]
    assert bill_payment["walked"] is False
    assert "5030" in bill_payment["reason"]
    assert "Purchase-only" in bill_payment["reason"]
    assert body["accounts_payable"] == "unavailable"
    # The bill payment is genuinely not counted.
    assert body["vendors"][0]["candidate_non_card_amount"] == "100.00"


def test_the_envelope_carries_its_interpretation_limits(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    seed_payment_tables(mock_intuit, with_ap=True)
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "summarize_vendor_payments",
                {"entity": "acme_main", "year": 2026}).structured_content

    assert body["date_basis"] == "TxnDate"
    assert "would be counted twice" in body["deduplication_note"]
    assert "No threshold" in body["interpretation_note"]
    assert body["consistency"] == "non_snapshot"


def test_detail_rows_respect_include_card_without_changing_totals(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    seed_payment_tables(mock_intuit, with_ap=True)
    server = build(mock_settings, registry, credentials, actor)

    shown = call(server, "summarize_vendor_payments", {
        "entity": "acme_main", "year": 2026, "detail": "rows", "include_card": True,
    }).structured_content
    hidden = call(server, "summarize_vendor_payments", {
        "entity": "acme_main", "year": 2026, "detail": "rows", "include_card": False,
    }).structured_content

    assert any(r["is_card"] for r in shown["rows"])
    assert not any(r["is_card"] for r in hidden["rows"])
    # Per-vendor totals are identical either way.
    assert shown["vendors"][0]["candidate_card_amount"] == "40.00"
    assert hidden["vendors"][0]["candidate_card_amount"] == "40.00"
    assert shown["vendors"][0]["candidate_non_card_amount"] == hidden["vendors"][0]["candidate_non_card_amount"]


@pytest.mark.parametrize("detail", ["rows", "summary"])
def test_an_object_where_a_payment_holds_one_value_reaches_no_result(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, detail: str
) -> None:
    """A payee the vendor list doesn't know is named from the payment itself,
    in the summary as well as the rows. An object there, or in a document
    number, would carry what is nested in it into the result as its text."""
    import json

    seed_payment_tables(mock_intuit, with_ap=True)
    nested = {"PrimaryEmailAddr": {"Address": "nested-canary"}}
    mock_intuit.tables["Purchase"].append(
        {"Id": "5", "TxnDate": "2026-03-04", "PaymentType": "Check", "TotalAmt": "9.00",
         "DocNumber": nested, "EntityRef": {"value": "55", "name": nested, "type": "Vendor"}}
    )
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "summarize_vendor_payments", {
        "entity": "acme_main", "year": 2026, "detail": detail,
    }).structured_content

    assert "nested-canary" not in json.dumps(body)
    unknown = next(v for v in body["vendors"] if v["vendor_id"] == "55")
    assert unknown["display_name"] is None and unknown["candidate_non_card_amount"] == "9.00"


def test_no_company_wide_rollup_is_emitted(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """Candidate totals are per vendor: no total of any kind is emitted for the
    company as a whole. Journal amounts held for review are summed nowhere,
    which test_journal_lines_are_review_only_and_absent_from_totals covers."""
    seed_payment_tables(mock_intuit, with_ap=True)
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "summarize_vendor_payments",
                {"entity": "acme_main", "year": 2026}).structured_content
    assert [key for key in body if "total" in key or "amount" in key] == []


def test_summary_detail_returns_no_rows(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    seed_payment_tables(mock_intuit, with_ap=True)
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "summarize_vendor_payments",
                {"entity": "acme_main", "year": 2026, "detail": "summary"}).structured_content
    assert body["rows"] is None


# --------------------------------------------------------------------------
# Input validation happens before any credential or network work
# --------------------------------------------------------------------------


def unconfigured(mock_settings, registry) -> MCPServer:
    """A server with no stored credential at all."""
    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    return server


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("run_query", {"statement": "SELECT Id, Name FROM Vendor"},
         "does not support field projections"),
        ("run_query", {"statement": "SELECT * FROM Employee"}, "not served"),
        ("get_transaction", {"transaction_type": "Estimate", "id": "1"},
         "transaction_type must be one of"),
        ("list_transactions", {"start_date": "2026-01-01", "end_date": "2026-12-31"},
         "at most 6 calendar months"),
        ("list_transactions", {"start_date": "2026-01-01", "end_date": "2026-02-01",
                               "cleared": "Pending"}, "cleared must be one of"),
        ("list_changes", {"since": "2020-01-01T00:00:00Z"}, "30 days"),
        ("list_changes", {"since": "not-a-date"}, "ISO timestamp"),
        ("summarize_vendor_payments", {"year": 2026, "detail": "everything"},
         "detail must be"),
    ],
)
def test_bad_input_is_rejected_without_a_credential(
    mock_settings, registry, tool, args, expected
) -> None:
    """A malformed argument should cost nothing: no credential read, no guard
    fetch, no request.

    Validating after the guard would answer 'no usable QuickBooks credential' to
    a malformed statement, which sends a model to fix the wrong thing.
    """
    server = unconfigured(mock_settings, registry)
    with pytest.raises(ToolError, match=expected):
        call(server, tool, {"entity": "acme_main", **args})


def test_a_valid_call_without_a_credential_still_says_so(
    mock_settings, registry
) -> None:
    """The mirror: the credential error must still be reachable, or the tests
    above would pass with everything rejected for the wrong reason."""
    server = unconfigured(mock_settings, registry)
    with pytest.raises(ToolError, match="auth connect acme_main"):
        call(server, "run_query",
             {"entity": "acme_main", "statement": "SELECT * FROM Vendor"})


def test_an_unavailable_column_is_named_by_position_not_by_value(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """The column list is the caller's text, so a refusal never quotes it."""
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError) as caught:
        call(server, "list_transactions", {
            "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-01-31",
            "columns": ["tx_date", "IGNOREPREVIOUSINSTRUCTIONSXYZ"],
        })
    assert "IGNOREPREVIOUSINSTRUCTIONSXYZ" not in str(caught.value)
    assert "columns[1]" in str(caught.value)


# --------------------------------------------------------------------------
# list_transactions: a bad cell is disclosed, a bad column definition fails
# --------------------------------------------------------------------------


def _with_cells(amount_text: str) -> dict:
    import copy

    report = copy.deepcopy(REPORT)
    report["Rows"]["Row"][0]["ColData"][3] = {"value": amount_text}
    return report


def test_a_malformed_amount_makes_list_transactions_partial_and_says_so_first(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.reports["TransactionList"] = _with_cells("twelve dollars")
    server = build(mock_settings, registry, credentials, actor)
    result = call(server, "list_transactions", {
        "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
    })
    body = result.structured_content
    assert body["completeness"] == "partial"
    assert body["anomalies"] == [{
        "row_id": "0", "column_key": "subt_nat_amount", "kind": "malformed_amount",
        "detail": body["anomalies"][0]["detail"],
    }]
    cells = {c["column_key"]: c for c in body["rows"][0]["cells"]}
    assert cells["subt_nat_amount"]["text"] == "twelve dollars"
    assert cells["subt_nat_amount"]["amount"] is None
    assert cells["subt_nat_amount"]["currency"] is None

    # The model reads the text. Its first sentence says what happened.
    text = result.content[0].text
    assert text.startswith('{"summary":"PARTIAL')


def test_a_clean_list_transactions_result_leads_with_its_summary_too(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    mock_intuit.reports["TransactionList"] = REPORT
    server = build(mock_settings, registry, credentials, actor)
    result = call(server, "list_transactions", {
        "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
    })
    assert result.structured_content["anomalies"] == []
    assert result.content[0].text.startswith('{"summary":"')
    assert not result.structured_content["summary"].startswith("PARTIAL")


def test_a_malformed_column_definition_fails_list_transactions_outright(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """A structural failure, end to end: an error, no rows, no canary."""
    import copy

    report = copy.deepcopy(REPORT)
    report["Columns"]["Column"][0] = "not-a-column-definition"
    report["Rows"]["Row"][0]["ColData"][0] = {"value": "PRIVATE-CANARY-4471"}
    mock_intuit.reports["TransactionList"] = report
    server = build(mock_settings, registry, credentials, actor)
    with pytest.raises(ToolError) as caught:
        call(server, "list_transactions", {
            "entity": "acme_main", "start_date": "2026-01-01", "end_date": "2026-06-30",
        })
    message = str(caught.value)
    assert "PRIVATE-CANARY-4471" not in message
    assert "column definition" in message
    assert "failed unexpectedly" not in message


def test_an_accounting_client_that_cant_be_built_is_reported_in_fixed_text(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building the TLS context opens files. A failure there reaches the model
    as its own fixed text, with no path in it, not as the generic failure."""
    from qbo_multicompany_mcp import client as client_module

    def unbuildable(**kwargs: object) -> None:
        raise PermissionError(13, "Permission denied", "/private-canary-path/ca-bundle.pem")

    server = build(mock_settings, registry, credentials, actor)
    monkeypatch.setattr(client_module, "isolated_client", unbuildable)
    with pytest.raises(ToolError) as caught:
        call(server, "list_accounts", {"entity": "acme_main"})
    message = str(caught.value)
    assert "could not be built" in message
    assert "failed unexpectedly" not in message
    assert "private-canary-path" not in message
