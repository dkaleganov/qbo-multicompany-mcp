"""``list_reference_data``: classes, departments, items, terms, payment
methods, tax codes and tax rates.

One kind per call, walked by the shared count-anchored, deduplicating walker
``list_accounts`` uses -- so a repeated row across a moving page boundary is
dropped and counted, a missing one fails the walk, and every result carries
the walk's window and ``consistency: non_snapshot``. Each kind is projected
through its allowlist in :mod:`qbo_multicompany_mcp.projections`
(``REFERENCE_SPECS``), finalised against live sandbox shapes observed
2026-09-24 for Item, Term, PaymentMethod, TaxCode and TaxRate, and from
Intuit's pages for Class and Department, whose live rows the sandbox company
does not have; those two results say ``documentation_derived``.

**A feature that is off is an empty list that says so, never an error.**
Class and Department are empty on the sandbox companies and must say so, not
fail. The walk still runs -- the answer is the company's, not a guess from
Preferences -- and when Preferences say the feature is off, the result names
the capability and says an empty list is expected. (Reports
take the opposite stance for a FILTER by class, which is refused when the
company cannot track them: a filter QuickBooks would ignore returns the whole
company under the filter's name, whereas an empty list is exactly true.)

``active`` follows ``list_vendors``: ``true`` by default, ``false`` for the
inactive ones, ``null`` for both through the documented
``Active IN (true, false)``. ``ORDERBY Name`` is sent on every kind.
OBSERVED 2026-09-25 on both sandbox companies: every kind's statement
answered 200, and the five non-empty kinds came back in Name order. Each
walk was one page and Class and Department were empty, so how the sort
behaves across pages is not shown; the count anchor catches a broken walk
either way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Final

from mcp.server import MCPServer
from mcp_types import CallToolResult
from pydantic import Field

from .config import Settings
from .envelopes import (
    ClassBlock,
    DepartmentBlock,
    ItemBlock,
    PaymentMethodBlock,
    ReferenceListEnvelope,
    TaxCodeBlock,
    TaxRateBlock,
    TermBlock,
)
from .projections import DOCUMENTATION_DERIVED_REFERENCE_SPECS, project_reference
from .query_grammar import QueryError, parse
from .registry import Registry
from .session import QboSession
from .tools import FREE_TEXT_DISCLOSURE, EntityKey, enum_of, make_result, read_only, tool_errors
from .tools_data import DEFAULT_LIMIT, MAX_LIMIT, _identity, _limit

#: kind -> (entity, block, the capability it depends on or None)
KINDS: Final[dict[str, tuple[str, type, str | None]]] = {
    "classes": ("Class", ClassBlock, "class_tracking"),
    "departments": ("Department", DepartmentBlock, "department_tracking"),
    "items": ("Item", ItemBlock, None),
    "terms": ("Term", TermBlock, None),
    "payment_methods": ("PaymentMethod", PaymentMethodBlock, None),
    "tax_codes": ("TaxCode", TaxCodeBlock, None),
    "tax_rates": ("TaxRate", TaxRateBlock, None),
}

#: The published enum, through json_schema_extra; :func:`_entity_of` refuses a
#: value outside it.
Kind = enum_of(tuple(KINDS))

DESCRIPTION: Final = (
    "List one company's reference data of one kind: classes, departments "
    "(locations), items (products and services), payment terms, payment "
    "methods, tax codes or tax rates, walked to the end. Use "
    "this for the ids the report filters take (class, department, item) and "
    "for what a term or tax code means; use list_accounts for accounts and "
    "list_vendors or list_customers for parties. active=null includes inactive "
    "rows. A company whose Preferences turn class or department tracking off "
    "returns an empty list that says so. Read-only." + FREE_TEXT_DISCLOSURE
)


def _entity_of(kind: str) -> tuple[str, type, str | None]:
    """The refusal behind the published enum. Fixed text; the value is never
    repeated."""
    try:
        return KINDS[kind]
    except KeyError:
        raise QueryError(
            f"kind is not one of the documented values. Accepted: {', '.join(KINDS)}."
        ) from None


def _feature_note(feature: str, enabled: bool) -> str | None:
    if enabled:
        return None
    return (
        f"The company's Preferences say {feature.replace('_', ' ')} is off, so an "
        f"empty list is the expected answer here, not a failure: the walk ran and "
        f"returned what the company holds."
    )


def register_reference_tools(
    server: MCPServer, settings: Settings, registry: Registry, session: QboSession
) -> None:
    # ---------------------------------------------- list_reference_data ---
    @server.tool(
        name="list_reference_data",
        title="List reference data of one kind",
        annotations=read_only("List reference data of one kind"),
        description=DESCRIPTION,
    )
    def list_reference_data(
        entity: EntityKey,
        kind: Annotated[Kind, Field(description="Which reference list to walk.")],
        active: Annotated[bool | None, Field(description="null includes inactive rows; true or false filters.")] = True,
        limit: Annotated[int | None, Field(ge=1, le=MAX_LIMIT, description="Rows returned; the walk still runs to the end.")] = DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, ReferenceListEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            entity_name, block, feature = _entity_of(kind)
            ready = session.ensure_ready(target)
            started = datetime.now(UTC)

            if active is None:
                clause = "Active IN (true, false)"
            else:
                clause = f"Active = {'true' if active else 'false'}"
            # Rendered through the parser, never string-concatenated: the
            # statement that runs is one the grammar accepted.
            query = parse(f"SELECT * FROM {entity_name} WHERE {clause} ORDERBY Name ASC")
            walk = session.walk(target, query, client_limit=_limit(limit))

            enabled: bool | None = None
            capabilities = ready.state.capabilities
            if feature is not None and capabilities is not None:
                enabled = capabilities.has(feature)

            return make_result(
                ReferenceListEnvelope(
                    **_identity(target, started, datetime.now(UTC)),
                    kind=kind,
                    query_entity=entity_name,
                    feature=feature,
                    feature_enabled=enabled,
                    feature_note=_feature_note(feature, enabled) if feature is not None and enabled is not None else None,
                    documentation_derived=entity_name in DOCUMENTATION_DERIVED_REFERENCE_SPECS,
                    count=len(walk.rows),
                    rows=[block(**project_reference(entity_name, row)) for row in walk.rows],
                    truncated=walk.truncated,
                    duplicates_dropped=walk.duplicates_dropped,
                    pages_walked=walk.pages_walked,
                    walk_started_at=walk.walk_started_at,
                    walk_finished_at=walk.walk_finished_at,
                )
            )
