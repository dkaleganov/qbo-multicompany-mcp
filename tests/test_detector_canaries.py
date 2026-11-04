"""Canaries for every rule that is proven by an absence.

A test that asserts "X does not appear" passes when the detector is broken, and
passes when the thing being detected was never there. A private-key scan that
matches its own source, a realm rule anchored on a name no fixture uses, a port
scan that finds itself: each reports zero and the zero means nothing.

So every absence-based check has a partner here that deliberately plants the
thing and proves the detector catches it.
"""

from __future__ import annotations

import json

import pytest

from qbo_multicompany_mcp.projections import (
    Derived,
    Scalar,
    project,
    project_customer,
    project_transaction,
    project_vendor,
)
from qbo_multicompany_mcp.query_grammar import ENTITY_REGISTRY, QueryError, parse
from qbo_multicompany_mcp.reports import (
    SENSITIVE_COLUMN_KEYS,
    ReportColumn,
    select_columns,
)

MARKER = "CANARY-SHOULD-NOT-APPEAR"


def contains_marker(value: object) -> bool:
    return MARKER in json.dumps(value, default=str)


# --------------------------------------------------------------------------
# The projection leak detector
# --------------------------------------------------------------------------


def test_the_marker_detector_fires_on_a_deliberately_leaky_spec() -> None:
    """If `contains_marker` were broken, every projection test would pass."""
    leaky = (Scalar("Secret", "secret"),)
    projected = project({"Secret": MARKER}, leaky)
    assert contains_marker(projected), "the leak detector must catch a real leak"


def test_the_marker_detector_does_not_fire_on_a_clean_projection() -> None:
    assert not contains_marker(project({"Secret": MARKER}, (Scalar("Id", "id"),)))


@pytest.mark.parametrize(
    ("projector", "payload"),
    [
        (project_vendor, {"Id": "1", "TaxIdentifier": MARKER, "PrimaryEmailAddr": MARKER,
                          "BillAddr": {"Line1": MARKER}, "Notes": MARKER, "AcctNum": MARKER}),
        (project_customer, {"Id": "1", "PrimaryEmailAddr": MARKER, "Notes": MARKER,
                            "BillAddr": {"Line1": MARKER}, "ResaleNum": MARKER,
                            "PrimaryTaxIdentifier": MARKER}),
        (project_transaction, {"Id": "1", "CreditChargeInfo": {"NameOnAcct": MARKER},
                               "CustomerMemo": {"value": MARKER}, "TxnSource": MARKER,
                               "SyncToken": MARKER, "BillAddr": {"Line1": MARKER},
                               "domain": MARKER, "sparse": MARKER}),
    ],
)
def test_real_projections_drop_every_planted_marker(projector, payload) -> None:
    assert not contains_marker(projector(payload))


def test_metadata_is_excluded_unless_asked_for() -> None:
    payload = {"Id": "1", "MetaData": {"LastUpdatedTime": "2026-09-01"}}
    assert "metadata" not in project_vendor(payload)
    assert "metadata" in project_vendor(payload, include_metadata=True)


def test_the_emit_none_canary() -> None:
    """`tax_id_on_file` must be able to be an explicit null.

    Without `emit_none`, a None result drops the key, and 'no opinion offered'
    is a weaker statement than 'this could not be determined'.
    """
    rule = Derived("X", "x", lambda p: None, emit_none=True)
    out: dict = {}
    rule.apply({}, out)
    assert out == {"x": None}

    silent = Derived("X", "x", lambda p: None)
    out2: dict = {}
    silent.apply({}, out2)
    assert out2 == {}


# --------------------------------------------------------------------------
# The parser's reject tables
# --------------------------------------------------------------------------


def test_the_field_rejection_detector_fires() -> None:
    """Prove the reject path is reachable, so the reject tests mean something."""
    with pytest.raises(QueryError, match="not a filterable field"):
        parse("SELECT * FROM Vendor WHERE DefinitelyNotAField = 'x'")


def test_the_accept_path_is_also_reachable() -> None:
    """The mirror: if everything were rejected, the reject tests would still
    pass while the parser was useless."""
    parse("SELECT * FROM Vendor WHERE DisplayName = 'x'")


def test_the_pii_filter_audit_would_catch_a_regression() -> None:
    """The audit over ENTITY_REGISTRY passes today. Prove it fails when a PII
    field is actually present, rather than because the loop is empty."""
    forbidden = {"PrimaryEmailAddr", "TaxIdentifier"}
    pretend_filterable = frozenset({"Id", "DisplayName", "PrimaryEmailAddr"})
    assert forbidden & pretend_filterable, "the audit expression must detect this"
    assert not any(forbidden & spec.filterable for spec in ENTITY_REGISTRY.values())


def test_the_entity_registry_is_not_empty() -> None:
    """A per-entity table test over an empty registry proves nothing."""
    assert len(ENTITY_REGISTRY) >= 16
    assert all(spec.filterable for spec in ENTITY_REGISTRY.values())
    assert all(spec.sortable for spec in ENTITY_REGISTRY.values())


# --------------------------------------------------------------------------
# The report column gate
# --------------------------------------------------------------------------


def column(key: str) -> ReportColumn:
    # ``leaf_key`` is the ColKey as declared and is what the allowlist matches;
    # a column built without one is a keyless column, which the gate excludes
    # by design (the TransactionList rule), so it is given here.
    return ReportColumn(index=0, key=key, title="T", type="String", leaf_key=key)


def test_the_sensitive_column_gate_fires() -> None:
    kept, excluded = select_columns([column("email")], "TransactionList")
    assert kept == [] and excluded == ["email"]


def test_the_unknown_column_gate_fires() -> None:
    kept, excluded = select_columns([column("never_seen_before")], "TransactionList")
    assert kept == [] and excluded == ["never_seen_before"]


def test_the_column_gate_lets_an_allowlisted_key_through() -> None:
    """Otherwise the two tests above would pass with everything blocked."""
    kept, excluded = select_columns([column("tx_date")], "TransactionList")
    assert [c.key for c in kept] == ["tx_date"] and excluded == []


def test_the_sensitive_key_set_is_not_empty() -> None:
    assert len(SENSITIVE_COLUMN_KEYS) >= 10


# --------------------------------------------------------------------------
# The "no threshold" canary
# --------------------------------------------------------------------------


def test_the_banned_field_canary_fires() -> None:
    """The real test asserts banned names are absent from the envelope. Prove
    the comparison works, rather than passing over an empty name set."""
    banned = {"threshold", "upper_bound", "requires_1099"}
    pretend_fields = {"vendor_id", "threshold"}
    assert banned & pretend_fields, "the check must detect a planted field"

    from qbo_multicompany_mcp.envelopes import VendorPaymentsEnvelope

    assert not (banned & set(VendorPaymentsEnvelope.model_fields))
    assert len(VendorPaymentsEnvelope.model_fields) > 10


# --------------------------------------------------------------------------
# Preservation fixtures, one per served transaction type
# --------------------------------------------------------------------------
#
# The privacy canaries above prove that what must go, goes. These prove the
# other half: that what must stay, stays. Routing and structural ids survive
# projection. Without FromAccountRef, ToAccountRef and
# CheckPayment.BankAccountRef in the common transaction spec, a Transfer keeps
# its own id and loses both of its accounts, and a BillPayment loses the
# account that paid it. Every reference below is one Intuit's entity page for
# that type documents, except VendorCredit, whose list follows Bill's.
#
# Each fixture also plants brand-new fields -- at the top level, inside a
# reference, inside a line detail -- because a field Intuit starts sending
# tomorrow must be excluded by default.

from qbo_multicompany_mcp.envelopes import TransactionBlock  # noqa: E402
from qbo_multicompany_mcp.query_grammar import TRANSACTION_TYPES  # noqa: E402

#: Where each reference's id lands after projection.
REF_TARGETS: dict[str, tuple[str, ...]] = {
    "VendorRef": ("vendor", "id"),
    "CustomerRef": ("customer", "id"),
    "EntityRef": ("entity_ref", "id"),
    "AccountRef": ("account", "id"),
    "APAccountRef": ("ap_account", "id"),
    "DepositToAccountRef": ("deposit_to_account", "id"),
    "FromAccountRef": ("from_account", "id"),
    "ToAccountRef": ("to_account", "id"),
    "CheckPayment.BankAccountRef": ("check_payment", "bank_account", "id"),
    "CreditCardPayment.CCAccountRef": ("credit_card_payment", "cc_account", "id"),
    "PaymentMethodRef": ("payment_method", "id"),
    "DepartmentRef": ("department", "id"),
    "ClassRef": ("class_ref", "id"),
    "SalesTermRef": ("terms", "id"),
    "CurrencyRef": ("currency", "value"),
    "ProjectRef": ("project", "id"),
    "RecurDataRef": ("recurring_template", "id"),
    "TxnTaxDetail.TxnTaxCodeRef": ("tax_detail", "txn_tax_code", "id"),
}

#: Line-detail references, as (detail, reference) -> projected path.
LINE_REF_TARGETS: dict[tuple[str, str], tuple[str, ...]] = {
    ("AccountBasedExpenseLineDetail", "AccountRef"): ("expense_detail", "account", "id"),
    ("AccountBasedExpenseLineDetail", "ClassRef"): ("expense_detail", "class_ref", "id"),
    ("AccountBasedExpenseLineDetail", "CustomerRef"): ("expense_detail", "customer", "id"),
    ("AccountBasedExpenseLineDetail", "TaxCodeRef"): ("expense_detail", "tax_code", "id"),
    ("ItemBasedExpenseLineDetail", "ItemRef"): ("item_detail", "item", "id"),
    ("ItemBasedExpenseLineDetail", "ClassRef"): ("item_detail", "class_ref", "id"),
    ("ItemBasedExpenseLineDetail", "CustomerRef"): ("item_detail", "customer", "id"),
    ("ItemBasedExpenseLineDetail", "TaxCodeRef"): ("item_detail", "tax_code", "id"),
    ("SalesItemLineDetail", "ItemRef"): ("sales_detail", "item", "id"),
    ("SalesItemLineDetail", "ClassRef"): ("sales_detail", "class_ref", "id"),
    ("SalesItemLineDetail", "TaxCodeRef"): ("sales_detail", "tax_code", "id"),
    ("DepositLineDetail", "AccountRef"): ("deposit_detail", "account", "id"),
    ("DepositLineDetail", "Entity"): ("deposit_detail", "entity", "id"),
    ("DepositLineDetail", "ClassRef"): ("deposit_detail", "class_ref", "id"),
    ("DepositLineDetail", "PaymentMethodRef"): ("deposit_detail", "payment_method", "id"),
    ("JournalEntryLineDetail", "AccountRef"): ("journal_detail", "account", "id"),
    ("JournalEntryLineDetail", "ClassRef"): ("journal_detail", "class_ref", "id"),
    ("JournalEntryLineDetail", "DepartmentRef"): ("journal_detail", "department", "id"),
    ("JournalEntryLineDetail", "TaxCodeRef"): ("journal_detail", "tax_code", "id"),
    ("DiscountLineDetail", "DiscountAccountRef"): ("discount_detail", "discount_account", "id"),
}

_EXPENSE_LINES = ("AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail")
_SALES_LINES = ("SalesItemLineDetail",)

#: Per served type: its header references, and the line details it carries.
# TxnTaxDetail is on every served type except BillPayment, Payment and
# Transfer (Intuit's pages for them do not document it).
_TAX = "TxnTaxDetail.TxnTaxCodeRef"

PRESERVED: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Bill": (("VendorRef", "APAccountRef", "DepartmentRef", "SalesTermRef", "CurrencyRef",
              "ProjectRef", "RecurDataRef", _TAX), _EXPENSE_LINES),
    "BillPayment": (("VendorRef", "APAccountRef", "CheckPayment.BankAccountRef",
                     "CreditCardPayment.CCAccountRef", "DepartmentRef", "CurrencyRef"), ()),
    "CreditMemo": (("CustomerRef", "ClassRef", "DepartmentRef", "SalesTermRef",
                    "PaymentMethodRef", "CurrencyRef", "ProjectRef", "RecurDataRef", _TAX),
                   _SALES_LINES),
    "Deposit": (("DepositToAccountRef", "DepartmentRef", "CurrencyRef", "ProjectRef",
                 "RecurDataRef", _TAX), ("DepositLineDetail",)),
    "Invoice": (("CustomerRef", "DepositToAccountRef", "ClassRef", "DepartmentRef",
                 "SalesTermRef", "CurrencyRef", "ProjectRef", "RecurDataRef", _TAX),
                (*_SALES_LINES, "DiscountLineDetail")),
    "JournalEntry": (("CurrencyRef", "ProjectRef", "RecurDataRef", _TAX),
                     ("JournalEntryLineDetail",)),
    "Payment": (("CustomerRef", "DepositToAccountRef", "PaymentMethodRef", "CurrencyRef",
                 "ProjectRef"), ()),
    "Purchase": (("AccountRef", "EntityRef", "PaymentMethodRef", "DepartmentRef",
                  "CurrencyRef", "ProjectRef", "RecurDataRef", _TAX), _EXPENSE_LINES),
    "PurchaseOrder": (("VendorRef", "APAccountRef", "ClassRef", "SalesTermRef", "CurrencyRef",
                       "ProjectRef", "RecurDataRef", _TAX), _EXPENSE_LINES),
    "RefundReceipt": (("CustomerRef", "DepositToAccountRef", "PaymentMethodRef", "ClassRef",
                       "DepartmentRef", "CurrencyRef", "ProjectRef", "RecurDataRef", _TAX),
                      _SALES_LINES),
    "SalesReceipt": (("CustomerRef", "DepositToAccountRef", "PaymentMethodRef", "ClassRef",
                      "DepartmentRef", "CurrencyRef", "ProjectRef", "RecurDataRef", _TAX),
                     _SALES_LINES),
    "Transfer": (("FromAccountRef", "ToAccountRef", "RecurDataRef"), ()),
    "VendorCredit": (("VendorRef", "APAccountRef", "DepartmentRef", "CurrencyRef", _TAX),
                     _EXPENSE_LINES),
}


def _ref(value: str) -> dict:
    # Every reference carries a child nobody has reviewed.
    return {"value": value, "name": f"name of {value}", "BrandNewChild2031": MARKER}


def _set_path(target: dict, dotted: str, value: object) -> None:
    *parents, leaf = dotted.split(".")
    for part in parents:
        target = target.setdefault(part, {"BrandNewSibling2031": MARKER})
    target[leaf] = value


def _get_path(source: object, path: tuple[str, ...]) -> object:
    for part in path:
        if not isinstance(source, dict) or part not in source:
            return None
        source = source[part]
    return source


def _fixture(entity: str) -> tuple[dict, dict[tuple[str, ...], str]]:
    header_refs, line_details = PRESERVED[entity]
    payload: dict = {"Id": f"{entity}-1", "BrandNewTopLevel2031": MARKER}
    expected: dict[tuple[str, ...], str] = {("id",): f"{entity}-1"}
    for ref in header_refs:
        value = f"{entity}-{ref}"
        _set_path(payload, ref, _ref(value))
        expected[REF_TARGETS[ref]] = value
    lines = []
    for index, detail in enumerate(line_details):
        body: dict = {"BrandNewLineChild2031": MARKER}
        for (owner, ref), path in LINE_REF_TARGETS.items():
            if owner == detail:
                value = f"{entity}-{detail}-{ref}"
                body[ref] = _ref(value)
                expected[("lines", str(index), *path)] = value
        lines.append({"Id": str(index), "DetailType": detail, detail: body,
                      "BrandNewLineField2031": MARKER})
    if lines:
        payload["Line"] = lines
    return payload, expected


def test_every_served_transaction_type_has_a_preservation_fixture() -> None:
    """A newly served type without one would be preserved by nobody's test."""
    assert set(PRESERVED) == set(TRANSACTION_TYPES)


@pytest.mark.parametrize("entity", sorted(PRESERVED))
def test_structural_references_survive_projection(entity: str) -> None:
    payload, expected = _fixture(entity)
    projected = project_transaction(payload)
    for path, value in expected.items():
        resolved = []
        for part in path:
            resolved.append(int(part) if part.isdigit() else part)
        node: object = projected
        for part in resolved:
            node = node[part] if isinstance(node, (dict, list)) and (
                isinstance(part, int) or part in node) else None
            if node is None:
                break
        assert node == value, f"{entity}: {'.'.join(path)} was dropped"


@pytest.mark.parametrize("entity", sorted(PRESERVED))
def test_a_field_intuit_starts_sending_is_excluded_by_default(entity: str) -> None:
    payload, _ = _fixture(entity)
    projected = project_transaction(payload)
    assert not contains_marker(projected)
    # And what survives is what the envelope declares: nothing unreviewed.
    TransactionBlock(**projected)


def test_tax_references_survive_projection() -> None:
    """The three tax references are projected: they are how a reader tells
    whether a line was taxed. Each is documented on Intuit's entity pages:
    line-level ``TaxCodeRef`` on every line-detail type served, and
    ``TxnTaxDetail`` with ``TxnTaxCodeRef`` and
    ``TaxLine[].TaxLineDetail.TaxRateRef`` on the header (Purchase, Invoice and
    Bill). A field nobody reviewed, planted at every depth, still goes."""
    payload = {
        "Id": "P-1",
        "TxnTaxDetail": {
            "TxnTaxCodeRef": _ref("TC-1"),
            "TotalTax": 26.82,
            "BrandNewTaxField2031": MARKER,
            "TaxLine": [
                {
                    "Amount": 26.82,
                    "DetailType": "TaxLineDetail",
                    "BrandNewTaxLineField2031": MARKER,
                    "TaxLineDetail": {
                        "TaxRateRef": _ref("TR-3"),
                        "PercentBased": True,
                        "TaxPercent": 8,
                        "NetAmountTaxable": 335.25,
                        "BrandNewTaxLineDetail2031": MARKER,
                    },
                }
            ],
        },
        "Line": [
            {"Id": "1", "DetailType": "AccountBasedExpenseLineDetail",
             "AccountBasedExpenseLineDetail": {"TaxCodeRef": _ref("TAX")}},
            {"Id": "2", "DetailType": "ItemBasedExpenseLineDetail",
             "ItemBasedExpenseLineDetail": {"TaxCodeRef": _ref("NON")}},
            {"Id": "3", "DetailType": "SalesItemLineDetail",
             "SalesItemLineDetail": {"TaxCodeRef": _ref("TAX")}},
            {"Id": "4", "DetailType": "JournalEntryLineDetail",
             "JournalEntryLineDetail": {"TaxCodeRef": _ref("TAX"), "TaxApplicableOn": "Sales"}},
        ],
    }
    projected = project_transaction(payload)
    tax = projected["tax_detail"]
    assert tax["txn_tax_code"] == {"id": "TC-1", "name": "name of TC-1"}
    assert tax["total_tax"] == 26.82
    line = tax["tax_lines"][0]
    assert line["amount"] == 26.82
    assert line["tax_line_detail"]["tax_rate"] == {"id": "TR-3", "name": "name of TR-3"}
    assert line["tax_line_detail"]["percent_based"] is True
    assert line["tax_line_detail"]["tax_percent"] == 8
    assert line["tax_line_detail"]["net_amount_taxable"] == 335.25
    lines = projected["lines"]
    assert lines[0]["expense_detail"]["tax_code"]["id"] == "TAX"
    assert lines[1]["item_detail"]["tax_code"]["id"] == "NON"
    assert lines[2]["sales_detail"]["tax_code"]["id"] == "TAX"
    assert lines[3]["journal_detail"]["tax_code"]["id"] == "TAX"
    assert not contains_marker(projected)
    TransactionBlock(**projected)


def test_the_other_three_references_are_deliberately_not_projected() -> None:
    """TaxExemptionRef carries a state-issued exemption id in its name (Intuit's
    Invoice page); ShipMethodRef and ItemAccountRef are not needed to read a
    transaction. Their absence is deliberate, stated in projections.py so the
    next reader does not take it for an oversight."""
    import inspect

    from qbo_multicompany_mcp import projections

    projected = project_transaction({
        "Id": "I-1",
        "ShipMethodRef": _ref("SM-1"),
        "TaxExemptionRef": {"value": "9", "name": "STATE-EXEMPTION-ID-SHOULD-NOT-APPEAR"},
        "Line": [{"Id": "1", "DetailType": "SalesItemLineDetail",
                  "SalesItemLineDetail": {"ItemAccountRef": _ref("IA-1")}}],
    })
    flat = repr(projected)
    assert "SM-1" not in flat and "IA-1" not in flat and "STATE-EXEMPTION" not in flat
    source = inspect.getsource(projections)
    for name in ("TaxExemptionRef", "ShipMethodRef", "ItemAccountRef"):
        assert name in source, f"{name}'s exclusion is not stated in projections.py"


def test_a_transfer_keeps_its_amount() -> None:
    """A Transfer has no TotalAmt; its figure is `Amount`. Without it a
    Transfer projected to two accounts and no money."""
    projected = project_transaction({"Id": "9", "Amount": 250.0,
                                     "FromAccountRef": _ref("1"), "ToAccountRef": _ref("2")})
    assert projected["amount"] == 250.0


def test_payment_method_is_a_reference_not_a_stringified_dict() -> None:
    """`Scalar(..., as_str)` would put the Python repr of the whole dict --
    unknown children included -- into `payment_method_id`."""
    projected = project_transaction({"Id": "1", "PaymentMethodRef": _ref("3")})
    assert projected["payment_method"] == {"id": "3", "name": "name of 3"}
    assert "payment_method_id" not in projected
    assert not contains_marker(projected)
