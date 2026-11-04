"""Pins the facts a live sandbox probe settled, and the rules they produced.

Each test names the probe result it encodes. Two of them pin a *method* rather
than a fact: a parameter is checked against a bogus-parameter control, since a
200 says the request was accepted, not that the parameter was understood; and a
sandbox result isn't taken for production's, since `ORDERBY Id` works in
sandbox and is gone from production.
"""

from __future__ import annotations

import json

import pytest

from qbo_multicompany_mcp.netpolicy import ACCOUNTING_QUERY_KEYS_BY_TAIL
from qbo_multicompany_mcp.projections import TRANSACTION_SPEC, project_transaction
from qbo_multicompany_mcp.query_grammar import ENTITY_REGISTRY, QueryError, parse
from qbo_multicompany_mcp.reports import (
    ENUMERATED_FILTERS,
    REPORT_SOURCE_ACCOUNT_TYPES,
    REPORT_TRANSACTION_TYPES,
    REPORT_TRANSACTION_TYPES_REJECTED,
    SILENT_MISMATCH_FILTERS,
    allowed_column_keys,
    explain_rejected_source_account_type,
    explain_rejected_transaction_type,
)
from qbo_multicompany_mcp.vendor_payments import (
    BILL_PAYMENT_PAY_TYPES,
    PURCHASE_PAYMENT_TYPES,
    Classification,
    classify_bill_payment,
    classify_purchase,
)


def run_report(mock_settings, registry, credentials, actor, mock_intuit, extra,
               *, report=None, window=("2026-01-01", "2026-03-01")):
    """Drive list_transactions against the mock and return the envelope."""
    import anyio
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    from .conftest import REALM_MAIN, make_token_response
    from .test_changes_and_tools import REPORT

    store = mock_settings.open_store(credentials=credentials)
    if store.get_row(REALM_MAIN) is None:
        store.commit_consent(
            entity="acme_main", realm_id=REALM_MAIN,
            response=make_token_response(), actor=actor,
        )
    mock_intuit.reports["TransactionList"] = REPORT if report is None else report
    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    args = {"entity": "acme_main", "start_date": window[0], "end_date": window[1]}
    args.update(extra)
    return anyio.run(lambda: server.call_tool("list_transactions", args)).structured_content


# --------------------------------------------------------------------------
# accounting_method: no observed effect, so not sent
# --------------------------------------------------------------------------


def test_accounting_method_is_not_an_allowed_report_parameter() -> None:
    """Probed live, it made no observed difference to the rows in one sandbox
    window, so whether it's honoured isn't established (docs/api-findings.md,
    item 1).

    A parameter that may do nothing while looking like it does something is
    worse than an absent one, so this server claims no cash-versus-accrual
    distinction this way. Keeping it out of the allowlist means the network
    policy refuses any request carrying it, so it cannot be added back by
    accident.
    """
    allowed = ACCOUNTING_QUERY_KEYS_BY_TAIL["reports/TransactionList"]
    assert "accounting_method" not in allowed


def test_the_differential_validation_rule_is_written_down() -> None:
    """A 200 means the request was accepted, not that the parameter was
    understood, so each parameter is checked against a bogus-parameter
    control."""
    from qbo_multicompany_mcp import reports

    doc = " ".join((reports.__doc__ or "").split())
    assert "no evidence for another's" in doc
    assert "its own probe, with its own control" in doc


def test_filters_sent_lists_what_was_sent_and_no_confirmed_flag_exists(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """A differential probe established that the documented filters are
    applied. That is a historical observation about one window, not a
    property of this response, and a boolean emitted True on every call would
    say otherwise. What was sent is a list; a "confirmed" flag is not honest,
    so there is none."""
    body = run_report(
        mock_settings, registry, credentials, actor, mock_intuit,
        {"vendor": ["7"], "cleared": "Cleared"},
    )
    assert set(body["filters_sent"]) == {"vendor", "cleared"}
    assert "filters_confirmed_applied" not in body


def test_the_result_names_the_values_transaction_type_accepts(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """Next to the txn_type cells a caller just read, which are a different
    vocabulary they cannot filter on."""
    body = run_report(mock_settings, registry, credentials, actor, mock_intuit, {})
    assert body["filterable_transaction_types"] == list(REPORT_TRANSACTION_TYPES)
    assert "Purchase" not in body["filterable_transaction_types"]
    assert "third vocabulary" in body["note"]


def test_no_filter_means_an_empty_filters_sent_list(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    body = run_report(mock_settings, registry, credentials, actor, mock_intuit, {})
    assert body["filters_sent"] == []
    assert body["zero_row_recheck"] is None


# --------------------------------------------------------------------------
# Payment type value sets: every documented value confirmed
# --------------------------------------------------------------------------


def test_purchase_payment_types_are_the_confirmed_three() -> None:
    assert PURCHASE_PAYMENT_TYPES == {"Cash", "Check", "CreditCard"}


def test_bill_payment_pay_types_are_the_confirmed_two() -> None:
    """There is no cash bill payment."""
    assert BILL_PAYMENT_PAY_TYPES == {"Check", "CreditCard"}


def test_an_unlisted_value_is_still_unclassified_despite_confirmation() -> None:
    """Confirmed in sandbox is not a contract with production.

    Sandbox honours ORDERBY Id, which Intuit's change log says production
    dropped, so a set confirmed there is a good default, not a guarantee.
    """
    out = Classification()
    classify_purchase(
        {"Id": "1", "EntityRef": {"value": "7", "type": "Vendor"},
         "PaymentType": "DirectDebit", "TotalAmt": "10.00"},
        out,
    )
    classify_bill_payment(
        {"Id": "2", "VendorRef": {"value": "7"}, "PayType": "Cash", "TotalAmt": "10.00"},
        out,
    )
    assert out.rows == []
    assert {u.reason for u in out.unclassified} == {
        "unrecognised_payment_type", "unrecognised_pay_type"
    }


def test_a_missing_payee_is_an_ordinary_outcome() -> None:
    """2 of 35 sandbox purchases have no payee, so a non-empty review list is
    normal rather than a signal of a fault."""
    from qbo_multicompany_mcp import vendor_payments

    doc = " ".join((vendor_payments.__doc__ or "").split())
    assert "2 of 35" in doc
    assert "ordinary outcome" in doc


# --------------------------------------------------------------------------
# ORDERBY: sandbox is not production
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entity", "field"),
    [
        ("Purchase", "TxnDate"),
        ("BillPayment", "TxnDate"),
        ("Vendor", "MetaData.LastUpdatedTime"),
        ("Vendor", "DisplayName"),
    ],
)
def test_confirmed_sortable_keys_are_accepted(entity: str, field: str) -> None:
    """Each confirmed honoured by a live ORDERBY probe."""
    assert field in ENTITY_REGISTRY[entity].sortable
    parse(f"SELECT * FROM {entity} ORDERBY {field} ASC")


def test_orderby_id_stays_rejected_although_sandbox_honours_it() -> None:
    """Production withdrew it in January.

    Accepting a thing because sandbox accepts it is how a server works in
    sandbox and fails on real books.
    """
    with pytest.raises(QueryError) as caught:
        parse("SELECT * FROM Vendor ORDERBY Id")
    message = str(caught.value)
    assert "sandbox" in message
    assert "withdrawn from production" in message


def test_display_name_remains_the_default_name_list_sort() -> None:
    """LastUpdatedTime is sortable on Vendor but is not unique, so two rows
    sharing a timestamp can straddle a page boundary."""
    assert ENTITY_REGISTRY["Vendor"].default_sort == "DisplayName"
    assert ENTITY_REGISTRY["Customer"].default_sort == "DisplayName"


# --------------------------------------------------------------------------
# TransactionList columns and CreditChargeInfo
# --------------------------------------------------------------------------


def test_the_confirmed_amount_column_key_is_allowlisted() -> None:
    """The default response carries 9 columns, with subt_nat_amount as the
    amount key."""
    assert "subt_nat_amount" in allowed_column_keys("TransactionList")


def test_the_column_allowlist_is_a_superset_of_the_default_response() -> None:
    """Nine columns come back by default; the allowlist is wider so a requested
    column is permitted if the endpoint honours the request. The response-side
    allowlist is what actually protects the boundary -- a `columns` request
    cannot be confirmed applied, for the same reason no report parameter can."""
    assert len(allowed_column_keys("TransactionList")) >= 9


def test_credit_charge_info_is_excluded_whatever_sandbox_returned() -> None:
    """Absent from all 16 sandbox payments -- a sandbox company has no real card
    on file to return, so that says little about production."""
    assert not any(rule.source == "CreditChargeInfo" for rule in TRANSACTION_SPEC)
    projected = project_transaction(
        {"Id": "1", "CreditChargeInfo": {"NameOnAcct": "CARDHOLDER", "CcExpiryMonth": 9}}
    )
    assert "CARDHOLDER" not in str(projected)


# --------------------------------------------------------------------------
# transaction_type: a third vocabulary, pinned to proven values
# --------------------------------------------------------------------------


def test_the_accepted_vocabulary_is_the_proven_ten() -> None:
    assert REPORT_TRANSACTION_TYPES == (
        "Bill", "Charge", "Check", "CreditCardCharge", "CreditMemo",
        "Deposit", "Estimate", "Invoice", "JournalEntry", "SalesReceipt",
    )


def test_the_three_vocabularies_really_are_different() -> None:
    """Not the display labels, not the entity names."""
    from qbo_multicompany_mcp.query_grammar import TRANSACTION_TYPES

    accepted = set(REPORT_TRANSACTION_TYPES)
    entity_names = set(TRANSACTION_TYPES)

    # Entity names a caller would most naturally reach for, all rejected.
    for name in ("Purchase", "BillPayment", "Transfer", "VendorCredit"):
        assert name in entity_names
        assert name not in accepted
        assert name in REPORT_TRANSACTION_TYPES_REJECTED

    # Spaced display labels rejected where the closed-up form is accepted.
    for label, closed in (
        ("Journal Entry", "JournalEntry"),
        ("Sales Receipt", "SalesReceipt"),
        ("Credit Memo", "CreditMemo"),
    ):
        assert label in REPORT_TRANSACTION_TYPES_REJECTED
        assert closed in accepted

    # And one the report filter accepts that this server does not serve as an
    # entity at all.
    assert "Estimate" in accepted and "Estimate" not in entity_names


def test_the_published_schema_pins_the_enum(
    mock_settings, registry, credentials, actor
) -> None:
    """Schema-level, so an unrecognised value cannot be sent at all."""
    import anyio
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    schema = tools["list_transactions"].input_schema
    field = schema["properties"]["transaction_type"]
    published = json.dumps(field)
    for value in REPORT_TRANSACTION_TYPES:
        assert value in published
    for rejected in ("Purchase", "BillPayment", "Journal Entry"):
        assert f'"{rejected}"' not in published


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Purchase", "that is an entity name"),
        ("BillPayment", "that is an entity name"),
        ("Sales Receipt", "that is a display label"),
        ("Expense", "that is a display label"),
        ("Nonsense", "not in this filter's vocabulary"),
    ],
)
def test_a_rejected_value_explains_where_it_came_from(value: str, expected: str) -> None:
    """The refusal names the API's actual behaviour: a hard 400.

    Such a value does not 'match nothing' -- the enumerated filters refuse
    loudly -- and a message that misdescribes the failure sends a reader
    looking in the wrong place.
    """
    message = explain_rejected_transaction_type(value)
    assert expected in message
    assert "HTTP 400 Invalid Enumeration" in message
    assert "impossible input" in message


def test_the_backstop_rejects_a_bad_value_on_a_direct_call(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        run_report(mock_settings, registry, credentials, actor, mock_intuit,
                   {"transaction_type": "Purchase"})


def test_an_accepted_value_is_forwarded(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    run_report(mock_settings, registry, credentials, actor, mock_intuit,
               {"transaction_type": "JournalEntry"})
    _, params = mock_intuit.report_queries[-1]
    assert params["transaction_type"] == "JournalEntry"


# --------------------------------------------------------------------------
# Zero rows: the ambiguous case
# --------------------------------------------------------------------------


def empty_report() -> dict:
    from .test_changes_and_tools import REPORT

    return {**REPORT, "Rows": {}}


def test_a_filtered_empty_result_is_rechecked_unfiltered(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """An id filter that matched nothing looks exactly like a window that holds
    nothing. Re-reading the window unfiltered separates the two."""
    from .test_changes_and_tools import REPORT

    empty = empty_report()
    state = {"n": 0}

    def alternating(name, query):
        state["n"] += 1
        # First call is filtered and empty; the re-check is unfiltered.
        return empty if "vendor" in query else REPORT

    mock_intuit.reports["TransactionList"] = empty
    original = type(mock_intuit).report_payload
    type(mock_intuit).report_payload = lambda self, name, query: (
        self.report_queries.append((name, dict(query))) or alternating(name, query)
    )
    try:
        body = run_report(mock_settings, registry, credentials, actor, mock_intuit,
                          {"vendor": ["999"]})
    finally:
        type(mock_intuit).report_payload = original

    recheck = body["zero_row_recheck"]
    assert recheck is not None
    assert recheck["performed"] is True
    assert recheck["unfiltered_row_count"] == 1
    interpretation = recheck["interpretation"]
    assert "No rows matched vendor" in interpretation
    assert "holds 1 transactions unfiltered" in interpretation
    assert "itself partial" not in interpretation
    # Names the filter that can actually mismatch silently.
    assert "matched by id or free text" in interpretation


def test_a_recheck_that_was_itself_partial_says_its_count_may_not_be_exact(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """The re-read lists a row for rows it couldn't read, and nothing says how
    many transactions that row stands for, so the explanation says so."""
    from .test_changes_and_tools import REPORT

    empty = empty_report()
    held = {**REPORT, "Rows": {**REPORT["Rows"], "FutureRow": [{"ColData": [{"value": "2026-03-01"}]}]}}
    mock_intuit.reports["TransactionList"] = empty
    original = type(mock_intuit).report_payload
    type(mock_intuit).report_payload = lambda self, name, query: (
        self.report_queries.append((name, dict(query))) or (empty if "vendor" in query else held)
    )
    try:
        body = run_report(mock_settings, registry, credentials, actor, mock_intuit,
                          {"vendor": ["999"]})
    finally:
        type(mock_intuit).report_payload = original

    recheck = body["zero_row_recheck"]
    assert recheck["unfiltered_row_count"] == 2
    assert "That re-read was itself partial, so the count may not be exact." in recheck["interpretation"]


def test_a_recheck_with_an_unreadable_amount_keeps_its_count_exact(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """A cell this server couldn't read leaves one row per transaction, so the
    count is still exact and the explanation doesn't say otherwise."""
    import copy

    from .test_changes_and_tools import REPORT

    empty = empty_report()
    unreadable = copy.deepcopy(REPORT)
    unreadable["Rows"]["Row"][0]["ColData"][3] = {"value": "not-an-amount"}
    mock_intuit.reports["TransactionList"] = empty
    original = type(mock_intuit).report_payload
    type(mock_intuit).report_payload = lambda self, name, query: (
        self.report_queries.append((name, dict(query))) or (empty if "vendor" in query else unreadable)
    )
    try:
        body = run_report(mock_settings, registry, credentials, actor, mock_intuit,
                          {"vendor": ["999"]})
    finally:
        type(mock_intuit).report_payload = original

    recheck = body["zero_row_recheck"]
    assert recheck["unfiltered_row_count"] == 1
    assert "holds 1 transactions unfiltered" in recheck["interpretation"]
    assert "itself partial" not in recheck["interpretation"]


def test_an_empty_window_is_not_reported_as_holding_no_transactions(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """Zero rows do not prove the window is empty, so the explanation cannot
    say it "holds no transactions at all" while the same result says
    completeness is unknown. What the unfiltered re-read does establish is
    narrower: the filter is not what emptied the result."""
    body = run_report(mock_settings, registry, credentials, actor, mock_intuit,
                      {"vendor": ["999"]}, report=empty_report())
    recheck = body["zero_row_recheck"]
    assert recheck["unfiltered_row_count"] == 0
    interpretation = recheck["interpretation"]
    assert "no transactions at all" not in interpretation
    assert "not what emptied" in interpretation
    assert "not evidence" in interpretation
    assert body["completeness"] == "unknown"


def test_an_unfiltered_empty_result_is_not_rechecked(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """Nothing to disambiguate, so no extra request is spent."""
    body = run_report(mock_settings, registry, credentials, actor, mock_intuit,
                      {}, report=empty_report())
    assert body["zero_row_recheck"] is None
    assert len(mock_intuit.report_queries) == 1


def test_a_non_empty_filtered_result_is_not_rechecked(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    body = run_report(mock_settings, registry, credentials, actor, mock_intuit,
                      {"vendor": ["7"]})
    assert body["zero_row_recheck"] is None
    assert len(mock_intuit.report_queries) == 1


# --------------------------------------------------------------------------
# Validation is not uniform: three classes, each needing its own control
# --------------------------------------------------------------------------


def test_the_three_validation_classes_are_written_down() -> None:
    """One parameter's validation behaviour is no evidence for another's. A
    probe that doesn't check the status code counts HTTP 400s as zero-row
    responses, and the enumerated filters look silent."""
    from qbo_multicompany_mcp import reports

    doc = " ".join((reports.__doc__ or "").split())
    assert "no evidence for another's" in doc
    assert "Every parameter needs its own probe, with its own control" in doc
    assert "HTTP 400" in doc
    assert "shape* is validated and its *existence* is not" in doc


def test_the_two_filter_classes_are_disjoint_and_complete() -> None:
    assert not (SILENT_MISMATCH_FILTERS & ENUMERATED_FILTERS)
    assert SILENT_MISMATCH_FILTERS == {"vendor", "customer", "docnum", "memo"}
    assert ENUMERATED_FILTERS == {"transaction_type", "cleared", "source_account_type"}


def test_only_enumerated_filters_are_pinned_in_the_schema(
    mock_settings, registry, credentials, actor
) -> None:
    """Pinning is possible precisely because a bad value 400s, which makes each
    candidate exactly testable. The id and free-text filters have no vocabulary
    to pin."""
    import anyio
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    props = tools["list_transactions"].input_schema["properties"]

    for name in ("transaction_type", "source_account_type"):
        assert "enum" in json.dumps(props[name]), name
    for name in SILENT_MISMATCH_FILTERS:
        assert "enum" not in json.dumps(props[name]), name


# --------------------------------------------------------------------------
# source_account_type: every documented value proven, and the spacing trap
# --------------------------------------------------------------------------


def test_source_account_types_are_the_account_type_enum_without_spaces() -> None:
    assert len(REPORT_SOURCE_ACCOUNT_TYPES) == 15
    assert "AccountsPayable" in REPORT_SOURCE_ACCOUNT_TYPES
    assert not any(" " in value for value in REPORT_SOURCE_ACCOUNT_TYPES)


def test_the_spacing_trap_is_named_in_the_refusal() -> None:
    """The chart of accounts reports the spaced form, so the value a caller
    reads off an account is not the value that filters on it."""
    message = explain_rejected_source_account_type("Accounts Payable")
    assert "spaces removed" in message
    assert "'AccountsPayable'" in message
    assert "list_accounts" in message
    assert "HTTP 400" in message


def test_list_accounts_really_does_report_the_spaced_form() -> None:
    """Proves the trap is real rather than asserted: this server's own tool
    emits the value that the filter refuses."""
    from qbo_multicompany_mcp.projections import project_account

    projected = project_account({"Id": "1", "Name": "AP", "AccountType": "Accounts Payable"})
    assert projected["account_type"] == "Accounts Payable"
    assert projected["account_type"] not in REPORT_SOURCE_ACCOUNT_TYPES


def test_an_unknown_source_account_type_lists_the_vocabulary() -> None:
    message = explain_rejected_source_account_type("Cryptocurrency")
    assert "not in this filter's vocabulary" in message
    assert "AccountsReceivable" in message


def test_a_spaced_source_account_type_cannot_be_sent(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """The schema enum is the primary gate, so the refusal comes from argument
    validation before the tool body runs -- which is the point of pinning it.

    That message is pydantic's, not this package's, so it names the accepted
    values but not the spacing trap. The trap is carried by the parameter's
    published description instead, where a model reads it BEFORE calling rather
    than after failing.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as caught:
        run_report(mock_settings, registry, credentials, actor, mock_intuit,
                   {"source_account_type": "Accounts Payable"})
    assert "AccountsPayable" in str(caught.value)


def test_the_published_description_warns_about_spacing_before_a_call(
    mock_settings, registry, credentials, actor
) -> None:
    import anyio
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    field = tools["list_transactions"].input_schema["properties"]["source_account_type"]
    description = json.dumps(field)
    assert "SPACES REMOVED" in description
    assert "list_accounts" in description


def test_the_backstop_names_the_trap_for_a_direct_call() -> None:
    """The in-code check covers a caller that bypasses schema validation, and
    it is where the useful message lives."""
    message = explain_rejected_source_account_type("Accounts Payable")
    assert "spaces removed" in message
    assert "'AccountsPayable'" in message


def test_an_accepted_source_account_type_is_forwarded(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    run_report(mock_settings, registry, credentials, actor, mock_intuit,
               {"source_account_type": "CreditCard"})
    _, params = mock_intuit.report_queries[-1]
    assert params["source_account_type"] == "CreditCard"


def test_the_result_names_the_source_account_vocabulary(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    body = run_report(mock_settings, registry, credentials, actor, mock_intuit, {})
    assert body["filterable_source_account_types"] == list(REPORT_SOURCE_ACCOUNT_TYPES)


# --------------------------------------------------------------------------
# cleared: confirmed live
# --------------------------------------------------------------------------


def test_cleared_values_are_the_confirmed_set() -> None:
    from qbo_multicompany_mcp.tools_data import CLEARED_VALUES

    assert CLEARED_VALUES == ("Cleared", "Uncleared", "Reconciled", "Deposited")


# --------------------------------------------------------------------------
# The re-check names the right suspect
# --------------------------------------------------------------------------


def test_an_enumerated_only_zero_result_is_not_blamed_on_a_bad_value(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """An enumerated filter cannot reach zero rows through a bad value, so an
    empty result there is a genuine absence and should be reported as one."""
    from .test_changes_and_tools import REPORT

    empty = {**REPORT, "Rows": {}}

    def alternating(self, name, query):
        self.report_queries.append((name, dict(query)))
        return empty if "transaction_type" in query else REPORT

    original = type(mock_intuit).report_payload
    type(mock_intuit).report_payload = alternating
    try:
        body = run_report(mock_settings, registry, credentials, actor, mock_intuit,
                          {"transaction_type": "Deposit"}, report=empty)
    finally:
        type(mock_intuit).report_payload = original

    interpretation = body["zero_row_recheck"]["interpretation"]
    assert "Every filter applied is enumerated" in interpretation
    assert "genuine absence of matching rows" in interpretation
    assert "matched by id or free text" not in interpretation


def test_a_mixed_zero_result_blames_only_the_unvalidated_filter(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    from .test_changes_and_tools import REPORT

    empty = {**REPORT, "Rows": {}}

    def alternating(self, name, query):
        self.report_queries.append((name, dict(query)))
        return empty if "vendor" in query else REPORT

    original = type(mock_intuit).report_payload
    type(mock_intuit).report_payload = alternating
    try:
        body = run_report(mock_settings, registry, credentials, actor, mock_intuit,
                          {"vendor": ["999"], "cleared": "Cleared"}, report=empty)
    finally:
        type(mock_intuit).report_payload = original

    interpretation = body["zero_row_recheck"]["interpretation"]
    assert "No rows matched cleared, vendor" in interpretation
    assert "vendor is matched by id or free text" in interpretation
    assert "check that first" in interpretation


def test_no_source_comment_claims_a_value_set_is_complete_or_exhaustive() -> None:
    """A 200 proves a value is accepted, not that the set is closed, so no
    source comment may claim a set is closed."""
    import re
    from pathlib import Path

    import qbo_multicompany_mcp

    offenders = []
    for path in sorted(Path(qbo_multicompany_mcp.__file__).parent.glob("*.py")):
        text = " ".join(path.read_text(encoding="utf-8").replace("#:", " ").split()).lower()
        for phrase in ("confirmed complete", "exhaustively tested", "exhaustively confirmed"):
            if re.search(re.escape(phrase), text):
                offenders.append((path.name, phrase))
    assert offenders == []
