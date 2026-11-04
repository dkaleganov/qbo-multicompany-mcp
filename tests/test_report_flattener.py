"""Proves the report flattener types cells correctly, keeps the section tree,
and treats report columns as inside the privacy boundary."""

from __future__ import annotations

import pytest

from qbo_multicompany_mcp.reports import (
    SENSITIVE_COLUMN_KEYS,
    ReportError,
    allowed_column_keys,
    completeness_of,
    flatten,
    parse_columns,
    requestable_columns,
    select_columns,
)


def column(key: str, ctype: str = "String", title: str = "T") -> dict:
    return {"ColTitle": title, "ColType": ctype, "MetaData": [{"Name": "ColKey", "Value": key}]}


def payload(columns: list[dict], rows: dict, *, currency: str = "USD") -> dict:
    return {
        "Header": {
            "ReportName": "TransactionList",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "Currency": currency,
        },
        "Columns": {"Column": columns},
        "Rows": rows,
    }


def cells_of(row) -> dict[str, object]:
    return {c.column_key: c for c in row.cells}


# --------------------------------------------------------------------------
# Typing
# --------------------------------------------------------------------------


def test_a_document_number_that_looks_numeric_survives_as_text() -> None:
    """The single most important rule here: no numeric inference on
    identifiers, document numbers or free text."""
    report = flatten(
        payload(
            [column("doc_num"), column("tx_date", "Date")],
            {"Row": [{"ColData": [{"value": "00123"}, {"value": "2026-02-01"}]}]},
        ),
        report_name="TransactionList",
        upstream_partial=False,
    )
    cell = cells_of(report.rows[0])["doc_num"]
    assert cell.text == "00123"
    assert cell.amount is None


def test_money_carries_an_exact_decimal_and_a_currency() -> None:
    report = flatten(
        payload(
            [column("subt_nat_amount", "Money")],
            {"Row": [{"ColData": [{"value": "1,234.50"}]}]},
        ),
        report_name="TransactionList",
        upstream_partial=False,
    )
    cell = cells_of(report.rows[0])["subt_nat_amount"]
    assert cell.text == "1,234.50"       # verbatim
    assert cell.amount == "1234.50"      # exact
    assert cell.currency == "USD"


def test_a_parenthesised_negative_is_understood() -> None:
    report = flatten(
        payload([column("nat_amount", "Money")], {"Row": [{"ColData": [{"value": "(99.99)"}]}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert cells_of(report.rows[0])["nat_amount"].amount == "-99.99"


def test_an_unparseable_money_value_keeps_its_text_and_is_disclosed() -> None:
    """The cell keeps its text and offers no amount, which alone would let the
    amount vanish without a trace. So the anomaly is listed at the top level
    and the report is not complete."""
    report = flatten(
        payload([column("nat_amount", "Money")], {"Row": [{"ColData": [{"value": "n/a"}]}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    cell = cells_of(report.rows[0])["nat_amount"]
    assert cell.text == "n/a"
    assert cell.amount is None and cell.currency is None
    assert [(a.row_id, a.column_key, a.kind) for a in report.anomalies] == [
        ("0", "nat_amount", "malformed_amount")
    ]
    assert report.completeness == "partial"


def test_an_empty_string_becomes_null() -> None:
    report = flatten(
        payload([column("memo")], {"Row": [{"ColData": [{"value": ""}]}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert cells_of(report.rows[0])["memo"].text is None


def test_a_routable_row_keeps_its_type_and_id() -> None:
    """This is how a model gets from a list row to get_transaction."""
    report = flatten(
        payload(
            [column("txn_type")],
            {"Row": [{"ColData": [{"value": "Purchase", "id": "42"}]}]},
        ),
        report_name="TransactionList",
        upstream_partial=False,
    )
    cell = cells_of(report.rows[0])["txn_type"]
    assert cell.text == "Purchase"
    assert cell.entity_id == "42"


# --------------------------------------------------------------------------
# Positional pairing
# --------------------------------------------------------------------------


def test_cells_are_paired_with_the_declared_column_order() -> None:
    """Cells do not all carry their own key. The column list is the schema."""
    report = flatten(
        payload(
            [column("tx_date", "Date"), column("txn_type"), column("doc_num")],
            {"Row": [{"ColData": [{"value": "2026-02-01"}, {"value": "Bill"}, {"value": "7"}]}]},
        ),
        report_name="TransactionList",
        upstream_partial=False,
    )
    cells = cells_of(report.rows[0])
    assert cells["tx_date"].text == "2026-02-01"
    assert cells["txn_type"].text == "Bill"
    assert cells["doc_num"].text == "7"


def test_a_short_row_has_its_values_withheld_and_is_disclosed() -> None:
    """A row with the wrong number of cells has its values withheld, whatever
    its columns. Paired by position as if the missing cells were the last,
    the amount "12.00" lands under txn_type, a wrong key, with its routing id
    kept. No cell of a short row is published; the row is listed and the
    result is partial."""
    report = flatten(
        payload(
            [column("tx_date", "Date"), column("txn_type"), column("subt_nat_amount", "Money")],
            {"Row": [{"ColData": [{"value": "2026-02-01"}, {"value": "12.00", "id": "9"}]}]},
        ),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert report.rows[0].cells == [], "a short row's values were paired by position"
    assert [(a.row_id, a.column_key, a.kind) for a in report.anomalies] == [
        ("0", None, "short_row")
    ]
    assert report.completeness == "partial"
    assert report.upstream_cell_count == 2


def test_a_column_without_a_key_cannot_be_allowlisted() -> None:
    columns = parse_columns(
        {"Columns": {"Column": [{"ColTitle": "Mystery", "ColType": "String"}]}}
    )
    assert columns[0].key == "column_0"
    kept, excluded = select_columns(columns, "TransactionList")
    assert kept == [] and excluded == ["column_0"]


def test_a_report_with_no_columns_is_an_error() -> None:
    with pytest.raises(ReportError, match="no column definitions"):
        flatten({"Rows": {}}, report_name="TransactionList", upstream_partial=False)


# --------------------------------------------------------------------------
# The privacy boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(SENSITIVE_COLUMN_KEYS)[:8])
def test_sensitive_column_keys_never_survive(key: str) -> None:
    report = flatten(
        payload([column(key)], {"Row": [{"ColData": [{"value": "SENSITIVE"}]}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert report.rows[0].cells == []
    assert key in report.excluded_column_keys


def test_an_unknown_column_key_fails_closed_and_is_named() -> None:
    """A column this package has never reviewed is the one that might carry
    something. Being named is a different statement from not existing."""
    report = flatten(
        payload([column("brand_new_key")], {"Row": [{"ColData": [{"value": "?"}]}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert report.rows[0].cells == []
    assert "brand_new_key" in report.excluded_column_keys


def test_the_allowlist_is_keyed_on_identifiers_not_display_titles() -> None:
    """Titles are localisable and user-editable, so a denylist of English
    headings protects nobody."""
    report = flatten(
        payload(
            [{"ColTitle": "Date", "ColType": "Date",
              "MetaData": [{"Name": "ColKey", "Value": "email"}]}],
            {"Row": [{"ColData": [{"value": "x@y.z"}]}]},
        ),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert report.rows[0].cells == []


def test_the_request_side_uses_the_same_allowlist() -> None:
    """A withheld column is never fetched in the first place."""
    requested = set(requestable_columns("TransactionList"))
    assert requested == set(allowed_column_keys("TransactionList"))
    assert not (requested & SENSITIVE_COLUMN_KEYS)


def test_an_unknown_report_allows_no_columns_at_all() -> None:
    assert allowed_column_keys("SomeFutureReport") == frozenset()


# --------------------------------------------------------------------------
# The section tree
# --------------------------------------------------------------------------


NESTED = payload(
    [column("tx_date", "Date"), column("subt_nat_amount", "Money")],
    {"Row": [
        {"group": "Bank",
         "Header": {"ColData": [{"value": "Bank"}, {"value": ""}]},
         "Rows": {"Row": [
             {"ColData": [{"value": "2026-02-01"}, {"value": "10.00"}]},
             {"group": "Sub",
              "Header": {"ColData": [{"value": "Sub"}, {"value": ""}]},
              "Rows": {"Row": [{"ColData": [{"value": "2026-02-02"}, {"value": "5.00"}]}]},
              "Summary": {"ColData": [{"value": "Sub total"}, {"value": "5.00"}]}},
         ]},
         "Summary": {"ColData": [{"value": "Total"}, {"value": "15.00"}]}},
    ]},
)


def test_header_children_and_summary_stay_associated_at_every_depth() -> None:
    report = flatten(NESTED, report_name="TransactionList", upstream_partial=False)
    by_id = {row.row_id: row for row in report.rows}

    assert by_id["0.header"].row_type == "section_header"
    assert by_id["0.header"].group == "Bank"
    assert by_id["0.0"].parent_row_id == "0"
    assert by_id["0.1.header"].parent_row_id == "0"
    assert by_id["0.1.0"].parent_row_id == "0.1"
    assert by_id["0.1.0"].depth == 2
    assert by_id["0.1.summary"].group == "Sub"
    assert by_id["0.summary"].group == "Bank"


def test_a_total_identifies_its_scope() -> None:
    report = flatten(NESTED, report_name="TransactionList", upstream_partial=False)
    summaries = [r for r in report.rows if r.row_type == "section_summary"]
    assert {s.scope for s in summaries} == {"Bank", "Sub"}
    assert all(s.covers == "upstream_section" for s in summaries)


def test_a_cap_signal_makes_every_total_unknown() -> None:
    """A total over truncated data is not a total over the data."""
    report = flatten(NESTED, report_name="TransactionList", upstream_partial=True)
    summaries = [r for r in report.rows if r.row_type == "section_summary"]
    assert all(s.covers == "unknown" for s in summaries)
    assert report.completeness == "unknown"


# --------------------------------------------------------------------------
# Completeness
# --------------------------------------------------------------------------


def test_upstream_cells_are_counted_before_client_trimming() -> None:
    report = flatten(NESTED, report_name="TransactionList", upstream_partial=False, max_rows=2)
    assert report.client_truncated is True
    assert len(report.rows) == 2
    assert report.upstream_row_count == 6
    assert report.upstream_cell_count == 12


def test_the_three_completeness_signals_stay_separate() -> None:
    assert completeness_of(upstream_partial=False, client_truncated=False) == "complete"
    assert completeness_of(upstream_partial=False, client_truncated=True) == "partial"
    # A cap alone makes it unknown regardless of payload size.
    assert completeness_of(upstream_partial=True, client_truncated=False) == "unknown"
    # A local trim is known loss, whatever the cap says.
    assert completeness_of(upstream_partial=True, client_truncated=True) == "partial"


@pytest.mark.parametrize(
    ("upstream_partial", "client_truncated", "anomalous", "expected"),
    [
        (False, False, False, "complete"),
        (True, False, False, "unknown"),
        (False, True, False, "partial"),
        (False, False, True, "partial"),
        (True, True, False, "partial"),
        (True, False, True, "partial"),
        (True, True, True, "partial"),
    ],
)
def test_completeness_puts_known_loss_before_an_upstream_cap(
    upstream_partial, client_truncated, anomalous, expected
) -> None:
    """No tool reaches flatten()'s answer here -- each works out its own
    completeness -- but the library's contract should match the tools',
    which call a result partial on a local trim or an anomaly whatever else
    is true (tools_data.py, tools_reports.py): both are known loss. An
    upstream cap alone stays ``unknown``."""
    assert completeness_of(
        upstream_partial=upstream_partial, client_truncated=client_truncated, anomalous=anomalous
    ) == expected


def test_flatten_calls_a_trimmed_report_partial_even_when_the_upstream_one_is() -> None:
    from copy import deepcopy

    from .test_changes_and_tools import REPORT

    source = deepcopy(REPORT)
    source["Rows"]["Row"].append(deepcopy(source["Rows"]["Row"][0]))
    flat = flatten(source, report_name="TransactionList", upstream_partial=True, max_rows=1)
    assert flat.upstream_partial and flat.client_truncated
    assert flat.completeness == "partial"


def test_an_empty_rows_array_is_not_evidence_of_no_data() -> None:
    report = flatten(
        payload([column("tx_date", "Date")], {}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert report.rows == []
    assert report.upstream_row_count == 0
    # Nothing in the result claims the company has no transactions.
    assert report.completeness == "complete"  # of THIS response, which was empty


# --------------------------------------------------------------------------
# Partial with prominent disclosure, or a failure
# --------------------------------------------------------------------------
#
# A problem confined to a cell or a row returns a PARTIAL result: the affected
# cell keeps its original text and carries no amount, every anomaly is listed
# at the top level, and completeness is never 'complete'. A problem that
# breaks the mapping between cells and columns FAILS the request, because then
# no cell can be trusted.

PRIVATE = "PRIVATE-CANARY-4471"


def kinds(report) -> list[tuple[str | None, str | None, str]]:
    return [(a.row_id, a.column_key, a.kind) for a in report.anomalies]


@pytest.mark.parametrize(
    "text",
    ["NaN", "Infinity", "-inf", "1e5", "1E+3", "1,2,3", "12.3.4", "$5.00", "--5", "5-", "( 5 )", "1 000"],
)
def test_an_amount_is_read_exactly_or_not_at_all(text: str) -> None:
    """Decimal() accepts NaN, Infinity and exponents. None of them is an
    amount a report cell should carry, and a thousands separator in the wrong
    place is not a separator."""
    report = flatten(
        payload([column("nat_amount", "Money")], {"Row": [{"ColData": [{"value": text}]}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    cell = cells_of(report.rows[0])["nat_amount"]
    assert cell.text == text
    assert cell.amount is None
    assert kinds(report) == [("0", "nat_amount", "malformed_amount")]


@pytest.mark.parametrize(
    ("text", "exact"),
    [("1,234.50", "1234.50"), ("(99.99)", "-99.99"), (".00", "0.00"), ("-5", "-5"),
     ("0", "0"), ("1,234,567.891", "1234567.891"), (" 12.00 ", "12.00")],
)
def test_the_amount_shapes_quickbooks_uses_are_read_exactly(text: str, exact: str) -> None:
    """The control: the strict parser refuses nothing a report cell really carries."""
    report = flatten(
        payload([column("nat_amount", "Money")], {"Row": [{"ColData": [{"value": text}]}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert cells_of(report.rows[0])["nat_amount"].amount == exact
    assert report.anomalies == []


def test_a_short_row_is_withheld_when_a_declared_column_is_unpublished() -> None:
    """Pairing a short row by position is safe only if every declared column is
    published: then a mis-paired value can land only under another published
    key. With a withheld column declared, a shift could publish its value --
    so the row's cells are withheld and the row is listed."""
    report = flatten(
        payload(
            [column("email"), column("memo"), column("doc_num")],
            {"Row": [{"ColData": [{"value": PRIVATE}, {"value": "x"}]}]},
        ),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert report.rows[0].cells == []
    assert kinds(report) == [("0", None, "short_row")]
    assert PRIVATE not in repr(report.rows)


@pytest.mark.parametrize("where", ["front", "back"])
def test_a_long_row_publishes_nothing_it_cannot_place(where: str) -> None:
    """A surplus cell belongs to no declared column. At the front it would
    shift every cell one to the right, putting an undeclared value under a
    published key; nothing here can tell front from back, so neither is
    paired."""
    cells = [{"value": "2026-02-01"}, {"value": "Bill"}]
    cells = [{"value": PRIVATE}, *cells] if where == "front" else [*cells, {"value": PRIVATE}]
    report = flatten(
        payload([column("tx_date", "Date"), column("txn_type")], {"Row": [{"ColData": cells}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert report.rows[0].cells == []
    assert kinds(report) == [("0", None, "long_row")]
    assert PRIVATE not in repr(report.rows)
    assert report.upstream_cell_count == 3, "surplus cells still count as upstream cells"
    assert report.completeness == "partial"


def test_an_allowlisted_column_of_an_unrecognised_type_is_text_only_and_disclosed() -> None:
    report = flatten(
        payload(
            [column("subt_nat_amount", "Currency2031")],
            {"Row": [{"ColData": [{"value": "12.00"}]}]},
        ),
        report_name="TransactionList",
        upstream_partial=False,
    )
    cell = cells_of(report.rows[0])["subt_nat_amount"]
    assert cell.text == "12.00" and cell.amount is None
    assert kinds(report) == [(None, "subt_nat_amount", "unrecognised_column_type")]
    assert report.completeness == "partial"


def test_an_amount_column_declared_as_text_is_disclosed_not_silently_amountless() -> None:
    """Otherwise the amount column's amounts are a silent null: a recognised
    type, just not the one that carries money."""
    report = flatten(
        payload([column("subt_nat_amount", "String")], {"Row": [{"ColData": [{"value": "12.00"}]}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert cells_of(report.rows[0])["subt_nat_amount"].amount is None
    assert kinds(report) == [(None, "subt_nat_amount", "unrecognised_column_type")]


def test_an_unrecognised_type_on_an_excluded_column_is_not_an_anomaly() -> None:
    """Excluded columns publish nothing, so their type changes nothing."""
    report = flatten(
        payload([column("brand_new_key", "Mystery")], {"Row": [{"ColData": [{"value": "?"}]}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert report.anomalies == []


@pytest.mark.parametrize(
    "cell",
    [PRIVATE, 12.5, {"nested": PRIVATE}, {"value": {"nested": PRIVATE}}, {"value": 12.5}],
    ids=["bare-string", "bare-number", "object-without-value", "object-value", "number-value"],
)
def test_a_cell_in_an_undocumented_shape_is_disclosed_and_never_coerced(cell) -> None:
    report = flatten(
        payload([column("subt_nat_amount", "Money")], {"Row": [{"ColData": [cell]}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    published = cells_of(report.rows[0])["subt_nat_amount"]
    assert published.text is None and published.amount is None
    if isinstance(cell, dict) and "value" not in cell:
        # A cell object with no value is an empty cell, which is ordinary.
        assert report.anomalies == []
    else:
        assert kinds(report) == [("0", "subt_nat_amount", "malformed_cell")]
    assert PRIVATE not in repr(report.rows)


# ----------------------------------------------------------- structural ---


def test_a_malformed_column_definition_fails_the_request() -> None:
    """Skip a non-object column definition at index 0 and the list compacts
    while the surviving column keeps index 1; cells then pair against the
    compacted list, so cell 0's private text comes out under the money key
    and the real amount is dropped. No partial result from such a report can
    be trusted."""
    bad = payload(
        ["not-a-column-definition", column("subt_nat_amount", "Money")],
        {"Row": [{"ColData": [{"value": PRIVATE}, {"value": "123.45"}]}]},
    )
    with pytest.raises(ReportError) as caught:
        flatten(bad, report_name="TransactionList", upstream_partial=False)
    assert PRIVATE not in str(caught.value)
    assert "column definition 0" in str(caught.value)


@pytest.mark.parametrize(
    "columns",
    [
        pytest.param([column("tx_date", "Date"), column("tx_date", "Date")], id="duplicate-key"),
        pytest.param([{"ColTitle": "T", "ColType": "String", "MetaData": "ColKey=memo"}], id="metadata-not-a-list"),
        pytest.param([{"ColTitle": "T", "ColType": "String", "MetaData": ["memo"]}], id="metadata-entry-not-an-object"),
        pytest.param([{"ColTitle": "T", "ColType": "String", "MetaData": [{"Name": "ColKey", "Value": 7}]}], id="colkey-not-a-string"),
        pytest.param([{"ColTitle": "T", "ColType": 7, "MetaData": [{"Name": "ColKey", "Value": "memo"}]}], id="coltype-not-a-string"),
        pytest.param([{"ColTitle": "T", "ColType": "String", "MetaData": [
            {"Name": "ColKey", "Value": "memo"}, {"Name": "ColKey", "Value": "doc_num"}]}], id="two-colkeys"),
    ],
)
def test_any_column_definition_that_breaks_the_mapping_fails(columns) -> None:
    with pytest.raises(ReportError):
        flatten(
            payload(columns, {"Row": [{"ColData": [{"value": "x"}]}]}),
            report_name="TransactionList",
            upstream_partial=False,
        )


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="rows-not-an-object"),
        pytest.param({"Row": {"ColData": []}}, id="row-list-not-a-list"),
        pytest.param({"Row": ["not-a-row"]}, id="row-not-an-object"),
        pytest.param({"Row": [{"ColData": {"value": "x"}}]}, id="coldata-not-a-list"),
        pytest.param({"Row": [{"Header": ["x"], "Rows": {"Row": []}}]}, id="header-not-an-object"),
        pytest.param({"Row": [{"Summary": {"ColData": "x"}}]}, id="summary-coldata-not-a-list"),
        pytest.param({"Row": [{"Rows": ["x"]}]}, id="nested-rows-not-an-object"),
    ],
)
def test_an_unsupported_shape_fails_rather_than_being_skipped(rows) -> None:
    """Skipped silently, a row that is not an object simply does not appear,
    and nothing says so."""
    with pytest.raises(ReportError):
        flatten(
            payload([column("tx_date", "Date")], rows),
            report_name="TransactionList",
            upstream_partial=False,
        )


def group(key: str, children: list[dict]) -> dict:
    """A column that groups sub-columns, as Intuit writes one."""
    return {"ColTitle": key, "MetaData": [{"Name": "ColKey", "Value": key}], "Columns": {"Column": children}}


@pytest.mark.parametrize("nested_first", [True, False], ids=["nested-first", "nested-second"])
def test_two_columns_that_would_share_a_key_fail_the_request(nested_first: bool) -> None:
    """A key joins a column's path with "/", so a ColKey holding one can match
    a nested column's key. Cells are published by key, so the unknown column's
    cells would come out under the published one's."""
    nested = group("g", [column("tx_date", "Date")])
    flat = column("g/tx_date")
    columns = [nested, flat] if nested_first else [flat, nested]
    cells = [{"value": "2026-02-01"}, {"value": PRIVATE}]
    if not nested_first:
        cells.reverse()
    with pytest.raises(ReportError, match="repeats a key") as caught:
        flatten(payload(columns, {"Row": [{"ColData": cells}]}), report_name="TransactionList", upstream_partial=False)
    assert PRIVATE not in str(caught.value)


def test_an_excluded_email_column_cannot_borrow_a_published_columns_key() -> None:
    """The sensitive gate excludes the email leaf by its own key. A period
    column keyed to match its path would otherwise have published its cells."""
    period = {
        "ColTitle": "Jan 2026",
        "ColType": "Money",
        "MetaData": [
            {"Name": "StartDate", "Value": "2026-01-01"},
            {"Name": "EndDate", "Value": "2026-01-31"},
            {"Name": "ColKey", "Value": "g/email"},
        ],
    }
    rows = {"Row": [{"ColData": [{"value": "10.00"}, {"value": PRIVATE}]}]}
    with pytest.raises(ReportError, match="repeats a key") as caught:
        flatten(
            payload([period, group("g", [column("email")])], rows),
            report_name="ProfitAndLoss",
            upstream_partial=False,
        )
    assert PRIVATE not in str(caught.value)


def test_a_row_with_nothing_this_server_reads_is_disclosed_not_dropped() -> None:
    """A row in a shape Intuit adds later would otherwise vanish from the
    result, with nothing to say so."""
    report = flatten(
        payload(
            [column("tx_date", "Date")],
            {"Row": [{"ColData": [{"value": "2026-02-01"}]}, {"type": "Data", "Cells": [{"value": PRIVATE}]}]},
        ),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert [row.row_id for row in report.rows] == ["0", "1"]
    assert report.rows[1].cells == [], "listed as a short row is: present, with nothing published"
    assert kinds(report) == [("1", None, "unrecognised_row")]
    assert report.completeness == "partial"
    assert PRIVATE not in repr(report)


def test_a_section_total_over_a_row_nothing_reads_is_flagged() -> None:
    """The total QuickBooks states covers that row, and the returned cells
    can't reproduce it, as with a short row in the same place."""
    report = flatten(
        payload(
            [column("tx_date", "Date"), column("subt_nat_amount", "Money")],
            {"Row": [{
                "group": "Bank",
                "Header": {"ColData": [{"value": "Bank"}, {"value": ""}]},
                "Rows": {"Row": [
                    {"ColData": [{"value": "2026-02-01"}, {"value": "10.00"}]},
                    {"type": "Data", "Cells": [{"value": "5.00"}]},
                ]},
                "Summary": {"ColData": [{"value": "Total"}, {"value": "15.00"}]},
            }]},
        ),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert ("0.1", None, "unrecognised_row") in kinds(report)
    assert ("0.summary", "subt_nat_amount", "stated_total_not_reproducible") in kinds(report)


HELD_ELSEWHERE = [{"ColData": [{"value": PRIVATE}]}]
ORDINARY = {"ColData": [{"value": "2026-02-01"}]}


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param({"FutureRow": HELD_ELSEWHERE}, ["0"], id="no-row-list"),
        pytest.param({"Row": None, "FutureRow": HELD_ELSEWHERE}, ["0"], id="null-row-list"),
        pytest.param({"Row": [ORDINARY], "FutureRow": HELD_ELSEWHERE}, ["0", "1"], id="beside-the-row-list"),
        pytest.param(
            {"Row": [ORDINARY, {"Rows": {"FutureRow": HELD_ELSEWHERE}}]}, ["0", "1.0"],
            id="under-a-row-holding-only-rows",
        ),
    ],
)
def test_rows_held_outside_the_row_list_are_disclosed_not_dropped(rows: dict, expected: list[str]) -> None:
    """Rows Intuit holds somewhere this server doesn't read would otherwise
    vanish, with nothing to say so. Listed as a row with nothing read is."""
    report = flatten(payload([column("tx_date", "Date")], rows), report_name="TransactionList", upstream_partial=False)
    assert [row.row_id for row in report.rows] == expected
    assert report.rows[-1].cells == []
    assert kinds(report) == [(expected[-1], None, "unrecognised_row")]
    assert report.completeness == "partial"
    assert PRIVATE not in repr(report)


def test_a_section_total_over_rows_held_outside_the_row_list_is_flagged() -> None:
    """The total covers rows the result doesn't carry, so the returned cells
    can't reproduce it."""
    report = flatten(
        payload(
            [column("tx_date", "Date"), column("subt_nat_amount", "Money")],
            {"Row": [{
                "group": "Bank",
                "Header": {"ColData": [{"value": "Bank"}, {"value": ""}]},
                "Rows": {
                    "Row": [{"ColData": [{"value": "2026-02-01"}, {"value": "10.00"}]}],
                    "FutureRow": [{"ColData": [{"value": "2026-02-02"}, {"value": "5.00"}]}],
                },
                "Summary": {"ColData": [{"value": "Total"}, {"value": "15.00"}]},
            }]},
        ),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert ("0.1", None, "unrecognised_row") in kinds(report)
    assert ("0.summary", "subt_nat_amount", "stated_total_not_reproducible") in kinds(report)


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param({}, id="empty-object"),
        pytest.param({"Row": []}, id="empty-row-list"),
        pytest.param({"Row": None}, id="null-row-list"),
        pytest.param({"Row": [], "Other": None}, id="null-member"),
        pytest.param({"Row": [], "Other": []}, id="empty-list-member"),
        pytest.param({"Row": [], "Other": {}}, id="empty-object-member"),
        pytest.param({"Row": [], "Other": ""}, id="empty-text-member"),
    ],
)
def test_a_rows_object_holding_nothing_is_an_ordinary_empty_report(rows: dict) -> None:
    """Nothing is held anywhere, so nothing is lost and the result isn't partial."""
    report = flatten(payload([column("tx_date", "Date")], rows), report_name="TransactionList", upstream_partial=False)
    assert report.rows == []
    assert report.anomalies == []
    assert report.completeness == "complete"  # of this response, which was empty


def test_a_member_a_row_holds_beside_what_this_server_reads_is_disclosed() -> None:
    """What it holds would otherwise vanish while the rest of the row comes
    back whole. Listed under the row, as a row with nothing read is."""
    report = flatten(
        payload([column("tx_date", "Date")], {"Row": [{**ORDINARY, "Detail": {"Row": HELD_ELSEWHERE}}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert [row.row_id for row in report.rows] == ["0", "0.unread"]
    assert report.rows[1].cells == [] and report.rows[1].parent_row_id == "0"
    assert kinds(report) == [("0.unread", None, "unrecognised_row")]
    assert report.anomalies[0].detail.startswith("the row holds a member")
    assert report.completeness == "partial"
    assert PRIVATE not in repr(report)


def _section_with_more_rows() -> dict:
    return payload(
        [column("tx_date", "Date"), column("subt_nat_amount", "Money")],
        {"Row": [{
            "group": "Bank",
            "Header": {"ColData": [{"value": "Bank"}, {"value": ""}]},
            "Rows": {"Row": [{"ColData": [{"value": "2026-02-01"}, {"value": "10.00"}]}]},
            "MoreRows": {"Row": [{"ColData": [{"value": "2026-02-02"}, {"value": "5.00"}]}]},
            "Summary": {"ColData": [{"value": "Total"}, {"value": "15.00"}]},
        }]},
    )


def test_a_section_total_over_a_member_this_server_doesnt_read_is_flagged() -> None:
    """Rows held beside the section's own list are covered by its total too."""
    report = flatten(_section_with_more_rows(), report_name="TransactionList", upstream_partial=False)
    assert ("0.summary", "subt_nat_amount", "stated_total_not_reproducible") in kinds(report)
    assert ("0.unread", None, "unrecognised_row") in kinds(report)


def test_what_a_section_holds_unread_comes_before_its_total() -> None:
    """Read in order, every row of a section comes before the section's total."""
    report = flatten(_section_with_more_rows(), report_name="TransactionList", upstream_partial=False)
    assert [row.row_id for row in report.rows] == ["0.header", "0.0", "0.unread", "0.summary"]


def _section_holding(wrapper: str, extra: dict) -> dict:
    """A section whose header or total holds ``extra`` beside its cells. The
    total matches the rows, so a flag on it can only come from ``extra``."""
    section = {
        "group": "Bank",
        "Header": {"ColData": [{"value": "Bank"}, {"value": ""}]},
        "Rows": {"Row": [{"ColData": [{"value": "2026-02-01"}, {"value": "10.00"}]}]},
        "Summary": {"ColData": [{"value": "Total"}, {"value": "10.00"}]},
    }
    section[wrapper] = {**section[wrapper], **extra}
    return payload([column("tx_date", "Date"), column("subt_nat_amount", "Money")], {"Row": [section]})


@pytest.mark.parametrize("wrapper", ["Header", "Summary"])
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"Rows": {"Row": [{"ColData": [{"value": PRIVATE}, {"value": "5.00"}]}]}}, id="rows"),
        pytest.param({"Detail": HELD_ELSEWHERE}, id="other-member"),
    ],
)
def test_what_a_header_or_total_holds_beside_its_cells_is_disclosed(wrapper: str, extra: dict) -> None:
    """A header and a total hold only cells, so anything beside them would
    vanish while the section came back whole. Listed under the section, before
    its total, so the total is flagged too."""
    report = flatten(_section_holding(wrapper, extra), report_name="TransactionList", upstream_partial=False)
    assert [row.row_id for row in report.rows] == ["0.header", "0.0", "0.unread", "0.summary"]
    assert ("0.unread", None, "unrecognised_row") in kinds(report)
    assert ("0.summary", "subt_nat_amount", "stated_total_not_reproducible") in kinds(report)
    assert report.completeness == "partial"
    assert PRIVATE not in repr(report)


@pytest.mark.parametrize("wrapper", ["Header", "Summary"])
@pytest.mark.parametrize("empty", [None, "", [], {}], ids=["null", "empty-text", "empty-list", "empty-object"])
def test_a_header_or_total_holding_only_its_cells_stays_clean(wrapper: str, empty: object) -> None:
    """A member holding nothing loses nothing."""
    report = flatten(_section_holding(wrapper, {"Other": empty}), report_name="TransactionList", upstream_partial=False)
    assert [row.row_id for row in report.rows] == ["0.header", "0.0", "0.summary"]
    assert report.anomalies == []


@pytest.mark.parametrize(
    "row",
    [
        pytest.param({"type": "Data", **ORDINARY}, id="type"),
        pytest.param({"group": "Bank", **ORDINARY}, id="group"),
        pytest.param({**ORDINARY, "Other": None}, id="null-member"),
        pytest.param({**ORDINARY, "Other": []}, id="empty-list-member"),
        pytest.param({**ORDINARY, "Other": {}}, id="empty-object-member"),
        pytest.param({**ORDINARY, "Other": ""}, id="empty-text-member"),
    ],
)
def test_a_row_holding_only_what_this_server_reads_stays_clean(row: dict) -> None:
    """The members it knows, and members holding nothing, lose nothing."""
    report = flatten(payload([column("tx_date", "Date")], {"Row": [row]}), report_name="TransactionList", upstream_partial=False)
    assert [r.row_id for r in report.rows] == ["0"]
    assert report.anomalies == []


def test_a_structural_failure_is_an_error_the_model_can_read() -> None:
    """A ReportError that is not a QboMcpError reaches the model as the
    generic 'failed unexpectedly', which says nothing about why."""
    from qbo_multicompany_mcp.errors import QboMcpError

    assert issubclass(ReportError, QboMcpError)


#: What an object's text would carry into a result: a field nobody reviewed.
OBJECT_FIELD = {"PrimaryEmailAddr": {"Address": "nested-canary"}}


@pytest.mark.parametrize(
    "field", ["Currency", "StartPeriod", "EndPeriod", "ReportBasis", "SummarizeColumnsBy", "DateMacro"]
)
def test_a_header_field_that_is_an_object_fails_rather_than_becoming_text(field: str) -> None:
    """The column and cell checks never see the header, so its fields are
    checked where they become text."""
    report = payload([column("tx_date", "Date")], {"Row": [{"ColData": [{"value": "2026-02-01"}]}]})
    report["Header"][field] = OBJECT_FIELD
    with pytest.raises(ReportError, match="not a single value") as caught:
        flatten(report, report_name="TransactionList", upstream_partial=False)
    assert "nested-canary" not in str(caught.value)


@pytest.mark.parametrize("group", [OBJECT_FIELD, ["Bank", OBJECT_FIELD]], ids=["an object", "a list"])
def test_a_row_group_that_is_not_one_value_fails_rather_than_becoming_text(group) -> None:
    rows = {"Row": [{"group": group, "ColData": [{"value": "2026-02-01"}]}]}
    with pytest.raises(ReportError, match="not a single value") as caught:
        flatten(payload([column("tx_date", "Date")], rows), report_name="TransactionList", upstream_partial=False)
    assert "nested-canary" not in str(caught.value)


# --------------------------------------------------------------- totals ---


SECTIONED = payload(
    [column("tx_date", "Date"), column("subt_nat_amount", "Money")],
    {"Row": [
        {"group": "Bank",
         "Header": {"ColData": [{"value": "Bank"}, {"value": ""}]},
         "Rows": {"Row": [
             {"ColData": [{"value": "2026-02-01"}, {"value": "10.00"}]},
             {"ColData": [{"value": "2026-02-02"}, {"value": "n/a"}]},
         ]},
         "Summary": {"ColData": [{"value": "Total"}, {"value": "15.00"}]}},
        {"group": "Card",
         "Header": {"ColData": [{"value": "Card"}, {"value": ""}]},
         "Rows": {"Row": [{"ColData": [{"value": "2026-02-03"}, {"value": "7.00"}]}]},
         "Summary": {"ColData": [{"value": "Total"}, {"value": "7.00"}]}},
    ]},
)


def test_a_total_quickbooks_states_is_reported_as_stated_and_flagged() -> None:
    """QuickBooks's own figure is not changed by a parse failure, so it is not
    nulled. What changes is that summing the returned cells would not
    reproduce it, and that is listed."""
    report = flatten(SECTIONED, report_name="TransactionList", upstream_partial=False)
    by_id = {row.row_id: row for row in report.rows}
    assert cells_of(by_id["0.summary"])["subt_nat_amount"].amount == "15.00"
    assert ("0.summary", "subt_nat_amount", "stated_total_not_reproducible") in kinds(report)
    # The other section has no affected cell, so its total is not flagged.
    assert ("1.summary", "subt_nat_amount", "stated_total_not_reproducible") not in kinds(report)


def test_a_total_the_server_computes_over_an_affected_cell_is_unknown() -> None:
    from qbo_multicompany_mcp.reports import computed_total

    total = computed_total(
        flatten(SECTIONED, report_name="TransactionList", upstream_partial=False),
        "subt_nat_amount",
    )
    assert total.amount is None and total.status == "unknown"
    assert "could not be read" in total.reason


def test_a_total_the_server_computes_over_clean_cells_is_exact() -> None:
    """The control: data rows only, section totals never added in."""
    from qbo_multicompany_mcp.reports import computed_total

    total = computed_total(
        flatten(NESTED, report_name="TransactionList", upstream_partial=False),
        "subt_nat_amount",
    )
    assert (total.amount, total.status, total.reason) == ("15.00", "computed", None)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [({"upstream_partial": True}, "cap"), ({"upstream_partial": False, "max_rows": 2}, "trimmed")],
)
def test_a_computed_total_over_an_incomplete_report_is_unknown(kwargs, reason: str) -> None:
    from qbo_multicompany_mcp.reports import computed_total

    total = computed_total(flatten(NESTED, report_name="TransactionList", **kwargs), "subt_nat_amount")
    assert total.amount is None and total.status == "unknown"
    assert reason in total.reason


def test_a_computed_total_never_treats_an_empty_cell_as_zero() -> None:
    from qbo_multicompany_mcp.reports import computed_total

    report = flatten(
        payload([column("subt_nat_amount", "Money")],
                {"Row": [{"ColData": [{"value": "4.00"}]}, {"ColData": [{"value": ""}]}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    total = computed_total(report, "subt_nat_amount")
    assert total.amount is None and "empty" in total.reason


# ------------------------------------------------------- the disclosure ---


def test_completeness_is_never_complete_while_an_anomaly_exists() -> None:
    assert completeness_of(upstream_partial=False, client_truncated=False, anomalous=True) == "partial"
    # With a cap too: an anomaly is known loss.
    assert completeness_of(upstream_partial=True, client_truncated=False, anomalous=True) == "partial"


def test_the_disclosure_sentence_leads_with_partial() -> None:
    from qbo_multicompany_mcp.reports import disclosure_sentence

    report = flatten(SECTIONED, report_name="TransactionList", upstream_partial=False)
    sentence = disclosure_sentence(report, completeness="partial")
    assert sentence.startswith("PARTIAL")
    assert "anomalies" in sentence
    clean = disclosure_sentence(
        flatten(NESTED, report_name="TransactionList", upstream_partial=False),
        completeness="unknown",
    )
    assert not clean.startswith("PARTIAL")


def test_a_trimmed_report_says_in_its_first_sentence_that_rows_were_withheld() -> None:
    """A local trim is known loss, so the sentence a model reads first names
    it -- with anomalies or without -- and a report that was not trimmed says
    nothing of the kind. Every report tool and list_transactions share this
    sentence."""
    from qbo_multicompany_mcp.reports import disclosure_sentence

    trimmed = flatten(NESTED, report_name="TransactionList", upstream_partial=False, max_rows=2)
    assert (trimmed.upstream_row_count, len(trimmed.rows), trimmed.client_truncated) == (6, 2, True)
    first = disclosure_sentence(trimmed, completeness="partial").split(". ")[0]
    assert not first.startswith("PARTIAL RESULT")
    assert "2 rows returned and 4 withheld by max_rows" in first

    anomalous = flatten(SECTIONED, report_name="TransactionList", upstream_partial=False, max_rows=2)
    assert anomalous.anomalies and anomalous.client_truncated
    first = disclosure_sentence(anomalous, completeness="partial").split(". ")[0]
    assert first.startswith("PARTIAL RESULT")
    assert "5 rows beyond max_rows were withheld" in first

    untrimmed = flatten(NESTED, report_name="TransactionList", upstream_partial=False)
    assert "withheld" not in disclosure_sentence(untrimmed, completeness="unknown")


#: The TransactionList column definitions a live sandbox response declared on
#: 2026-09-23, for both sandbox companies, when asked for this package's default
#: column set. Definitions only -- no values from the response are used here.
#:
#: `create_date` and `last_mod_date` arrive as ``TimeStamp``, a type absent from
#: every documentation sample. Unrecognised, it marks every live
#: list_transactions result partial.
LIVE_TRANSACTIONLIST_COLUMNS_2026_09_23 = (
    ("tx_date", "Date"), ("txn_type", "String"), ("doc_num", "String"),
    ("is_no_post", "Boolean"), ("create_date", "TimeStamp"),
    ("last_mod_date", "TimeStamp"), ("name", "String"), ("memo", "String"),
    ("account_name", "String"), ("other_account", "String"),
    ("term_name", "String"), ("due_date", "Date"), ("is_ap_paid", "String"),
    ("is_cleared", "String"), ("subt_nat_amount", "Money"),
    ("nat_open_bal", "Money"),
)


def test_the_live_column_vocabulary_produces_no_anomaly() -> None:
    """The column definitions QuickBooks actually sends must not, by themselves,
    mark a report partial. An unrecognised type is disclosed loudly, which is
    right for a genuinely unknown type and wrong for one the live API always
    sends: a tool whose every result says PARTIAL teaches its reader to ignore
    the word."""
    # A value valid for each declared type, so the only thing under test is
    # whether the TYPE is recognised. An "x" in a Money column is a malformed
    # amount -- a real anomaly, and not the one this test is about.
    valid = {"Money": "12.00", "Date": "2026-02-01",
             "TimeStamp": "2026-02-01T10:00:00-08:00", "Boolean": "false"}
    columns = [column(key, typ) for key, typ in LIVE_TRANSACTIONLIST_COLUMNS_2026_09_23]
    values = [{"value": valid.get(typ, "x")} for _, typ in LIVE_TRANSACTIONLIST_COLUMNS_2026_09_23]
    report = flatten(
        payload(columns, {"Row": [{"ColData": values}]}),
        report_name="TransactionList",
        upstream_partial=False,
    )
    assert kinds(report) == []
    assert report.completeness != "partial"
