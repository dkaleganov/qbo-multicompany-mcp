"""`list_reference_data` returns only declared fields, for every kind it serves.

Seven kinds -- classes, departments, items, terms, payment_methods, tax_codes,
tax_rates -- each walked by the shared count-anchored, deduplicating walker
`list_accounts` uses, each projected through an allowlist finalised against
live shapes observed in sandbox 2026-09-24 (field names only, synthetic values
here). Class and Department returned zero rows there because the features are
off on that company, so their projections come from Intuit's pages and say so.

Every test drives the tool through the registered server against the loopback
mock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from qbo_multicompany_mcp.query_grammar import QueryError

from .conftest import MockIntuit
from .test_changes_and_tools import build, call

KINDS = ("classes", "departments", "items", "terms", "payment_methods", "tax_codes", "tax_rates")
ENTITY_OF = {
    "classes": "Class", "departments": "Department", "items": "Item", "terms": "Term",
    "payment_methods": "PaymentMethod", "tax_codes": "TaxCode", "tax_rates": "TaxRate",
}

#: The marker every undeclared field carries. If it ever reaches a result,
#: the projection walked the payload instead of the spec.
LEAK = "should-not-appear"


def synthetic_rows() -> dict[str, list[dict[str, Any]]]:
    """One or two rows per entity, in the structure of a live sandbox response,
    with every value synthetic, and undeclared fields planted at every depth."""
    meta = {"CreateTime": "2026-01-01T00:00:00-08:00", "LastUpdatedTime": "2026-02-01T00:00:00-08:00",
            "LastModifiedByRef": {"value": LEAK}}
    return {
        "Class": [
            {"Id": "5", "Name": "Example Class", "Active": True, "SubClass": False,
             "FullyQualifiedName": "Example Class", "SyncToken": LEAK, "domain": LEAK, "sparse": False,
             "MetaData": meta},
            {"Id": "6", "Name": "Example Sub", "Active": False, "SubClass": True,
             "ParentRef": {"value": "5", "name": "Example Class", "Undeclared": LEAK},
             "FullyQualifiedName": "Example Class:Example Sub", "MetaData": meta},
        ],
        "Department": [
            {"Id": "1", "Name": "Example Dept", "Active": True, "SubDepartment": False,
             "FullyQualifiedName": "Example Dept", "SyncToken": LEAK, "MetaData": meta},
        ],
        "Item": [
            {"Id": "11", "Name": "Example Widget", "Active": True, "Type": "Inventory", "Sku": "EX-1",
             "Taxable": True, "UnitPrice": 25, "PurchaseCost": 10, "QtyOnHand": 3, "TrackQtyOnHand": True,
             "InvStartDate": "2026-01-01", "SubItem": False, "Level": 0, "FullyQualifiedName": "Example Widget",
             "Description": "Example description", "PurchaseDesc": "Example purchase description",
             "IncomeAccountRef": {"value": "79", "name": "Example Income"},
             "ExpenseAccountRef": {"value": "80", "name": "Example COGS"},
             "AssetAccountRef": {"value": "81", "name": "Example Asset"},
             "PrefVendorRef": {"value": LEAK}, "SalesTaxCodeRef": {"value": LEAK},
             "CustomExtensions": {"x": LEAK}, "SyncToken": LEAK, "domain": LEAK, "MetaData": meta},
            {"Id": "12", "Name": "Example Service", "Active": True, "Type": "Service", "Taxable": False,
             "UnitPrice": 50, "IncomeAccountRef": {"value": "79", "name": "Example Income"},
             "ParentRef": {"value": "11"}, "SubItem": True, "Level": 1, "MetaData": meta},
        ],
        "Term": [
            {"Id": "3", "Name": "Example Net 30", "Active": True, "Type": "STANDARD", "DueDays": 30,
             "DiscountDays": 10, "DiscountPercent": 2, "SyncToken": LEAK, "MetaData": meta},
            {"Id": "4", "Name": "Example Date Driven", "Active": True, "Type": "DATE_DRIVEN",
             "DayOfMonthDue": 15, "DueNextMonthDays": 5, "DiscountDayOfMonth": 1, "MetaData": meta},
        ],
        "PaymentMethod": [
            {"Id": "1", "Name": "Example Card", "Active": True, "Type": "CREDIT_CARD", "SyncToken": LEAK,
             "MetaData": meta},
        ],
        "TaxCode": [
            {"Id": "2", "Name": "Example Tax", "Active": True, "Taxable": True, "TaxGroup": True, "Hidden": False,
             "TaxCodeConfigType": "USER_DEFINED", "Description": "Example tax code description",
             "SalesTaxRateList": {"TaxRateDetail": [
                 {"TaxRateRef": {"value": "3", "name": LEAK}, "TaxTypeApplicable": LEAK, "TaxOrder": 0}]},
             "PurchaseTaxRateList": {"TaxRateDetail": [
                 {"TaxRateRef": {"value": "4", "name": LEAK}, "TaxTypeApplicable": LEAK, "TaxOrder": 0}]},
             "SyncToken": LEAK, "MetaData": meta},
        ],
        "TaxRate": [
            {"Id": "3", "Name": "Example Rate", "Active": True, "RateValue": 1.23, "SpecialTaxType": "NONE",
             "DisplayType": "ReadOnly", "Description": "Example rate description",
             "AgencyRef": {"value": "1", "Undeclared": LEAK}, "EffectiveTaxRate": [{"RateValue": LEAK}],
             "TaxReturnLineRef": {"value": LEAK}, "OriginalTaxRate": LEAK, "SyncToken": LEAK, "MetaData": meta},
        ],
    }


@pytest.fixture
def server(mock_settings, registry, credentials, actor, mock_intuit: MockIntuit) -> MCPServer:
    mock_intuit.tables.update(synthetic_rows())
    return build(mock_settings, registry, credentials, actor)


def body_of(result) -> dict[str, Any]:
    return result.structured_content


def compact(row: dict[str, Any]) -> dict[str, Any]:
    """A projected row without the envelope's absent fields: the block dumps
    every declared field, null where the payload had none."""
    return {k: v for k, v in row.items() if v is not None}


def statements(mock: MockIntuit) -> list[str]:
    return [q.get("query", "") for _m, path, q, _t in mock.requests if path.endswith("/query")]


# ==========================================================================
# The surface
# ==========================================================================


def test_list_reference_data_is_registered_with_the_seven_kinds(server: MCPServer) -> None:
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert "list_reference_data" in tools
    assert set(tools["list_reference_data"].input_schema["properties"]["kind"]["enum"]) == set(KINDS)


def test_an_unknown_kind_is_refused_without_being_echoed(server, mock_intuit) -> None:
    from qbo_multicompany_mcp.tools_reference import _entity_of

    hostile = "PRIVATE-KIND <script>"
    with pytest.raises(ToolError, match="kind"):
        call(server, "list_reference_data", {"entity": "acme_main", "kind": hostile})
    with pytest.raises(QueryError) as caught:
        _entity_of(hostile)
    assert hostile not in str(caught.value)


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_walks_its_entity_with_the_shared_walker(server, mock_intuit, kind: str) -> None:
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": kind}))
    assert body["kind"] == kind and body["query_entity"] == ENTITY_OF[kind]
    assert {"count", "rows", "truncated", "duplicates_dropped", "pages_walked",
            "walk_started_at", "walk_finished_at"} <= set(body)
    assert body["consistency"] == "non_snapshot"
    assert body["count"] == len(body["rows"]) >= 1
    for key in ("entity", "environment", "display_name_from_registry", "realm_id_last4"):
        assert key in body
    sent = statements(mock_intuit)
    assert any(f"SELECT COUNT(*) FROM {ENTITY_OF[kind]}" in s for s in sent), "count-anchored"
    assert any(f"SELECT * FROM {ENTITY_OF[kind]} WHERE Active = true" in s for s in sent)


def test_active_null_includes_inactive_rows(server, mock_intuit) -> None:
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "classes", "active": None}))
    assert any("Active IN (true, false)" in s for s in statements(mock_intuit))
    assert {row["id"] for row in body["rows"]} == {"5", "6"}
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "classes", "active": False}))
    assert any("Active = false" in s for s in statements(mock_intuit))


def test_limit_trims_after_the_completeness_check(server, mock_intuit) -> None:
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "items", "limit": 1}))
    assert body["count"] == 1 and body["truncated"] is True


# ==========================================================================
# Projections: allowlist at every depth, excluded by default (the canary)
# ==========================================================================


@pytest.mark.parametrize("kind", KINDS)
def test_nothing_undeclared_reaches_a_result(server, kind: str) -> None:
    """The canary. Every undeclared field in the synthetic rows carries the
    marker, at the top level, inside references, inside lists and inside
    metadata; none may pass."""
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": kind}))
    assert LEAK not in json.dumps(body), kind


def test_the_canary_would_fire() -> None:
    """The control for the test above: the marker IS in the raw rows, at
    every depth it claims to be."""
    raw = json.dumps(synthetic_rows())
    assert raw.count(LEAK) >= 20


def test_classes_keep_names_ids_hierarchy_and_the_active_flag(server) -> None:
    """Id, Name, ParentRef, SubClass, Active and FullyQualifiedName, from
    Intuit's Class page. MetaData and SyncToken do not travel; a class is a
    documentation-derived projection (the sandbox has the feature off)."""
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "classes", "active": None}))
    rows = {r["id"]: r for r in body["rows"]}
    assert compact(rows["6"]) == {"id": "6", "name": "Example Sub", "active": False, "sub_class": True,
                                  "parent": {"id": "5"}, "fully_qualified_name": "Example Class:Example Sub"}
    assert rows["5"]["metadata"] is None and "sync_token" not in rows["5"]


def test_departments_keep_the_same_shape(server) -> None:
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "departments"}))
    assert [compact(r) for r in body["rows"]] == [
        {"id": "1", "name": "Example Dept", "active": True, "sub_department": False,
         "fully_qualified_name": "Example Dept"}]


def test_items_keep_type_the_three_account_references_and_the_disclosed_descriptions(server) -> None:
    """Type, the three account references, Sku, Taxable, UnitPrice,
    PurchaseCost, QtyOnHand, TrackQtyOnHand, InvStartDate, SubItem, ParentRef,
    Level and FullyQualifiedName, from Intuit's Item page. Description and
    PurchaseDesc are kept as free text under the free-text disclosure.
    PrefVendorRef, the tax code references, CustomExtensions and SyncToken
    do not travel."""
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "items"}))
    widget = next(r for r in body["rows"] if r["id"] == "11")
    assert widget["type"] == "Inventory" and widget["sku"] == "EX-1" and widget["taxable"] is True
    assert (widget["unit_price"], widget["purchase_cost"], widget["qty_on_hand"]) == (25.0, 10.0, 3.0)
    assert widget["track_qty_on_hand"] is True and widget["inv_start_date"] == "2026-01-01"
    assert widget["income_account"] == {"id": "79", "name": "Example Income"}
    assert widget["expense_account"] == {"id": "80", "name": "Example COGS"}
    assert widget["asset_account"] == {"id": "81", "name": "Example Asset"}
    assert widget["description"] == "Example description"
    assert widget["purchase_description"] == "Example purchase description"
    assert "pref_vendor" not in widget and "sales_tax_code" not in widget
    service = next(r for r in body["rows"] if r["id"] == "12")
    assert service["parent"] == {"id": "11"} and service["sub_item"] is True and service["level"] == 1


def test_terms_keep_days_and_discount_terms(server) -> None:
    """Intuit's Term page documents two shapes: STANDARD terms count DueDays,
    DATE_DRIVEN terms name days of the month."""
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "terms"}))
    rows = {r["id"]: compact(r) for r in body["rows"]}
    assert rows["3"] == {"id": "3", "name": "Example Net 30", "active": True, "type": "STANDARD",
                         "due_days": 30, "discount_days": 10, "discount_percent": 2.0}
    assert rows["4"] == {"id": "4", "name": "Example Date Driven", "active": True, "type": "DATE_DRIVEN",
                         "day_of_month_due": 15, "due_next_month_days": 5, "discount_day_of_month": 1}


def test_payment_methods_keep_name_type_and_active(server) -> None:
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "payment_methods"}))
    assert [compact(r) for r in body["rows"]] == [{"id": "1", "name": "Example Card", "active": True, "type": "CREDIT_CARD"}]


def test_tax_codes_keep_the_flags_and_only_the_rate_ids_from_the_rate_lists(server) -> None:
    """From SalesTaxRateList and PurchaseTaxRateList, shaped like the
    TaxRateDetail sample on Intuit's TaxCode page, only the TaxRateRef ids
    travel; TaxTypeApplicable, TaxOrder and the rate names do not."""
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "tax_codes"}))
    code = compact(body["rows"][0])
    assert code == {"id": "2", "name": "Example Tax", "active": True, "taxable": True, "tax_group": True,
                    "hidden": False, "config_type": "USER_DEFINED", "description": "Example tax code description",
                    "sales_tax_rates": [{"tax_rate": {"id": "3"}}],
                    "purchase_tax_rates": [{"tax_rate": {"id": "4"}}]}


def test_tax_rates_keep_the_value_and_the_agency_reference(server) -> None:
    """RateValue, AgencyRef, SpecialTaxType, DisplayType and Description, from
    Intuit's TaxRate page; EffectiveTaxRate, TaxReturnLineRef and
    OriginalTaxRate do not travel."""
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "tax_rates"}))
    assert [compact(r) for r in body["rows"]] == [
        {"id": "3", "name": "Example Rate", "active": True, "rate_value": 1.23,
         "special_tax_type": "NONE", "display_type": "ReadOnly",
         "description": "Example rate description", "agency": {"id": "1"}}]


# ==========================================================================
# Features that are off: an empty list that says why, never an error
# ==========================================================================


@pytest.mark.parametrize(("kind", "feature"), [("classes", "class_tracking"), ("departments", "department_tracking")])
def test_an_untracked_feature_returns_an_empty_list_that_says_so(mock_settings, registry, credentials, actor, mock_intuit, kind, feature) -> None:
    """Class and Department are empty on the sandbox and must say so, not
    fail. The company's Preferences say the feature is off; the walk still
    runs (the answer is the company's, not a guess) and the result names the
    reason an empty list is expected."""
    server = build(mock_settings, registry, credentials, actor)  # no rows seeded
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": kind}))
    assert body["count"] == 0 and body["rows"] == []
    assert body["feature"] == feature and body["feature_enabled"] is False
    assert "off" in body["feature_note"] and "empty" in body["feature_note"]


def test_a_tracked_feature_carries_no_note_and_other_kinds_have_no_feature(mock_settings, registry, credentials, actor, mock_intuit) -> None:
    from .test_report_tools import with_tracking

    with_tracking(mock_intuit)
    mock_intuit.tables.update(synthetic_rows())
    server = build(mock_settings, registry, credentials, actor)
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "classes"}))
    assert body["feature_enabled"] is True and body["feature_note"] is None
    body = body_of(call(server, "list_reference_data", {"entity": "acme_main", "kind": "terms"}))
    assert body["feature"] is None and body["feature_enabled"] is None


# ==========================================================================
# The reference entities are served, with projections, everywhere a route
# needs one; TaxCode and TaxRate are outside change data capture
# ==========================================================================


@pytest.mark.parametrize("entity", sorted(ENTITY_OF.values()))
def test_run_query_serves_each_reference_entity_through_its_projection(server, entity: str) -> None:
    body = body_of(call(server, "run_query", {"entity": "acme_main", "statement": f"SELECT * FROM {entity}"}))
    assert body["query_entity"] == entity and body["count"] >= 1
    assert LEAK not in json.dumps(body)


def _recent_since() -> str:
    """Inside list_changes' 30-day look-back on whatever day the suite runs."""
    return (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")


def test_tax_codes_and_tax_rates_are_refused_by_list_changes(server, mock_intuit) -> None:
    """Intuit's ChangeDataCapture page supports all objects except JournalCode,
    TimeActivity, TaxAgency, TaxCode and TaxRate. Refused by name before any
    request; Class goes through."""
    for entity in ("TaxCode", "TaxRate"):
        with pytest.raises(ToolError, match="change data capture"):
            call(server, "list_changes", {"entity": "acme_main", "since": _recent_since(), "types": [entity]})
    assert not mock_intuit.cdc_queries
    mock_intuit.cdc = {"CDCResponse": [{"QueryResponse": [{"Class": [{"Id": "5", "Name": "Example Class",
                                                                        "SyncToken": LEAK}]}]}]}
    body = body_of(call(server, "list_changes", {"entity": "acme_main", "since": _recent_since(),
                                                 "types": ["Class"], "full": True}))
    assert body["objects"][0]["row"]["name"] == "Example Class" and LEAK not in json.dumps(body)


def test_employee_is_still_served_nowhere(server) -> None:
    with pytest.raises(ToolError, match="Employee"):
        call(server, "run_query", {"entity": "acme_main", "statement": "SELECT * FROM Employee"})
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert "employees" not in tools["list_reference_data"].input_schema["properties"]["kind"]["enum"]


def test_list_reference_data_carries_the_free_text_disclosure() -> None:
    from qbo_multicompany_mcp.tools import FREE_TEXT_DISCLOSURE
    from qbo_multicompany_mcp.tools_reference import DESCRIPTION

    assert FREE_TEXT_DISCLOSURE in DESCRIPTION


def test_the_orderby_name_claim_cites_the_live_observation() -> None:
    """The live run of 2026-09-25: every kind's
    ``ORDERBY Name ASC`` answered 200 on both sandbox companies, and the five
    non-empty kinds came back in Name order. The two places that make the claim
    cite the observation and say what it does not show (one page each; Class
    and Department were empty)."""
    from qbo_multicompany_mcp import query_grammar, tools_reference

    for module in (tools_reference, query_grammar):
        source = " ".join(open(module.__file__, encoding="utf-8").read().replace("#", " ").split())
        assert "honours it on these seven" not in source and "is an ASSUMPTION until the live" not in source, module.__name__
        assert "OBSERVED 2026-09-25" in source, module.__name__
        assert "one page" in source, module.__name__
