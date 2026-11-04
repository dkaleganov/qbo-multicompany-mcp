"""Accounts payable is established by first use, not guessed.

Preferences has no reliable flag for it, so the first AP-only call settles it:
a failure with the documented feature fault marks ``accounts_payable`` false
for that entity, with no retry, and the answer is cached per entity per
process. The feature fault is 5030 (Intuit's "Review QuickBooks Online
Accounting API error codes" page); 6190 is the subscription fault and means
something else entirely.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from .conftest import REALM_MAIN, MockIntuit, make_token_response
from .test_changes_and_tools import build, call, seed_payment_tables


def fault_body(code: str) -> dict:
    return {
        "Fault": {
            "Error": [{"Message": "should-not-appear", "Detail": "should-not-appear", "code": code}],
            "type": "ValidationFault",
        },
        "time": "2026-09-23T00:00:00Z",
    }


def bill_payment_requests(mock: MockIntuit) -> int:
    return sum(1 for r in mock.requests if "FROM BillPayment" in r[2].get("query", ""))


def account_count_requests(mock: MockIntuit) -> int:
    return sum(
        1 for r in mock.requests
        if "COUNT(*)" in r[2].get("query", "").upper().replace(" ", "")
        and "FROM Account " in r[2].get("query", "") + " "
    )


def test_accounts_payable_is_established_by_the_feature_fault_not_the_chart(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """The chart holds an AP account; Intuit refuses the BillPayment read with
    5030. The fault is the answer. The chart is not consulted."""
    seed_payment_tables(mock_intuit, with_ap=True)
    mock_intuit.faults["BillPayment"] = (400, fault_body("5030"))
    server = build(mock_settings, registry, credentials, actor)

    body = call(server, "summarize_vendor_payments",
                {"entity": "acme_main", "year": 2026}).structured_content

    bill_payment = [s for s in body["sources"] if s["source"] == "BillPayment"][0]
    assert bill_payment["walked"] is False
    assert "5030" in bill_payment["reason"]
    assert body["accounts_payable"] == "unavailable"
    assert body["vendors"][0]["candidate_non_card_amount"] == "100.00"
    assert account_count_requests(mock_intuit) == 0, "the chart of accounts was consulted"
    assert bill_payment_requests(mock_intuit) == 1


def test_the_feature_fault_is_cached_with_the_capability_state(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """Once known, no retry. Invalidated with the rest of the capability state,
    under the same consent-epoch key: a re-consent may be a different company
    file entirely."""
    seed_payment_tables(mock_intuit, with_ap=True)
    mock_intuit.faults["BillPayment"] = (400, fault_body("5030"))
    server = build(mock_settings, registry, credentials, actor)

    call(server, "summarize_vendor_payments", {"entity": "acme_main", "year": 2026})
    call(server, "summarize_vendor_payments", {"entity": "acme_main", "year": 2026})
    assert bill_payment_requests(mock_intuit) == 1, "a known-unavailable feature was retried"

    info = call(server, "get_company_info", {"entity": "acme_main"}).structured_content
    assert info["capabilities"]["accounts_payable"] == "unavailable"
    assert info["capabilities"]["accounts_payable_fault_code"] == "5030"

    # Re-consent: the cache key changes, so the question is asked again.
    import time

    time.sleep(0.01)
    mock_settings.open_store(credentials=credentials).commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor,
    )
    call(server, "summarize_vendor_payments", {"entity": "acme_main", "year": 2026})
    assert bill_payment_requests(mock_intuit) == 2


def test_accounts_payable_is_unknown_until_first_use_and_available_after_a_successful_read(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """Before either outcome there is an explicit UNKNOWN, disclosed."""
    seed_payment_tables(mock_intuit, with_ap=True)
    server = build(mock_settings, registry, credentials, actor)

    before = call(server, "get_company_info", {"entity": "acme_main"}).structured_content
    assert before["capabilities"]["accounts_payable"] == "unknown"
    assert before["capabilities"]["accounts_payable_fault_code"] is None

    body = call(server, "summarize_vendor_payments",
                {"entity": "acme_main", "year": 2026}).structured_content
    assert body["accounts_payable"] == "available"
    bill_payment = [s for s in body["sources"] if s["source"] == "BillPayment"][0]
    assert bill_payment["walked"] is True

    after = call(server, "get_company_info", {"entity": "acme_main"}).structured_content
    assert after["capabilities"]["accounts_payable"] == "available"
    listed = call(server, "list_entities", {}).structured_content
    entity = {e["entity"]: e for e in listed["entities"]}["acme_main"]
    assert entity["capabilities"]["accounts_payable"] == "available"


def test_a_subscription_fault_is_not_read_as_no_accounts_payable(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """6190 says the company's subscription has a problem. That is a failed
    call, not a plan without accounts payable, and it leaves the state unknown."""
    seed_payment_tables(mock_intuit, with_ap=True)
    mock_intuit.faults["BillPayment"] = (400, fault_body("6190"))
    server = build(mock_settings, registry, credentials, actor)

    with pytest.raises(ToolError) as caught:
        call(server, "summarize_vendor_payments", {"entity": "acme_main", "year": 2026})
    assert "fault=6190" in str(caught.value)
    assert "subscription" in str(caught.value)
    assert "should-not-appear" not in str(caught.value)

    info = call(server, "get_company_info", {"entity": "acme_main"}).structured_content
    assert info["capabilities"]["accounts_payable"] == "unknown"


def test_the_disclosure_says_purchase_only_and_makes_no_claim_about_lower_plans(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    seed_payment_tables(mock_intuit, with_ap=True)
    mock_intuit.faults["BillPayment"] = (400, fault_body("5030"))
    server = build(mock_settings, registry, credentials, actor)
    body = call(server, "summarize_vendor_payments",
                {"entity": "acme_main", "year": 2026}).structured_content
    reason = [s for s in body["sources"] if s["source"] == "BillPayment"][0]["reason"]
    assert "lower QuickBooks plans" not in reason
    assert "Purchase-only" in reason


# --------------------------------------------------------------------------
# Cached per entity PER PROCESS
# --------------------------------------------------------------------------


def _fresh_process(mock_settings, registry):
    """A second server on the same store with no new consent: what a restart
    is. (`build` commits a consent, which would invalidate the cache for a
    different reason and prove nothing about the process boundary.)"""
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    server = MCPServer(name="t2", version="0")
    register_tools(server, mock_settings, registry)
    return server


def test_accounts_payable_is_re_established_by_a_fresh_process(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """The first process learns the feature fault; the company's plan then
    gains accounts payable; the operator restarts. A new process that imported
    'unavailable' from the store would omit BillPayment again until a
    re-consent, so it asks once itself."""
    seed_payment_tables(mock_intuit, with_ap=True)
    mock_intuit.faults["BillPayment"] = (400, fault_body("5030"))
    first = build(mock_settings, registry, credentials, actor)
    body = call(first, "summarize_vendor_payments",
                {"entity": "acme_main", "year": 2026}).structured_content
    assert body["accounts_payable"] == "unavailable"
    assert bill_payment_requests(mock_intuit) == 1

    del mock_intuit.faults["BillPayment"]  # the company upgraded
    second = _fresh_process(mock_settings, registry)

    listed = call(second, "list_entities", {}).structured_content
    entity = {e["entity"]: e for e in listed["entities"]}["acme_main"]
    assert entity["capabilities"]["accounts_payable"] == "unknown", (
        "a fresh process reported another process's finding as its own"
    )

    asked_before = bill_payment_requests(mock_intuit)
    body = call(second, "summarize_vendor_payments",
                {"entity": "acme_main", "year": 2026}).structured_content
    # A successful walk is a count and a page; the point is that it happened.
    assert bill_payment_requests(mock_intuit) > asked_before, "the new process never asked"
    assert body["accounts_payable"] == "available"
    bill_payment = [s for s in body["sources"] if s["source"] == "BillPayment"][0]
    assert bill_payment["walked"] is True


def test_accounts_payable_is_still_cached_within_one_process(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit
) -> None:
    """The control: within a process the answer is asked once, and the
    request-free list reports it -- this process's finding, not the store's."""
    seed_payment_tables(mock_intuit, with_ap=True)
    mock_intuit.faults["BillPayment"] = (400, fault_body("5030"))
    server = build(mock_settings, registry, credentials, actor)
    call(server, "summarize_vendor_payments", {"entity": "acme_main", "year": 2026})
    call(server, "summarize_vendor_payments", {"entity": "acme_main", "year": 2026})
    assert bill_payment_requests(mock_intuit) == 1
    listed = call(server, "list_entities", {}).structured_content
    entity = {e["entity"]: e for e in listed["entities"]}["acme_main"]
    assert entity["capabilities"]["accounts_payable"] == "unavailable"
    assert entity["capabilities"]["accounts_payable_fault_code"] == "5030"
