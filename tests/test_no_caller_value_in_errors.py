"""No refusal repeats a value the caller composed.

It is a contract that no error text repeats a caller's value (docs/design.md,
"No error repeats a caller's value"; docs/tools.md: a refusal "names the
parameter, never the caller's value"). The six-month refusal is held to it
too, in ``list_transactions`` and in the report tools' shared window helper. A
date's syntax keeps arbitrary text out, but the dates are still the caller's,
and the contract has no exception for them.

The rule every refusal is held to here: it may name a parameter, a list
position, a limit, and a member of this package's own closed vocabulary in
the package's spelling -- an enum value, a canonical entity or field name, a
configured entity key. It never repeats a value the caller composed: text,
ids, dates, numbers, list elements.

Every path to those two refusals is tested. Then the sweep:
every parameter of every published tool is sent a composed value -- a marker
string, dates no package text contains, a number no limit equals, lists
carrying the marker -- through the same call boundary a client reaches, and
no refusal may contain any of them. Its detector is proven on a planted echo.
"""

from __future__ import annotations

import re
from dataclasses import replace as dc_replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from qbo_multicompany_mcp.query_grammar import QueryError
from qbo_multicompany_mcp.server import QboMCPServer, build_server
from qbo_multicompany_mcp.tools import tool_errors
from qbo_multicompany_mcp.tools_reports import _check_window

from .conftest import REALM_MAIN, make_token_response
from .test_attachments import FILES_HOST

#: Values a caller composes, none of which any text in this package contains.
MARKER = "Zq9SweepMarker7"
EARLY, LATE = "2031-01-17", "2031-11-23"  # ten months apart
NUMBER = 987654
OLD_STAMP = "2001-02-03T04:05:06Z"
COMPOSED = (MARKER, EARLY, LATE, str(NUMBER), "2001-02-03")

_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _server(mock_settings: Any, registry: Any, credentials: Any, actor: Any) -> QboMCPServer:
    """The server a client reaches -- QboMCPServer, whose call boundary
    rebuilds the SDK's argument errors -- with documents on, so
    get_attachment is on the surface too, and a stored credential."""
    settings = dc_replace(mock_settings, allow_documents=True, attachment_hosts=(FILES_HOST,))
    settings.open_store(credentials=credentials).commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor,
    )
    server = build_server(settings, registry)
    assert isinstance(server, QboMCPServer)
    return server


def _refusal(server: QboMCPServer, tool: str, arguments: dict[str, Any]) -> str | None:
    """The refusal text a client reads, or None if the call answered."""
    try:
        anyio.run(lambda: server.call_tool(tool, arguments))
    except ToolError as refused:
        return str(refused)
    return None


# --------------------------------------------------------------------------
# 1. The six-month refusal names the limit, not the dates
# --------------------------------------------------------------------------

#: Every path to the six-month refusal, each with a window ten months wide.
WINDOW_PATHS = {
    "list_transactions": ("list_transactions", {"start_date": EARLY, "end_date": LATE}),
    "general_ledger": ("report_general_ledger", {"start_date": EARLY, "end_date": LATE}),
    "aging_detail_due_dates": (
        "report_aging",
        {"side": "receivables", "detail": "detail", "start_duedate": EARLY, "end_duedate": LATE},
    ),
    "run_report_detail": ("run_report", {"report": "ProfitAndLossDetail", "start_date": EARLY, "end_date": LATE}),
    "run_report_due_dates": (
        "run_report", {"report": "VendorBalanceDetail", "start_duedate": EARLY, "end_duedate": LATE}
    ),
}


@pytest.mark.parametrize("path", sorted(WINDOW_PATHS))
def test_the_six_month_refusal_names_the_limit_and_no_date(
    path, mock_settings, registry, credentials, actor
) -> None:
    tool, window = WINDOW_PATHS[path]
    server = _server(mock_settings, registry, credentials, actor)
    text = _refusal(server, tool, {"entity": "acme_main", **window})
    assert text is not None, f"{path}: a ten-month window was not refused"
    assert "calendar months" in text, text
    assert EARLY not in text and LATE not in text, f"{path} repeated the caller's dates: {text}"
    assert not _ISO_DATE.search(text), f"{path} names a date: {text}"


def test_a_resolved_macro_window_is_refused_without_its_dates_too(
    mock_settings, registry, credentials, actor
) -> None:
    """A macro the server resolved to more than six months is refused like
    explicit dates. Those dates are the server's, not the caller's, and the
    refusal names none of them either: one rule, one text."""
    server = _server(mock_settings, registry, credentials, actor)
    text = _refusal(server, "run_report", {
        "entity": "acme_main", "report": "ProfitAndLossDetail", "date_macro": "Last Fiscal Year",
    })
    assert text is not None and "calendar months" in text, text
    assert not _ISO_DATE.search(text), text


def test_the_window_helper_names_its_parameters_and_the_limit_never_a_date() -> None:
    with pytest.raises(QueryError) as refused:
        _check_window(date(2031, 1, 17), date(2031, 11, 23), start_name="start_duedate",
                      end_name="end_duedate", months=6)
    text = str(refused.value)
    assert "start_duedate" in text and "end_duedate" in text and "6 calendar months" in text, text
    assert not _ISO_DATE.search(text), text


# --------------------------------------------------------------------------
# 2. The sweep: every parameter of every tool, sent a composed value
# --------------------------------------------------------------------------

#: Inside list_changes' 30-day look-back on whatever day the suite runs. A fixed
#: date ages past it, and the look-back refusal then comes first, so the rest of
#: list_changes' parameters are never reached.
RECENT_SINCE = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")

#: The smallest valid call of each tool, so the value under test is what is
#: wrong. The bases for run_report and report_aging cover the window kinds.
BASES: dict[str, list[dict[str, Any]]] = {
    "get_company_info": [{}],
    "list_accounts": [{}],
    "list_vendors": [{}],
    "list_customers": [{}],
    "list_transactions": [{"start_date": "2026-01-01", "end_date": "2026-01-31"}],
    "get_transaction": [{"transaction_type": "Purchase", "id": "1"}],
    "run_query": [{"statement": "SELECT * FROM Vendor"}],
    "summarize_vendor_payments": [{"year": 2025}],
    "list_changes": [{"since": RECENT_SINCE}],
    "report_profit_and_loss": [{"start_date": "2026-01-01", "end_date": "2026-01-31"}],
    "report_balance_sheet": [{"as_of": "2026-01-31"}],
    "report_general_ledger": [{"start_date": "2026-01-01", "end_date": "2026-01-31"}],
    "report_aging": [
        {"side": "receivables"},
        {"side": "payables", "detail": "detail"},
    ],
    "run_report": [
        {"report": "ProfitAndLossDetail", "start_date": "2026-01-01", "end_date": "2026-01-31"},
        {"report": "VendorBalanceDetail"},
        {"report": "TrialBalance"},
        {"report": "TaxSummary", "agency_id": "1"},
    ],
    "list_reference_data": [{"kind": "terms"}],
    "list_attachments": [{}],
    "get_attachment": [{"attachable_id": "1"}],
}

#: Statements a caller composes, the marker in each place a statement has.
STATEMENTS = (
    f"SELECT * FROM {MARKER}",
    f"SELECT * FROM Vendor WHERE {MARKER} = 'x'",
    f"SELECT * FROM Vendor WHERE DisplayName = '{MARKER}' ORDERBY {MARKER}",
    f"SELECT * FROM Vendor MAXRESULTS {MARKER}",
    f"SELECT * FROM Vendor WHERE DisplayName {MARKER} 'x'",
    f"SELECT * FROM Vendor WHERE DisplayName IN ('a', {MARKER})",
    f"SELECT * FROM Vendor STARTPOSITION {NUMBER}0 MAXRESULTS {NUMBER}",
    f"{MARKER} * FROM Vendor",
)

#: Pairs whose order matters: a window ten months wide, and one reversed.
WINDOWS = (("start_date", "end_date"), ("start_duedate", "end_duedate"), ("start_date", "as_of"))


def _types(schema: dict[str, Any]) -> set[str]:
    found = {schema["type"]} if "type" in schema else set()
    for option in schema.get("anyOf", []):
        if "type" in option:
            found.add(option["type"])
    return found


def _composed_values(name: str, schema: dict[str, Any]) -> list[Any]:
    if name == "statement":
        return list(STATEMENTS)
    types = _types(schema)
    values: list[Any] = []
    if "string" in types:
        values.append(MARKER)
        if name in ("since", "updated_since"):
            values.append(OLD_STAMP)
    if "array" in types:
        values += [[MARKER], ["1", MARKER], [str(NUMBER), MARKER]]
    if "integer" in types:
        values += [MARKER, NUMBER, -NUMBER]
    if "boolean" in types:
        values.append(MARKER)
    return values


def sweep_calls(tools: list[Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """``(tool, what, arguments)`` for every composed value of every parameter."""
    calls: list[tuple[str, str, dict[str, Any]]] = []
    for tool in tools:
        properties = tool.input_schema.get("properties", {})
        for base in BASES.get(tool.name, [{}]):
            full = {"entity": "acme_main", **base} if "entity" in properties else dict(base)
            for name, schema in properties.items():
                for value in _composed_values(name, schema):
                    calls.append((tool.name, name, {**full, name: value}))
            for start_name, end_name in WINDOWS:
                if start_name in properties and end_name in properties:
                    calls.append((tool.name, f"{start_name}..{end_name}", {**full, start_name: EARLY, end_name: LATE}))
                    calls.append((tool.name, f"{end_name}<{start_name}", {**full, start_name: LATE, end_name: EARLY}))
    return calls


def violations(server: Any, calls: list[tuple[str, str, dict[str, Any]]]) -> tuple[list[str], int]:
    """Every refusal that repeats a composed value, and how many refused."""
    found: list[str] = []
    refused = 0
    for tool, what, arguments in calls:
        text = _refusal(server, tool, arguments)
        if text is None:
            continue
        refused += 1
        repeated = [value for value in COMPOSED if value in text]
        if repeated:
            found.append(f"{tool}.{what}: repeats {repeated}: {text[:200]}")
    return found, refused


def test_no_refusal_on_the_published_surface_repeats_a_composed_value(
    mock_settings, registry, credentials, actor
) -> None:
    server = _server(mock_settings, registry, credentials, actor)
    tools = anyio.run(server.list_tools)
    assert {tool.name for tool in tools} >= set(BASES), "a base call names no tool"
    needs_a_base = {tool.name for tool in tools if set(tool.input_schema.get("required", [])) - {"entity"}}
    assert needs_a_base <= set(BASES), "a tool with required arguments has no base call; add one"
    calls = sweep_calls(tools)
    found, refused = violations(server, calls)
    print(f"sweep: {len(tools)} tools, {len(calls)} calls, {refused} refused, "
          f"{len(found)} repeating a composed value")
    assert found == [], "\n".join(found)
    # Not vacuous: most composed values must actually be refused.
    assert refused >= 0.8 * len(calls), f"only {refused} of {len(calls)} calls were refused"


def test_the_sweeps_detector_sees_a_refusal_that_repeats_a_value() -> None:
    """A planted tool that repeats its argument must be reported: a check
    whose passing result is an absence is proven on a canary."""
    server = QboMCPServer(name="planted", version="0")

    @server.tool(name="echo_probe")
    def echo_probe(value: str) -> str:
        with tool_errors():
            raise QueryError(f"the value {value} is refused")

    tools = anyio.run(server.list_tools)
    found, refused = violations(server, sweep_calls(tools))
    assert refused >= 1
    assert found and all("echo_probe.value" in line for line in found), found
