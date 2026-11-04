"""Proves that nothing reaches a model except what a spec names.

The canary payloads below carry a marker at every depth. The marker must not
survive projection -- at any depth, in any container.
"""

from __future__ import annotations

import json

import pytest

from qbo_multicompany_mcp.capabilities import Capabilities, derive_capabilities
from qbo_multicompany_mcp.envelopes import AccountBlock, CompanyInfoBlock, CompanySettings
from qbo_multicompany_mcp.errors import CapabilityUnavailable
from qbo_multicompany_mcp.projections import (
    project_account,
    project_company_info,
    project_preferences,
    settings_from_preferences,
)

MARKER = "CANARY-SHOULD-NOT-APPEAR"


def contains_marker(value: object) -> bool:
    return MARKER in json.dumps(value)


# --------------------------------------------------------------------------
# CompanyInfo
# --------------------------------------------------------------------------


COMPANY_CANARY = {
    "Id": "1",
    "CompanyName": "Acme Main",
    "LegalName": "Acme Main LLC",
    "Country": "US",
    "EmployerId": "00-0000123",
    MARKER: MARKER,
    "CompanyAddr": {"Line1": MARKER, "PostalCode": MARKER, "Nested": {"Deep": MARKER}},
    "LegalAddr": {"Line1": MARKER},
    "CustomerCommunicationAddr": {"Line1": MARKER},
    "PrimaryPhone": {"FreeFormNumber": MARKER},
    "Email": {"Address": MARKER},
    "WebAddr": {"URI": MARKER},
    "NameValue": [{"Name": MARKER, "Value": MARKER}],
    "MetaData": {"CreateTime": "2020-01-01T00:00:00Z", MARKER: MARKER},
}


def test_company_info_projection_drops_everything_unnamed() -> None:
    projected = project_company_info(COMPANY_CANARY)
    assert not contains_marker(projected)


def test_company_info_projection_drops_addresses_and_contacts() -> None:
    projected = project_company_info(COMPANY_CANARY)
    for absent in (
        "company_addr",
        "legal_addr",
        "email",
        "web_addr",
        "primary_phone",
        "name_value",
    ):
        assert absent not in projected


def test_employer_id_leaves_only_its_last_four() -> None:
    """The employer id comes back in clear and must never leave whole."""
    projected = project_company_info(COMPANY_CANARY)
    assert projected["employer_id_last4"] == "0123"
    assert "00-0000123" not in json.dumps(projected)
    assert "0000123" not in json.dumps(projected)


def test_company_info_projection_validates_against_its_envelope() -> None:
    """extra='forbid' means a projection change without a model change fails
    here rather than shipping an unreviewed field."""
    CompanyInfoBlock(**project_company_info(COMPANY_CANARY))


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------


BANK_CANARY = {
    "Id": "1",
    "Name": "Checking",
    "AccountType": "Bank",
    "AcctNum": "000000009876",
    "CurrentBalance": 100.0,
    "CurrentBalanceWithSubAccounts": 150.0,
    MARKER: MARKER,
    "ParentRef": {"value": "9", "name": MARKER},
    "CurrencyRef": {"value": "USD", "name": MARKER},
    "MetaData": {"CreateTime": MARKER},
}


def test_account_projection_drops_everything_unnamed_at_every_depth() -> None:
    projected = project_account(BANK_CANARY)
    assert not contains_marker(projected)


@pytest.mark.parametrize("account_type", ["Bank", "Credit Card"])
def test_bank_and_credit_card_account_numbers_are_truncated(account_type: str) -> None:
    projected = project_account({**BANK_CANARY, "AccountType": account_type})
    assert projected["acct_num"] == "9876"


def test_other_account_types_keep_their_chart_code() -> None:
    """On every other account type the value passes through in full: it is a
    user-defined chart-of-accounts code (Intuit's Account page), and what a
    user typed into it is not known here."""
    projected = project_account({"Id": "2", "AccountType": "Expense", "AcctNum": "6000"})
    assert projected["acct_num"] == "6000"


def test_both_balances_are_carried_separately_and_never_summed() -> None:
    """CurrentBalanceWithSubAccounts on a parent already contains its
    children's balances; adding the two double-counts every child."""
    projected = project_account(BANK_CANARY)
    assert projected["current_balance"] == 100.0
    assert projected["current_balance_with_sub_accounts"] == 150.0
    assert 250.0 not in projected.values()


def test_hierarchy_is_preserved() -> None:
    projected = project_account(BANK_CANARY)
    assert projected["parent"] == {"id": "9"}


def test_account_projection_validates_against_its_envelope() -> None:
    AccountBlock(**project_account(BANK_CANARY))


# --------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------


PREFS_CANARY = {
    "AccountingInfoPrefs": {
        "FirstMonthOfFiscalYear": "January",
        "BookCloseDate": "2025-12-31",
        "ClassTrackingPerTxn": True,
        "TrackDepartments": True,
        MARKER: MARKER,
    },
    "CurrencyPrefs": {"MultiCurrencyEnabled": True, "HomeCurrency": {"value": "USD", MARKER: MARKER}},
    "ReportPrefs": {"ReportBasis": "Cash"},
    "ProductAndServicesPrefs": {"QuantityOnHand": True},
    "EmailMessagesPrefs": {"InvoiceMessage": {"Message": MARKER}},
    "SalesFormsPrefs": {"DefaultCustomerMessage": MARKER},
    MARKER: MARKER,
}


def test_preferences_projection_drops_customer_facing_templates() -> None:
    projected = project_preferences(PREFS_CANARY)
    assert not contains_marker(projected)


def test_settings_block_validates_against_its_envelope() -> None:
    projected = project_preferences(PREFS_CANARY)
    settings = CompanySettings(**settings_from_preferences(projected))
    assert settings.report_basis == "Cash"
    assert settings.book_close_date == "2025-12-31"
    assert settings.class_tracking_enabled is True
    assert settings.department_tracking_enabled is True
    assert settings.home_currency == "USD"


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


def test_capabilities_are_derived_from_projected_preferences_only() -> None:
    capabilities = derive_capabilities(project_preferences(PREFS_CANARY))
    assert capabilities == Capabilities(
        class_tracking=True,
        department_tracking=True,
        inventory=True,
        multi_currency=True,
        accounting_basis="Cash",
        # The fiscal year start, read from the projected month name.
        fiscal_year_start_month=1,
    )


def test_a_bare_plan_reports_every_feature_off() -> None:
    capabilities = derive_capabilities(project_preferences({"ReportPrefs": {"ReportBasis": "Accrual"}}))
    assert capabilities.class_tracking is False
    assert capabilities.department_tracking is False
    assert capabilities.inventory is False
    assert capabilities.multi_currency is False


def test_class_tracking_per_line_alone_counts_as_class_tracking() -> None:
    capabilities = derive_capabilities(
        project_preferences({"AccountingInfoPrefs": {"ClassTrackingPerTxnLine": True}})
    )
    assert capabilities.class_tracking is True


def test_a_missing_feature_raises_before_any_request() -> None:
    """Never silently downgrade to an empty result: "no classes" and "this plan
    has no classes" are different answers."""
    capabilities = derive_capabilities({})
    with pytest.raises(CapabilityUnavailable) as caught:
        capabilities.require("class_tracking", "acme_main")
    message = str(caught.value)
    assert "class tracking" in message
    assert "acme_main" in message
    assert "No request was made." in message


def test_a_present_feature_does_not_raise() -> None:
    derive_capabilities(project_preferences(PREFS_CANARY)).require("inventory", "acme_main")


def test_capabilities_round_trip_through_the_store_form() -> None:
    capabilities = derive_capabilities(project_preferences(PREFS_CANARY))
    assert Capabilities.from_json(capabilities.to_json()) == capabilities


def test_company_info_id_is_never_projected() -> None:
    """CompanyInfo.Id may be the realm id.

    Projecting that field could publish the full realm id in every
    get_company_info result.
    """
    from .conftest import REALM_MAIN

    projected = project_company_info({"Id": REALM_MAIN, "CompanyName": "Acme Main"})
    assert "id" not in projected
    assert REALM_MAIN not in json.dumps(projected)


# --------------------------------------------------------------------------
# A container at a scalar leaf
# --------------------------------------------------------------------------


def test_a_container_at_a_scalar_leaf_is_omitted_at_every_depth() -> None:
    """A Scalar rule names a leaf, and a leaf holds a scalar. When upstream
    puts an object or a list there, string coercion would publish its Python
    representation whole -- every unreviewed child field included -- under an
    allowlisted key. The value is omitted instead, at the top level, inside a
    nested reference, inside a list item, and on a derived field."""
    from qbo_multicompany_mcp.envelopes import TransactionBlock
    from qbo_multicompany_mcp.projections import project_transaction

    projected = project_transaction(
        {
            "Id": {"nested": MARKER},
            "DocNumber": [MARKER],
            "PaymentMethodRef": {"value": {"UnreviewedPrivateField": MARKER}, "name": "Check"},
            "Line": [
                {
                    "Id": ["1", MARKER],
                    "Amount": {"deep": MARKER},
                    "DetailType": "AccountBasedExpenseLineDetail",
                }
            ],
        }
    )
    assert not contains_marker(projected), projected
    assert projected["payment_method"] == {"name": "Check"}
    assert "id" not in projected and "doc_number" not in projected
    assert all("id" not in line and "amount" not in line for line in projected["lines"])
    TransactionBlock(**projected)


def test_a_container_under_a_derived_field_is_omitted() -> None:
    account = project_account(
        {"Id": "1", "Name": "Checking", "AccountType": "Bank", "AcctNum": {"digits": MARKER}}
    )
    assert not contains_marker(account), account
    assert "acct_num" not in account


def test_a_scalar_at_a_scalar_leaf_is_still_projected() -> None:
    """The control: the omission is of containers, not of values."""
    from qbo_multicompany_mcp.projections import project_transaction

    projected = project_transaction(
        {"Id": 42, "DocNumber": "00123", "PaymentMethodRef": {"value": 7, "name": "Check"}}
    )
    assert projected["id"] == "42"
    assert projected["doc_number"] == "00123"
    assert projected["payment_method"] == {"id": "7", "name": "Check"}
