"""The published ``tools/list`` meets the schema rules on every tool.

Every parameter has a ``Field(description=...)``, ``format: date`` is on every
``YYYY-MM-DD`` argument, and every tool carries the four read-only annotations.
The check runs over what the real ``MCPServer.list_tools`` publishes -- the
schema a client reads -- not over the Python functions, because the SDK builds
the schema and a rule met in the source can still be lost on the way out.
Documents are enabled so all nineteen tools are listed.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import anyio
from mcp.server import MCPServer

from qbo_multicompany_mcp.tools import register_tools

#: 18 default tools and the opt-in get_attachment.
PUBLISHED_TOOL_COUNT = 19

#: A ``YYYY-MM-DD`` argument by name: ``start_date``, ``end_date``,
#: ``report_date``, ``start_duedate``, ``end_duedate``, ``as_of``.
#: ``since`` and ``updated_since`` are timestamps, not dates.
DATE_NAME = re.compile(r"(^|_)(date|duedate)$|^as_of$")

#: The four read-only annotations on every tool, in the wire names.
READ_ONLY_HINTS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

#: The two tools that make no request say so with ``openWorldHint: false``;
#: every other tool may reach QuickBooks.
REQUEST_FREE_TOOLS = frozenset({"list_entities", "server_info"})


def expected_hints(tool_name: str) -> dict[str, bool]:
    return {**READ_ONLY_HINTS, "openWorldHint": tool_name not in REQUEST_FREE_TOOLS}

#: Titles stay short and readable. The longest published one is 47 characters.
MAX_TITLE_LENGTH = 50

#: The report tools, and the parameters that are id filters on them: Intuit's
#: pages document each as "One or more comma separated ... IDs", so each takes
#: a list of ids. Not here, by their own contracts: ``payment_method`` takes a
#: payment method's NAME, and ``agency_id`` is one required id.
REPORT_TOOLS = frozenset(
    {"report_profit_and_loss", "report_balance_sheet", "report_general_ledger", "report_aging",
     "run_report", "list_transactions"}
)
REPORT_ID_FILTERS = frozenset({"customer", "vendor", "class", "department", "item", "employee", "account"})


def published_tools(mock_settings, registry) -> list[Any]:
    server = MCPServer(name="t", version="0")
    register_tools(server, replace(mock_settings, allow_documents=True), registry)
    return anyio.run(server.list_tools)


def _branches(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """The schema itself, or each branch of an optional value's ``anyOf``."""
    return schema.get("anyOf") or [schema]


def _is_date_argument(name: str, schema: dict[str, Any]) -> bool:
    return bool(DATE_NAME.search(name)) or "YYYY-MM-DD" in (schema.get("description") or "")


def _has_date_format(schema: dict[str, Any]) -> bool:
    strings = [b for b in _branches(schema) if b.get("type") == "string"]
    return bool(strings) and all(b.get("format") == "date" for b in strings)


def _is_id_list(schema: dict[str, Any]) -> bool:
    """An array of strings, each validated as an id by a pattern, at least one."""
    arrays = [b for b in _branches(schema) if b.get("type") == "array"]
    return bool(arrays) and all(
        b.get("items", {}).get("type") == "string" and b.get("items", {}).get("pattern") and b.get("minItems", 0) >= 1
        for b in arrays
    ) and not any(b.get("type") == "string" for b in _branches(schema))


def schema_problems(tools: list[Any]) -> list[str]:
    """Every breach of the rules, named by tool and parameter."""
    problems: list[str] = []
    for tool in tools:
        wire = tool.model_dump(by_alias=True, exclude_none=True)
        for name, schema in wire["inputSchema"].get("properties", {}).items():
            if not (schema.get("description") or "").strip():
                problems.append(f"{tool.name}.{name}: no description")
            if _is_date_argument(name, schema) and not _has_date_format(schema):
                problems.append(f"{tool.name}.{name}: no format: date")
            if tool.name in REPORT_TOOLS and name in REPORT_ID_FILTERS and not _is_id_list(schema):
                problems.append(f"{tool.name}.{name}: a report id filter that is not a list of ids")

        annotations = wire.get("annotations") or {}
        for hint, expected in expected_hints(tool.name).items():
            if annotations.get(hint) is not expected:
                problems.append(f"{tool.name}: {hint} is {annotations.get(hint)!r}, not {expected!r}")
        title = annotations.get("title") or ""
        if not title or len(title) > MAX_TITLE_LENGTH:
            problems.append(f"{tool.name}: annotation title {title!r} is not a short human title")
        elif title != tool.title:
            problems.append(f"{tool.name}: annotation title differs from the tool title")
    return problems


def test_every_published_tool_meets_the_schema_rules(mock_settings, registry) -> None:
    """One test over the whole published list: a missing description, date
    format or annotation anywhere fails it, and the failure names each one."""
    tools = published_tools(mock_settings, registry)
    assert len(tools) == PUBLISHED_TOOL_COUNT

    problems = schema_problems(tools)
    assert problems == [], "\n".join(problems)


def published_enums(tools: list[Any]) -> dict[str, list[str]]:
    """``tool.parameter`` -> the enum its published schema carries, from the
    parameter itself or the string branch of an optional one's ``anyOf``."""
    found: dict[str, list[str]] = {}
    for tool in tools:
        wire = tool.model_dump(by_alias=True, exclude_none=True)
        for name, schema in wire["inputSchema"].get("properties", {}).items():
            for branch in _branches(schema):
                if "enum" in branch:
                    assert branch.get("type") == "string", (tool.name, name, branch)
                    found[f"{tool.name}.{name}"] = branch["enum"]
    return found


def test_every_published_enum_stays_in_the_schema(mock_settings, registry) -> None:
    """The enums go through ``json_schema_extra`` rather than ``Literal``,
    because the SDK refuses a Literal with text that quotes the caller's value.
    A client must not lose one published value, or its order, on the way:
    tests/fixtures/published_enums.json is a snapshot of every enum
    ``tools/list`` publishes."""
    import json
    from pathlib import Path

    snapshot = json.loads(
        (Path(__file__).parent / "fixtures" / "published_enums.json").read_text(encoding="utf-8")
    )
    assert published_enums(published_tools(mock_settings, registry)) == snapshot


def test_the_schema_check_sees_each_kind_of_breach(mock_settings, registry) -> None:
    """The check itself, against planted breaches, so a check that stopped
    looking would fail here rather than pass above."""
    tool = next(t for t in published_tools(mock_settings, registry) if t.name == "list_transactions")
    wire = tool.model_dump(by_alias=True, exclude_none=True)

    class Planted:
        name = "planted"
        title = tool.title

        def __init__(self, input_schema: dict[str, Any], annotations: dict[str, Any] | None) -> None:
            self._wire = {"inputSchema": input_schema, "annotations": annotations}

        def model_dump(self, **_: Any) -> dict[str, Any]:
            return self._wire

    good = {"properties": {"start_date": {"type": "string", "format": "date", "description": "d"}}}
    hints = {**READ_ONLY_HINTS, "title": tool.title}
    assert schema_problems([Planted(good, hints)]) == []

    no_description = {"properties": {"memo": {"type": "string"}}}
    no_format = {"properties": {"end_date": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "d"}}}
    assert schema_problems([Planted(no_description, hints)]) == ["planted.memo: no description"]
    assert schema_problems([Planted(no_format, hints)]) == ["planted.end_date: no format: date"]
    assert any("readOnlyHint" in p for p in schema_problems([Planted(good, None)]))
    assert any("title" in p for p in schema_problems([Planted(good, {**hints, "title": ""})]))
    assert wire["inputSchema"]["properties"], "the planted shapes mirror a real published schema"

    class PlantedReport(Planted):
        name = "run_report"

    string_ids = {"properties": {"customer": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "d"}}}
    unvalidated = {"properties": {"customer": {"anyOf": [
        {"type": "array", "items": {"type": "string"}}, {"type": "null"}], "description": "d"}}}
    id_list = {"properties": {"customer": {"anyOf": [
        {"type": "array", "items": {"type": "string", "pattern": "^[0-9]+$"}, "minItems": 1}, {"type": "null"}],
        "description": "d"}}}
    assert schema_problems([PlantedReport(string_ids, hints)]) == [
        "run_report.customer: a report id filter that is not a list of ids"
    ]
    assert schema_problems([PlantedReport(unvalidated, hints)]) == [
        "run_report.customer: a report id filter that is not a list of ids"
    ]
    assert schema_problems([PlantedReport(id_list, hints)]) == []

    # Both ways: a request-free tool claiming it may reach outside, and an
    # ordinary tool claiming it cannot, are each a breach.
    class PlantedLocal(Planted):
        name = "server_info"

    assert schema_problems([PlantedLocal(good, hints)]) == ["server_info: openWorldHint is True, not False"]
    assert schema_problems([PlantedLocal(good, {**hints, "openWorldHint": False})]) == []
    assert schema_problems([Planted(good, {**hints, "openWorldHint": False})]) == [
        "planted: openWorldHint is False, not True"
    ]


def test_the_two_report_filters_that_are_not_id_lists_keep_their_own_contracts(mock_settings, registry) -> None:
    """Checked per contract rather than by name: ``agency_id`` is TaxSummary's
    one required id, and ``payment_method`` takes a payment method's name --
    Cash, Check, Visa and so on, as Intuit's pages list them -- so neither is
    a list, and payment_method does not claim to take an id."""
    run_report = next(t for t in published_tools(mock_settings, registry) if t.name == "run_report")
    props = run_report.model_dump(by_alias=True, exclude_none=True)["inputSchema"]["properties"]
    for name in ("agency_id", "payment_method"):
        assert {b.get("type") for b in _branches(props[name])} == {"string", "null"}, name
    assert " id" not in props["payment_method"]["description"]


def test_only_the_two_request_free_tools_say_they_reach_nothing(mock_settings, registry) -> None:
    """``openWorldHint`` is false on exactly ``list_entities`` and
    ``server_info``, which make no request, and true on the other seventeen,
    which may reach QuickBooks."""
    tools = published_tools(mock_settings, registry)
    closed = {tool.name for tool in tools if tool.annotations.open_world_hint is False}
    assert closed == REQUEST_FREE_TOOLS
    assert all(tool.annotations.open_world_hint is True for tool in tools if tool.name not in closed)
