"""Per-company capabilities, derived from Preferences.

MCP advertises **one** tool list for the whole server, and this server holds
several companies. So capability cannot live in a tool's schema: there is no
way to say "``list_classes`` exists for ``acme_main`` but not for
``acme_ops``". Capability therefore lives in results and in errors.

Companies on lower QuickBooks plans genuinely lack features -- Simple Start has
no class tracking, Essentials has no inventory, and multi-currency is off by
default everywhere. A tool whose required feature is known to be absent returns
a fixed, actionable error naming the feature and the entity, **before any
request is made**. It never silently returns an empty result: "no classes"
and "this plan has no classes" are different answers, and a model that receives
the first one will report that the company has no classes, which is false.

Preferences are fetched once per entity per process and cached alongside the
name guard, so the cost is one extra request on first use, not one per call.

Accounts payable is the exception. It is not reliably a Preferences flag, so
it is established by first use: a BillPayment read that Intuit refuses with the
documented feature fault marks it unavailable for that entity and is not
retried; one that succeeds marks it available; until either, it is ``unknown``
and every result that carries capabilities says so. The state is cached with
the rest of the capability block, under the same consent, so a re-consent asks
the question again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Final

from .date_macros import fiscal_start_month
from .errors import CapabilityUnavailable, capability_missing_text

#: Intuit's "Feature Not Supported" fault ("Review QuickBooks Online Accounting
#: API error codes"), the fault for a feature this company's QuickBooks plan
#: lacks. A BillPayment read refused with it is how a company without
#: accounts payable is recognised. 6190, the subscription fault, is not this:
#: it says the company's subscription has a problem, and is not read as an
#: absent feature.
ACCOUNTS_PAYABLE_FEATURE_FAULT: Final = "5030"

AP_UNKNOWN: Final = "unknown"
AP_AVAILABLE: Final = "available"
AP_UNAVAILABLE: Final = "unavailable"

#: Feature keys, and the human names used in errors.
FEATURE_NAMES: Final[dict[str, str]] = {
    "class_tracking": "class tracking",
    "department_tracking": "department (location) tracking",
    "inventory": "inventory quantity tracking",
    "multi_currency": "multi-currency",
}


@dataclass(frozen=True, slots=True)
class Capabilities:
    class_tracking: bool
    department_tracking: bool
    inventory: bool
    multi_currency: bool
    accounting_basis: str
    #: Established by first use, never by Preferences and never by the chart
    #: of accounts. ``unknown`` until a BillPayment read has succeeded or
    #: been refused with the feature fault.
    accounts_payable: str = AP_UNKNOWN
    #: The fault code that established ``unavailable``, as the reason.
    accounts_payable_fault_code: str | None = None
    #: ``FirstMonthOfFiscalYear`` as a month number, 1-12 (projected as
    #: ``fiscal_year_start_month``), or ``None`` when Preferences carried
    #: nothing readable. The fiscal date macros resolve against it
    #: (:mod:`qbo_multicompany_mcp.date_macros`) and are refused, never guessed
    #: as January, when it is unknown.
    fiscal_year_start_month: int | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> Capabilities | None:
        if not isinstance(data, Mapping):
            return None
        try:
            accounts_payable = data.get("accounts_payable", AP_UNKNOWN)
            fault = data.get("accounts_payable_fault_code")
            fiscal = data.get("fiscal_year_start_month")
            return cls(
                class_tracking=bool(data["class_tracking"]),
                department_tracking=bool(data["department_tracking"]),
                inventory=bool(data["inventory"]),
                multi_currency=bool(data["multi_currency"]),
                accounting_basis=str(data["accounting_basis"]),
                accounts_payable=(
                    accounts_payable
                    if accounts_payable in (AP_AVAILABLE, AP_UNAVAILABLE)
                    else AP_UNKNOWN
                ),
                accounts_payable_fault_code=str(fault) if isinstance(fault, (str, int)) else None,
                fiscal_year_start_month=(
                    fiscal
                    if isinstance(fiscal, int) and not isinstance(fiscal, bool) and 1 <= fiscal <= 12
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def with_accounts_payable(self, *, available: bool, fault_code: str | None = None) -> Capabilities:
        """What first use established."""
        from dataclasses import replace

        if available:
            return replace(self, accounts_payable=AP_AVAILABLE, accounts_payable_fault_code=None)
        return replace(
            self,
            accounts_payable=AP_UNAVAILABLE,
            accounts_payable_fault_code=fault_code or ACCOUNTS_PAYABLE_FEATURE_FAULT,
        )

    def has(self, feature: str) -> bool:
        if feature not in FEATURE_NAMES:
            raise KeyError(feature)
        return bool(getattr(self, feature))

    def require(self, feature: str, entity: str) -> None:
        """Raise before any request if ``feature`` is known to be off.

        Known, not guessed: this is only ever called with a capability block
        derived from that company's own Preferences.
        """
        if not self.has(feature):
            raise CapabilityUnavailable(
                capability_missing_text(entity, FEATURE_NAMES[feature])
            )


def derive_capabilities(preferences: Mapping[str, Any]) -> Capabilities:
    """Derive capabilities from *projected* preferences.

    Takes the projection's output rather than the raw payload, so a capability
    can never be derived from a field the projection deliberately removed.
    """
    accounting = preferences.get("accounting", {}) or {}
    currency = preferences.get("currency", {}) or {}
    reports = preferences.get("reports", {}) or {}
    products = preferences.get("products_and_services", {}) or {}

    return Capabilities(
        # Either granularity counts: a company tracking classes per line still
        # has classes.
        class_tracking=bool(
            accounting.get("class_tracking_per_transaction", False)
            or accounting.get("class_tracking_per_line", False)
        ),
        department_tracking=bool(accounting.get("track_departments", False)),
        # Inventory requires quantity-on-hand tracking; price-and-rate alone is
        # a non-inventory service setting.
        inventory=bool(products.get("quantity_on_hand", False)),
        multi_currency=bool(currency.get("multi_currency_enabled", False)),
        accounting_basis=str(reports.get("report_basis") or "Unknown"),
        fiscal_year_start_month=fiscal_start_month(accounting.get("fiscal_year_start_month")),
    )
