"""The report shapes observed in sandbox, and what the parser must do about
each.

Each fixture in ``tests/fixtures/report_shapes.py`` mirrors the structure of a
live sandbox response with synthetic values.

The anomaly rules hold throughout: cell-level anomalies are disclosed and
partial, anything that breaks the cell-to-column mapping fails the request.
Nested columns and keyless labels are the documented shape, not anomalies, and
the parser makes them ordinary before the anomaly rules run.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from qbo_multicompany_mcp import reports as R
from qbo_multicompany_mcp.reports import ReportError, flatten

from .fixtures import report_shapes as S


def kinds(report) -> list[tuple[str | None, str | None, str]]:
    return [(a.row_id, a.column_key, a.kind) for a in report.anomalies]


def by_key(row) -> dict[str, Any]:
    return {c.column_key: c for c in row.cells}


def data_rows(report):
    return [r for r in report.rows if r.row_type == R.ROW_DATA]


# ==========================================================================
# Nested columns (ItemSales): flatten to leaves before pairing cells
# ==========================================================================


def test_nested_columns_are_flattened_to_leaves_before_cells_are_paired() -> None:
    """ItemSales declares two top-level columns; the second holds seven
    sub-columns, so rows carry EIGHT cells. Read at the top level, every row
    is a long row and has its values withheld: an empty report, silently
    wrong."""
    report = flatten(S.item_sales(), report_name="ItemSales", upstream_partial=False)

    assert [c.key for c in report.columns] == [
        "item", "total/Quantity", "total/Amount", "total/PercentSales", "total/AvgPrice",
        "total/Cogs", "total/GrossMargin", "total/GrossMarginPerc",
    ]
    assert kinds(report) == [], "the documented nested shape is not an anomaly"
    assert all(len(row.cells) == 8 for row in report.rows), "a row of leaf cells was withheld"
    assert report.completeness == "complete"


def test_a_leaf_carries_its_parent_title_and_period_and_its_identity_is_its_path() -> None:
    report = flatten(S.item_sales(), report_name="ItemSales", upstream_partial=False)
    amount = next(c for c in report.columns if c.key == "total/Amount")
    assert amount.leaf_key == "Amount"
    assert amount.parent_title == "Total"
    assert (amount.period_start, amount.period_end) == ("2026-01-01", "2026-06-30")
    assert amount.index == 2, "leaf index, not top-level index"

    cell = by_key(report.rows[0])["total/Amount"]
    assert (cell.text, cell.amount, cell.currency) == ("20.00", "20.00", "USD")
    assert cell.period == "2026-01-01/2026-06-30"
    assert by_key(report.rows[0])["total/PercentSales"].period is None


def test_two_leaves_with_the_same_key_under_different_parents_stay_distinct() -> None:
    """Identity is the path. Two parents each holding an ``Amount`` leaf are
    two columns, not a duplicate-key structural failure."""
    payload = S.item_sales()
    payload["Columns"]["Column"].append(
        S.col("prior", "Money", "Prior", children=[S.col("Amount", "Money", "Amount")])
    )
    for row in payload["Rows"]["Row"]:
        row["ColData"].append(S.cell("1.00"))
    report = flatten(payload, report_name="ItemSales", upstream_partial=False)
    assert "prior/Amount" in [c.key for c in report.columns]
    assert kinds(report) == []


def test_the_leaf_count_is_what_a_row_is_measured_against() -> None:
    """A row carrying only the two top-level cells IS short: a row's cell
    count is measured in leaves, and a short row has its values withheld."""
    payload = S.item_sales()
    payload["Rows"]["Row"][0]["ColData"] = [S.cell("Example Item", "1"), S.cell("20.00")]
    report = flatten(payload, report_name="ItemSales", upstream_partial=False)
    assert kinds(report) == [("0", None, "short_row")]
    assert report.rows[0].cells == []


@pytest.mark.parametrize(
    "children",
    [pytest.param("not-a-block", id="columns-not-an-object"),
     pytest.param({"Column": "not-a-list"}, id="column-not-a-list"),
     pytest.param({"Column": ["not-an-object"]}, id="child-not-an-object")],
)
def test_a_malformed_nested_column_block_fails_the_request(children) -> None:
    """Structural: a parent whose children cannot be read leaves every leaf
    index in doubt."""
    payload = S.item_sales()
    payload["Columns"]["Column"][1]["Columns"] = children
    with pytest.raises(ReportError):
        flatten(payload, report_name="ItemSales", upstream_partial=False)


# ==========================================================================
# Period columns have dynamic keys: admit by shape, surface the period
# ==========================================================================


def test_a_month_column_is_admitted_by_its_period_metadata_and_money_type() -> None:
    """'Jan 2026' is not in any allowlist and cannot be: the key changes with
    the window."""
    report = flatten(S.profit_and_loss_by_month(), report_name="ProfitAndLoss", upstream_partial=False)
    assert report.excluded_column_keys == []
    jan = next(c for c in report.columns if c.key == "Jan 2026")
    assert (jan.period_start, jan.period_end) == ("2026-01-01", "2026-01-31")
    assert jan.is_money
    assert kinds(report) == []


def test_the_period_is_surfaced_on_the_cell_and_the_total_column_has_none() -> None:
    report = flatten(S.profit_and_loss_by_month(), report_name="ProfitAndLoss", upstream_partial=False)
    deepest = next(r for r in data_rows(report) if r.row_id == "0.0.0.0")
    cells = by_key(deepest)
    assert cells["Jan 2026"].amount == "10.00"
    assert cells["Jan 2026"].period == "2026-01-01/2026-01-31"
    assert cells["total"].amount == "60.00"
    assert cells["total"].period is None
    assert cells["account"].entity_id == "12", "the account id routes the row"


def test_a_quarter_column_with_a_comma_in_its_key_is_admitted_the_same_way() -> None:
    """BalanceSheet summarised by Quarter keys by 'Jan - Mar, 2026'."""
    report = flatten(S.balance_sheet_by_quarter(), report_name="BalanceSheet", upstream_partial=False)
    assert [c.key for c in report.columns] == ["account", "Jan - Mar, 2026", "Apr - Jun, 2026"]
    assert report.summarize_columns_by == "Quarter"
    assert report.date_macro == "this calendar year-to-date"
    assert kinds(report) == []


def test_a_period_column_of_a_non_money_type_is_not_admitted_by_the_period_rule() -> None:
    """The shape is StartDate AND EndDate AND Money. A String column with
    period metadata and an unknown key stays excluded, named."""
    payload = S.profit_and_loss_by_month()
    payload["Columns"]["Column"][1]["ColType"] = "String"
    report = flatten(payload, report_name="ProfitAndLoss", upstream_partial=False)
    assert report.excluded_column_keys == ["Jan 2026"]


def test_a_period_column_is_not_admitted_on_a_report_that_declares_no_periods() -> None:
    """TransactionList's shape has no period columns; the rule is per report,
    so a period-shaped Money column there stays excluded."""
    payload = S.profit_and_loss_by_month()
    payload["Header"]["ReportName"] = "TransactionList"
    report = flatten(payload, report_name="TransactionList", upstream_partial=False)
    assert "Jan 2026" in report.excluded_column_keys


PERIOD_REPORTS = sorted(name for name, shape in R.REPORT_SHAPES.items() if shape.period_columns)
MARKED_CELL = "marked-cell"


def _unreviewed_money_column(report_name: str, start: str, end: str) -> dict[str, Any]:
    """A report with one Money column no allowlist names, its cells marked."""
    payload = S.profit_and_loss_by_month()
    payload["Header"]["ReportName"] = report_name
    meta = payload["Columns"]["Column"][1]["MetaData"]
    for item in meta:
        if item["Name"] == "StartDate":
            item["Value"] = start
        elif item["Name"] == "EndDate":
            item["Value"] = end

    def mark(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            for holder in (row, row.get("Header") or {}, row.get("Summary") or {}):
                if holder.get("ColData"):
                    holder["ColData"][1] = {"value": MARKED_CELL}
            mark((row.get("Rows") or {}).get("Row") or [])

    mark(payload["Rows"]["Row"])
    return payload


@pytest.mark.parametrize("report_name", PERIOD_REPORTS)
@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("not a date", "2026-01-31"),
        ("2026-01-01", "not a date"),
        ("2026-02-31", "2026-03-31"),
        ("2026-01-31", "2026-01-01"),
        ("20260101", "20260131"),
        ("2026-W01-1", "2026-W05-1"),
        (" 2026-01-01", "2026-01-31"),
    ],
)
def test_an_unreviewed_money_column_with_bad_period_dates_stays_out(report_name, start, end) -> None:
    """Admission on shape needs a real period: two real dates, in order. On
    every report that admits period columns, anything else stays excluded and
    named, and none of its cells leave."""
    payload = _unreviewed_money_column(report_name, start, end)
    report = flatten(payload, report_name=report_name, upstream_partial=False)
    assert "Jan 2026" in report.excluded_column_keys
    assert MARKED_CELL not in repr(report.rows)


@pytest.mark.parametrize("report_name", PERIOD_REPORTS)
def test_the_same_column_with_good_dates_passes_on_its_shape_and_says_so(report_name) -> None:
    """The control for the test above, and the limit of the rule: no allowlist
    names this column. It passes because it looks like a period."""
    payload = _unreviewed_money_column(report_name, "2026-01-01", "2026-01-31")
    report = flatten(payload, report_name=report_name, upstream_partial=False)
    column = next(c for c in report.columns if c.key == "Jan 2026")
    assert column.admitted_by == "period"
    assert MARKED_CELL in repr(report.rows)


# ==========================================================================
# Keyless label columns: admitted by position and type, never by key
# ==========================================================================


def test_a_keyless_label_column_is_admitted_on_a_hierarchical_report() -> None:
    """AgedPayables' first column has no ColKey and type Vendor; it carries the
    row's identity."""
    report = flatten(S.aged_payables(), report_name="AgedPayables", upstream_partial=False)
    label = report.columns[0]
    assert (label.key, label.leaf_key, label.type, label.synthetic_key) == ("vendor", None, "Vendor", True)
    first = by_key(report.rows[0])
    assert first["vendor"].text == "Example Vendor A"
    assert first["vendor"].entity_id == "1"
    assert len(report.rows[0].cells) == 7
    assert kinds(report) == []


@pytest.mark.parametrize(
    ("ctype", "expected_key"),
    [("Account", "account"), ("Customer", "customer"), ("Vendor", "vendor"),
     ("ProductsAndService", "item"), ("Class", "class"), ("Department", "department"), ("String", "label")],
)
def test_the_admitted_label_types_and_their_synthetic_keys(ctype: str, expected_key: str) -> None:
    payload = S.aged_payables()
    payload["Columns"]["Column"][0]["ColType"] = ctype
    report = flatten(payload, report_name="AgedPayables", upstream_partial=False)
    assert report.columns[0].key == expected_key


def test_a_keyless_column_of_another_type_or_position_is_still_excluded() -> None:
    payload = S.aged_payables()
    payload["Columns"]["Column"][0]["ColType"] = "Money"  # keyless Money at 0: not a label
    report = flatten(payload, report_name="AgedPayables", upstream_partial=False)
    assert "column_0" in report.excluded_column_keys

    payload = S.aged_payables()
    payload["Columns"]["Column"].insert(3, S.col(None, "Vendor", "Mystery"))  # a label type, not at 0
    for row in payload["Rows"]["Row"]:
        if "ColData" in row:
            row["ColData"].insert(3, S.cell("x"))
        else:
            row["Summary"]["ColData"].insert(3, S.cell("x"))
    report = flatten(payload, report_name="AgedPayables", upstream_partial=False)
    assert "column_3" in report.excluded_column_keys


def test_trial_balance_with_no_key_on_any_column_admits_the_label_and_the_titled_money() -> None:
    """TrialBalance has no ColKey on any column. The label is admitted by
    position; the two Money columns are Debit and Credit by ColTitle and are
    admitted by title under the synthetic keys `debit` and `credit`, the
    column saying so. A keyless Money column of any OTHER title stays excluded
    and named."""
    report = flatten(S.trial_balance(), report_name="TrialBalance", upstream_partial=False)
    assert [c.key for c in report.columns] == ["account", "debit", "credit"]
    assert report.excluded_column_keys == []
    debit = report.columns[1]
    assert (debit.leaf_key, debit.synthetic_key, debit.admitted_by, debit.title) == (None, True, "title", "Debit")
    assert report.columns[0].admitted_by == "label_position"
    assert by_key(report.rows[0])["account"].entity_id == "1"
    assert by_key(report.rows[0])["debit"].amount == "100.00"
    assert kinds(report) == []
    payload = S.trial_balance()
    payload["Columns"]["Column"][2]["ColTitle"] = "Mystery"
    report = flatten(payload, report_name="TrialBalance", upstream_partial=False)
    assert report.excluded_column_keys == ["column_2"]


def test_the_transactionlist_rule_is_kept_for_detail_reports() -> None:
    """'A column without a key cannot be allowlisted' stays right for the
    detail-style reports: a keyless String at position 0 of GeneralLedger is
    not a label column."""
    payload = S.general_ledger()
    payload["Columns"]["Column"][0] = S.col(None, "String", "Date")
    report = flatten(payload, report_name="GeneralLedger", upstream_partial=False)
    assert "column_0" in report.excluded_column_keys
    assert R.REPORT_SHAPES["GeneralLedger"].style == R.STYLE_DETAIL


# ==========================================================================
# An absent ColType on an empty report is disclosed once, not partial
# ==========================================================================


def test_absent_types_on_a_report_with_no_rows_are_disclosed_not_partial() -> None:
    """InventoryValuationDetail over an empty window: nine columns, no ColType
    on any, Rows an empty object. There is nothing to type."""
    report = flatten(S.inventory_valuation_detail_empty(), report_name="InventoryValuationDetail", upstream_partial=False)
    assert report.rows == []
    assert kinds(report) == []
    assert report.completeness == "complete"
    assert len(report.disclosures) == 1
    assert "no type" in report.disclosures[0] and "no rows" in report.disclosures[0]
    assert report.no_report_data is True


def test_absent_types_with_rows_to_type_are_still_anomalies() -> None:
    """The control: the same columns over a window WITH rows (the live
    response declared them all). Strip the types and rows exist -> every
    published column is an unrecognised-type anomaly and the result is
    partial."""
    payload = S.inventory_valuation_detail_with_rows()
    for column in payload["Columns"]["Column"]:
        del column["ColType"]
    report = flatten(payload, report_name="InventoryValuationDetail", upstream_partial=False)
    assert {k for _, k, kind in kinds(report) if kind == "unrecognised_column_type"} == {k for k, _, _ in S.IVD_COLUMNS}
    assert report.completeness == "partial"
    assert report.disclosures == []


def test_the_absent_type_disclosure_reaches_the_first_sentence() -> None:
    report = flatten(S.inventory_valuation_detail_empty(), report_name="InventoryValuationDetail", upstream_partial=False)
    sentence = R.disclosure_sentence(report, completeness="complete")
    assert not sentence.startswith("PARTIAL")
    assert "no type" in sentence


# ==========================================================================
# Quantity: numeric, not money
# ==========================================================================


def test_quantity_is_recognised_as_numeric_without_a_currency() -> None:
    """OBSERVED live 2026-09-24: InventoryValuationDetail with rows declared
    ``quantity`` as ``Quantity``."""
    assert "Quantity" in R.RECOGNISED_COLUMN_TYPES
    assert "Quantity" not in R.MONEY_COLUMN_TYPES
    report = flatten(S.inventory_valuation_detail_with_rows(), report_name="InventoryValuationDetail", upstream_partial=False)
    assert kinds(report) == []
    cell = by_key(data_rows(report)[0])["quantity"]
    assert (cell.text, cell.amount, cell.currency) == ("-3", "-3", None)
    money = by_key(data_rows(report)[0])["home_amount"]
    assert (money.amount, money.currency) == ("-7.50", "USD")


def test_a_malformed_quantity_is_disclosed_like_a_malformed_amount() -> None:
    payload = S.inventory_valuation_detail_with_rows()
    payload["Rows"]["Row"][0]["Rows"]["Row"][0]["ColData"][4]["value"] = "three"
    report = flatten(payload, report_name="InventoryValuationDetail", upstream_partial=False)
    assert ("0.0", "quantity", "malformed_amount") in kinds(report)
    assert by_key(data_rows(report)[0])["quantity"].text == "three"
    assert by_key(data_rows(report)[0])["quantity"].amount is None


# ==========================================================================
# A column keyed Quantity is a quantity whatever its ColType
# ==========================================================================
#
# Intuit declares two quantity columns ColType Money --
# InventoryValuationSummary's 'Qty' and ItemSales' 'Quantity', both ColKey
# ``Quantity`` (observed in sandbox on 2026-09-24; the samples on Intuit's
# pages agree). Decided by ColType, ten units are published as ten dollars.
# The key decides, like the percent rule: the number, no currency, never in a
# money total.


def test_inventory_valuation_summarys_money_typed_qty_is_a_quantity() -> None:
    from .fixtures import report_shapes_run_report as Q

    report = flatten(Q.inventory_valuation_summary(), report_name="InventoryValuationSummary", upstream_partial=False)
    assert kinds(report) == []
    cells = by_key(data_rows(report)[0])
    quantity = cells["Quantity"]
    assert (quantity.text, quantity.amount, quantity.currency, quantity.unit) == ("10.00", "10.00", None, None)
    assert (cells["HomeAmount"].amount, cells["HomeAmount"].currency) == ("20.00", "USD")
    column = next(c for c in report.columns if c.key == "Quantity")
    assert column.type == "Money", "the declared type is still reported as declared"
    assert column.is_quantity and not column.is_money


def test_item_sales_money_typed_quantity_is_a_quantity_and_keeps_its_period() -> None:
    """ItemSales' Quantity is a nested leaf with period metadata: the cell
    keeps its period and loses its currency."""
    report = flatten(S.item_sales(), report_name="ItemSales", upstream_partial=False)
    assert kinds(report) == []
    cells = by_key(data_rows(report)[0])
    quantity = cells["total/Quantity"]
    assert (quantity.amount, quantity.currency, quantity.period) == ("2", None, "2026-01-01/2026-06-30")
    assert cells["total/Amount"].currency == "USD"


@pytest.mark.parametrize(
    ("report_name", "key"),
    [("InventoryValuationSummary", "Quantity"), ("ItemSales", "total/Quantity")],
)
def test_a_quantity_column_never_enters_a_money_total(report_name: str, key: str) -> None:
    from .fixtures import report_shapes_run_report as Q

    builder = Q.inventory_valuation_summary if report_name == "InventoryValuationSummary" else S.item_sales
    report = flatten(builder(), report_name=report_name, upstream_partial=False)
    total = R.computed_total(report, key)
    assert (total.status, total.amount) == (R.UNKNOWN, None)
    assert "monetary" in (total.reason or "")


def test_the_quantity_key_decides_on_any_report_the_lowercase_key_included() -> None:
    """Key-based, not per report. JournalReport documents a ``quantity``
    column no live response has shown; declared Money it is still a quantity.
    ``quantity`` is the key InventoryValuationDetail declares ColType Quantity
    (observed in sandbox on 2026-09-24)."""
    payload = {
        "Header": S.header("JournalReport"),
        "Columns": {"Column": [
            S.col("tx_date", "Date", "Date"), S.col("txn_type", "String", "Transaction Type"),
            S.col("quantity", "Money", "Qty"), S.col("debt_amt", "Money", "Debit"),
        ]},
        "Rows": {"Row": [S.data([S.cell("2026-01-05"), S.cell("Journal Entry", "7"), S.cell("3"), S.cell("12.00")])]},
    }
    report = flatten(payload, report_name="JournalReport", upstream_partial=False)
    assert kinds(report) == []
    cells = by_key(data_rows(report)[0])
    assert (cells["quantity"].amount, cells["quantity"].currency) == ("3", None)
    assert (cells["debt_amt"].amount, cells["debt_amt"].currency) == ("12.00", "USD")


def test_no_report_shape_lists_a_quantity_key_among_its_money_keys() -> None:
    """A shape's money keys are flagged when declared non-monetary, so a
    quantity key there would turn every such report PARTIAL."""
    for name, shape in R.REPORT_SHAPES.items():
        assert not (shape.money_keys & R.QUANTITY_COLUMN_KEYS), name


# ==========================================================================
# Aging buckets: keyed current/0/1/2/3/total, the title carried on the cell
# ==========================================================================


def test_aging_buckets_are_admitted_by_key_and_carry_their_title() -> None:
    report = flatten(S.aged_payables(), report_name="AgedPayables", upstream_partial=False)
    assert [c.key for c in report.columns] == ["vendor", "current", "0", "1", "2", "3", "total"]
    assert next(c for c in report.columns if c.key == "0").title == "1 - 30"
    cells = by_key(report.rows[0])
    assert cells["1"].label == "31 - 60"
    assert cells["1"].amount == "5.00"
    assert cells["current"].label == "Current"
    assert cells["vendor"].label is None, "the label column's own title is empty"


def test_the_receivables_side_keeps_a_nested_customer_with_its_header_id() -> None:
    """AgedReceivables: a parent customer is a Section whose Header carries
    the id in cell 0."""
    report = flatten(S.aged_receivables(), report_name="AgedReceivables", upstream_partial=False)
    by_id = {r.row_id: r for r in report.rows}
    assert by_key(by_id["1.header"])["customer"].entity_id == "2"
    assert by_id["1.0"].parent_row_id == "1"
    assert by_id["1.summary"].scope == "section 1"
    grand = by_id["2.summary"]
    assert grand.scope == "GrandTotal"
    assert by_key(grand)["total"].amount == "7.00"
    assert kinds(report) == []


# ==========================================================================
# Entity groupings: Money columns keyed by entity id, admitted by shape
# under a request that named the grouping (observed 2026-09-25)
# ==========================================================================


def test_grouping_columns_are_admitted_by_shape_under_an_entity_grouping() -> None:
    """ProfitAndLoss by Customers: Money columns keyed by CUSTOMER ID with the
    name as ColTitle, plus not_specified and total, and no period metadata.
    The period rule reads date metadata, so on its own it excludes every one
    of these and the caller gets labels with no amounts. They are admitted by
    shape -- Money, an integer-like key or not_specified -- under a request
    that named the grouping; the id goes on the column as entity_id."""
    report = flatten(S.profit_and_loss_by_customers(), report_name="ProfitAndLoss",
                     upstream_partial=False, entity_grouping="Customers")
    assert report.excluded_column_keys == []
    assert [c.key for c in report.columns] == [
        "account", "2", "7", "8", "7 (total)", "20", "not_specified", "total",
    ]
    two = next(c for c in report.columns if c.key == "2")
    assert (two.entity_id, two.entity_type, two.entity_total, two.title) == ("2", "Customer", False, "Example Customer A")
    assert two.leaf_key == "2" and not two.is_period
    unspecified = next(c for c in report.columns if c.key == "not_specified")
    assert unspecified.entity_id is None and unspecified.entity_type == "Customer"
    total = next(c for c in report.columns if c.key == "total")
    assert total.entity_type is None, "the report's own total column is the allowlisted one, not a grouping"
    assert kinds(report) == []
    assert report.completeness == "complete"
    cells = by_key(next(r for r in data_rows(report) if r.row_id == "0.0.0.0"))
    assert cells["8"].amount == "275.00" and cells["8"].currency == "USD"
    assert cells["8"].label == "Example Sub B", "the key alone says nothing; the title rides on the cell"
    assert cells["2"].text is None and cells["2"].amount is None, "an empty cell is not a zero"


def test_a_repeated_entity_key_is_that_entitys_total_column_not_a_structural_failure() -> None:
    """The observed Customers grouping declares key 7 twice: the parent's own
    column, then -- after its sub-customer -- the parent's total. Anywhere
    else a repeated key breaks the cell-to-column mapping and fails the
    request; under an entity grouping the later occurrence is the entity's
    total (the live response titles it 'Total ...' and its cells sum the
    parent and its children; the ORDER, not the title, is what the rule
    reads)."""
    report = flatten(S.profit_and_loss_by_customers(), report_name="ProfitAndLoss",
                     upstream_partial=False, entity_grouping="Customers")
    own = next(c for c in report.columns if c.key == "7")
    total = next(c for c in report.columns if c.key == "7 (total)")
    assert (own.entity_id, own.entity_total) == ("7", False)
    assert (total.entity_id, total.entity_total, total.leaf_key) == ("7", True, "7")
    assert total.title == "Total Example Customer B"
    cells = by_key(next(r for r in data_rows(report) if r.row_id == "0.0.0.0"))
    assert cells["7"].amount is None and cells["7 (total)"].amount == "275.00"
    # The same payload with no grouping named keeps the structural rule.
    with pytest.raises(ReportError, match="repeats a key"):
        flatten(S.profit_and_loss_by_customers(), report_name="ProfitAndLoss", upstream_partial=False)


def test_without_a_grouping_request_integer_keyed_money_columns_stay_excluded() -> None:
    """The control: the shape alone does not admit them. A request that did
    not name an entity grouping has no business receiving columns keyed by
    ids it did not ask for."""
    payload = S.profit_and_loss_by_customers()
    # Drop the repeated-key total column and its cells so only the admission
    # rule is under test, not the structural one.
    del payload["Columns"]["Column"][4]
    for cells in coldata_lists(payload["Rows"]):
        del cells[4]
    report = flatten(payload, report_name="ProfitAndLoss", upstream_partial=False)
    assert set(report.excluded_column_keys) == {"2", "7", "8", "20", "not_specified"}
    assert [c.key for c in report.columns] == ["account", "total"]


def test_a_grouping_column_must_be_money_with_an_id_like_key() -> None:
    payload = S.profit_and_loss_by_customers()
    payload["Columns"]["Column"][1]["ColType"] = "String"          # key 2, not Money
    payload["Columns"]["Column"][5]["MetaData"][0]["Value"] = "twenty"  # key 20 -> not id-like
    report = flatten(payload, report_name="ProfitAndLoss", upstream_partial=False, entity_grouping="Customers")
    assert set(report.excluded_column_keys) == {"2", "twenty"}


def test_a_grouping_on_a_report_that_declares_no_summarised_columns_is_not_admitted() -> None:
    """Per report, like the period rule: GeneralLedger's shape declares none."""
    payload = S.profit_and_loss_by_customers()
    del payload["Columns"]["Column"][4]
    for cells in coldata_lists(payload["Rows"]):
        del cells[4]
    report = flatten(payload, report_name="GeneralLedger", upstream_partial=False, entity_grouping="Customers")
    assert "2" in report.excluded_column_keys


def test_an_unpopulated_grouping_is_not_specified_and_total_and_no_anomaly() -> None:
    """ProfitAndLoss by Classes: the feature is off on the sandbox company, so
    the grouping yields not_specified and total only. Disclosed by the columns
    themselves, not an anomaly."""
    report = flatten(S.profit_and_loss_by_classes_unpopulated(), report_name="ProfitAndLoss",
                     upstream_partial=False, entity_grouping="Classes")
    assert [c.key for c in report.columns] == ["account", "not_specified", "total"]
    unspecified = report.columns[1]
    assert (unspecified.entity_type, unspecified.entity_id) == ("Class", None)
    assert kinds(report) == [] and report.excluded_column_keys == []


@pytest.mark.parametrize(
    ("grouping", "entity_type"),
    [("Customers", "Customer"), ("Vendors", "Vendor"), ("Classes", "Class"),
     ("Departments", "Department"), ("Employees", "Employee"), ("ProductsAndServices", "Item")],
)
def test_each_grouping_names_the_entity_type_its_columns_route_to(grouping: str, entity_type: str) -> None:
    """Customers route to list_customers, Vendors to list_vendors, Classes,
    Departments and Items to list_reference_data; Employee is served nowhere
    and is named so a reader knows the id has no route here."""
    assert R.ENTITY_GROUPINGS[grouping] == entity_type
    payload = S.profit_and_loss_by_classes_unpopulated()
    payload["Header"]["SummarizeColumnsBy"] = grouping
    report = flatten(payload, report_name="ProfitAndLoss", upstream_partial=False, entity_grouping=grouping)
    assert report.columns[1].entity_type == entity_type


def test_a_period_grouping_admits_no_entity_columns() -> None:
    """Month is a period grouping, not an entity one: an integer-keyed Money
    column under it is still excluded."""
    payload = S.profit_and_loss_by_classes_unpopulated()
    payload["Header"]["SummarizeColumnsBy"] = "Month"
    payload["Columns"]["Column"][1]["MetaData"][0]["Value"] = "3"
    report = flatten(payload, report_name="ProfitAndLoss", upstream_partial=False, entity_grouping="Month")
    assert "3" in report.excluded_column_keys


# ==========================================================================
# Fault 5020 is a fixed, actionable error on every report endpoint
# ==========================================================================


def test_fault_5020_has_its_own_hint_distinct_from_5030_and_6190() -> None:
    from qbo_multicompany_mcp.client import FAULT_HINTS

    assert "5020" in FAULT_HINTS
    hint = FAULT_HINTS["5020"]
    assert hint not in (FAULT_HINTS["5030"], FAULT_HINTS["6190"])
    assert "sign in" in hint and "administrator" in hint
    assert "empty" in hint, "the hint says this is not an empty report"


def test_every_report_endpoint_is_labelled_as_a_report_in_errors() -> None:
    """A masked endpoint class, never a URL: the 5020 error from AgedPayables
    reads endpoint=accounting/reports, the same as TransactionList's."""
    from qbo_multicompany_mcp.client import endpoint_label

    for path in R.REPORT_ENDPOINTS.values():
        assert endpoint_label(f"reports/{path}") == "accounting/reports"
    assert endpoint_label("reports/TransactionList") == "accounting/reports"
    assert endpoint_label("query") == "accounting/query"


# ==========================================================================
# Endpoint names differ from page titles; the map is code, not prose
# ==========================================================================

#: Every ``reports/<Path>`` this build may address, as each of Intuit's pages
#: names its own endpoint: the seven the named tools serve, then run_report's
#: twenty-one. Twenty-seven of the twenty-eight answered 200 live on 2026-09-24;
#: TaxSummary was refused with fault 5020.
OBSERVED_PATHS = frozenset({
    "ProfitAndLoss", "BalanceSheet", "GeneralLedger",
    "AgedPayables", "AgedReceivables", "AgedPayableDetail", "AgedReceivableDetail",
    "AccountList", "CashFlow", "CustomerBalance", "CustomerBalanceDetail", "CustomerIncome",
    "InventoryValuationDetail", "InventoryValuationSummary", "JournalReport", "ProfitAndLossDetail",
    "ClassSales", "CustomerSales", "DepartmentSales", "ItemSales", "TaxSummary",
    "TransactionListByCustomer", "TransactionListByVendor", "TransactionListWithSplits", "TrialBalance",
    "VendorBalance", "VendorBalanceDetail", "VendorExpenses",
})


def test_every_tool_facing_report_name_resolves_to_a_documented_path() -> None:
    assert set(R.REPORT_ENDPOINTS) == {
        "ProfitAndLoss", "BalanceSheet", "GeneralLedger",
        "APAgingSummary", "ARAgingSummary", "APAgingDetail", "ARAgingDetail",
        "AccountListDetail", "CashFlow", "CustomerBalance", "CustomerBalanceDetail", "CustomerIncome",
        "InventoryValuationDetail", "InventoryValuationSummary", "JournalReport", "ProfitAndLossDetail",
        "SalesByClassSummary", "SalesByCustomer", "SalesByDepartment", "SalesByProduct", "TaxSummary",
        "TransactionListByCustomer", "TransactionListByVendor", "TransactionListWithSplits", "TrialBalance",
        "VendorBalance", "VendorBalanceDetail", "VendorExpenses",
    }
    assert set(R.REPORT_ENDPOINTS.values()) == OBSERVED_PATHS
    assert R.REPORT_ENDPOINTS["APAgingSummary"] == "AgedPayables"
    assert R.REPORT_ENDPOINTS["ARAgingDetail"] == "AgedReceivableDetail"
    assert R.REPORT_ENDPOINTS["AccountListDetail"] == "AccountList"
    assert R.REPORT_ENDPOINTS["SalesByProduct"] == "ItemSales"
    assert len(R.REPORT_ENDPOINTS) == 28, "the 29 documented reports less TransactionList, which list_transactions serves"


def test_the_typo_in_intuits_sample_request_is_not_propagated() -> None:
    """The sample request on Intuit's CustomerBalanceDetail page spells the
    path 'CustomerBalancedDetail'; nothing here does."""
    assert "CustomerBalancedDetail" not in R.REPORT_ENDPOINTS.values()
    from qbo_multicompany_mcp.netpolicy import ACCOUNTING_PATH_TAILS

    assert "reports/CustomerBalancedDetail" not in ACCOUNTING_PATH_TAILS


def test_every_mapped_path_is_on_the_network_allowlist_by_exact_name() -> None:
    from qbo_multicompany_mcp.netpolicy import ACCOUNTING_PATH_TAILS, ACCOUNTING_QUERY_KEYS_BY_TAIL

    for path in R.REPORT_ENDPOINTS.values():
        assert f"reports/{path}" in ACCOUNTING_PATH_TAILS
        assert f"reports/{path}" in ACCOUNTING_QUERY_KEYS_BY_TAIL
    assert not any(tail.endswith("/") or "*" in tail for tail in ACCOUNTING_PATH_TAILS)


# ==========================================================================
# The shape fixtures every enabled report needs
# ==========================================================================

from .fixtures import report_shapes_run_report as Q  # noqa: E402

ENDPOINT_FIXTURES: dict[str, Callable[[], dict[str, Any]]] = {
    "ProfitAndLoss": S.profit_and_loss_by_month,
    "BalanceSheet": S.balance_sheet_by_quarter,
    "GeneralLedger": S.general_ledger,
    "AgedPayables": S.aged_payables,
    "AgedReceivables": S.aged_receivables,
    "AgedPayableDetail": S.aged_payable_detail,
    "AgedReceivableDetail": S.aged_receivable_detail,
    # Every report run_report serves (structure observed in sandbox on
    # 2026-09-24; TaxSummary from Intuit's page, never observed live).
    "AccountList": Q.account_list,
    "CashFlow": Q.cash_flow,
    "CustomerBalance": Q.customer_balance,
    "CustomerBalanceDetail": Q.customer_balance_detail,
    "CustomerIncome": Q.customer_income,
    "InventoryValuationDetail": S.inventory_valuation_detail_with_rows,
    "InventoryValuationSummary": Q.inventory_valuation_summary,
    "JournalReport": Q.journal_report,
    "ProfitAndLossDetail": Q.profit_and_loss_detail,
    "ClassSales": Q.class_sales,
    "CustomerSales": Q.customer_sales,
    "DepartmentSales": Q.department_sales,
    "ItemSales": S.item_sales,
    "TaxSummary": Q.tax_summary,
    "TransactionListByCustomer": Q.transaction_list_by_customer,
    "TransactionListByVendor": Q.transaction_list_by_vendor,
    "TransactionListWithSplits": Q.transaction_list_with_splits,
    "TrialBalance": S.trial_balance,
    "VendorBalance": Q.vendor_balance,
    "VendorBalanceDetail": Q.vendor_balance_detail,
    "VendorExpenses": Q.vendor_expenses,
}
ENDPOINTS = sorted(ENDPOINT_FIXTURES)


def coldata_lists(node: Any) -> Iterator[list[dict[str, Any]]]:
    """Every ColData list in a payload, whatever its depth."""
    if isinstance(node, dict):
        if "ColData" in node:
            yield node["ColData"]
        for key in ("Header", "Summary", "Rows"):
            if key in node:
                yield from coldata_lists(node[key])
        if "Row" in node:
            for row in node["Row"]:
                yield from coldata_lists(row)


def first_money_column(payload) -> int:
    return next(i for i, c in enumerate(R.parse_columns(payload)) if c.is_money)


def first_data(payload) -> dict[str, Any]:
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "Data" or ("ColData" in node and "type" not in node):
                return node
            for key in ("Rows",):
                if key in node:
                    found = walk(node[key])
                    if found:
                        return found
            if "Row" in node:
                for row in node["Row"]:
                    found = walk(row)
                    if found:
                        return found
        return None
    return walk(payload["Rows"])


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_the_known_shape_of_every_served_report_produces_no_anomaly(endpoint: str) -> None:
    """The live shape must not, by itself, mark a report partial: a tool whose
    every result says PARTIAL teaches its reader to ignore the word."""
    payload = ENDPOINT_FIXTURES[endpoint]()
    report = flatten(payload, report_name=endpoint, upstream_partial=False)
    assert kinds(report) == []
    assert report.completeness == "complete"
    assert report.excluded_column_keys == []
    width = len(report.columns)
    assert all(len(row.cells) == width for row in report.rows), "an observed row was withheld"
    assert report.report_basis == payload["Header"].get("ReportBasis")


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_shape_deep_nesting_keeps_parent_ids_and_scopes_at_every_depth(endpoint: str) -> None:
    payload = ENDPOINT_FIXTURES[endpoint]()
    width = len(R.parse_columns(payload))
    blank = [S.cell("")] * width
    inner = payload["Rows"]["Row"]
    for depth in range(3):
        inner = [S.section(f"Wrap{depth}", header_cells=[S.cell(f"Wrap {depth}"), *blank[1:]], rows=inner,
                           summary_cells=[S.cell(f"Total Wrap {depth}"), *blank[1:]])]
    payload["Rows"]["Row"] = inner
    report = flatten(payload, report_name=endpoint, upstream_partial=False)
    by_id = {r.row_id: r for r in report.rows}
    assert by_id["0.0.0.header"].parent_row_id == "0.0"
    assert by_id["0.0.0.header"].depth == 2
    assert by_id["0.0.0.summary"].scope == "Wrap0"
    deepest = max(report.rows, key=lambda r: r.depth)
    assert deepest.depth >= 3
    parent = by_id.get(deepest.parent_row_id + ".header") or by_id.get(deepest.parent_row_id + ".summary")
    assert parent is not None, "the deepest row's parent is not in the result"
    assert kinds(report) == []


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_shape_repeated_labels_stay_distinct_rows_with_distinct_scopes(endpoint: str) -> None:
    payload = ENDPOINT_FIXTURES[endpoint]()
    first = payload["Rows"]["Row"][0]
    payload["Rows"]["Row"] = [first, copy.deepcopy(first), copy.deepcopy(first)]
    report = flatten(payload, report_name=endpoint, upstream_partial=False)
    ids = [r.row_id for r in report.rows]
    assert len(ids) == len(set(ids))
    summaries = [r for r in report.rows if r.row_type == R.ROW_SECTION_SUMMARY and r.depth == 0]
    if summaries:
        assert len({s.row_id for s in summaries}) == len(summaries)
    top = [r for r in report.rows if r.depth == 0]
    assert len(top) == 3 * len([r for r in flatten({**payload, "Rows": {"Row": [first]}}, report_name=endpoint, upstream_partial=False).rows if r.depth == 0])


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_shape_numeric_looking_text_is_never_inferred(endpoint: str) -> None:
    payload = ENDPOINT_FIXTURES[endpoint]()
    row = first_data(payload)
    text_index = next(i for i, c in enumerate(R.parse_columns(payload, report_name=endpoint)) if not c.is_money)
    row["ColData"][text_index]["value"] = "00123"
    report = flatten(payload, report_name=endpoint, upstream_partial=False)
    key = R.parse_columns(payload, report_name=endpoint)[text_index].key
    published = [c for r in data_rows(report) for c in r.cells if c.column_key == key and c.text == "00123"]
    assert published and published[0].amount is None


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_shape_an_unknown_column_is_excluded_and_named_without_withholding_rows(endpoint: str) -> None:
    payload = ENDPOINT_FIXTURES[endpoint]()
    payload["Columns"]["Column"].append(S.col("brand_new_key", "String", "Mystery"))
    for cells in coldata_lists(payload["Rows"]):
        cells.append(S.cell("PRIVATE-CANARY"))
    report = flatten(payload, report_name=endpoint, upstream_partial=False)
    assert report.excluded_column_keys == ["brand_new_key"]
    assert kinds(report) == []
    assert "PRIVATE-CANARY" not in repr(report.rows)
    assert all(len(r.cells) == len(report.columns) for r in report.rows)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_shape_an_empty_section_yields_its_header_and_total_and_no_data(endpoint: str) -> None:
    payload = ENDPOINT_FIXTURES[endpoint]()
    width = len(R.parse_columns(payload))
    blank = [S.cell("")] * (width - 1)
    payload["Rows"]["Row"].insert(0, S.section("Empty", header_cells=[S.cell("Nothing here"), *blank], rows=[],
                                                  summary_cells=[S.cell("Total Nothing"), *blank]))
    report = flatten(payload, report_name=endpoint, upstream_partial=False)
    by_id = {r.row_id: r for r in report.rows}
    assert by_id["0.header"].row_type == R.ROW_SECTION_HEADER
    assert by_id["0.summary"].scope == "Empty"
    assert not any(r.row_id.startswith("0.") and r.row_type == R.ROW_DATA for r in report.rows)
    assert kinds(report) == []


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_shape_a_malformed_value_is_disclosed_and_the_stated_total_flagged(endpoint: str) -> None:
    payload = ENDPOINT_FIXTURES[endpoint]()
    money = first_money_column(payload)
    row = first_data(payload)
    row["ColData"][money]["value"] = "n/a"
    report = flatten(payload, report_name=endpoint, upstream_partial=False)
    key = R.parse_columns(payload, report_name=endpoint)[money].key
    malformed = [(r, k) for r, k, kind in kinds(report) if kind == "malformed_amount"]
    assert malformed == [(malformed[0][0], key)]
    assert report.completeness == "partial"
    affected_row = malformed[0][0]
    by_id = {r.row_id: r for r in report.rows}
    section_total = by_id.get(affected_row.rsplit(".", 1)[0] + ".summary") if "." in affected_row else None
    if section_total is not None and any(c.column_key == key and c.text for c in section_total.cells):
        # Inside a section whose stated total covers this column: that total
        # cannot be reproduced from the returned cells, and is flagged.
        assert any(kind == "stated_total_not_reproducible" for _, _, kind in kinds(report))
    total = R.computed_total(report, key)
    assert total.status == "unknown"


#: Fixtures whose structure has no Summary block at all, checked against the
#: live responses: AccountList's rows are one flat list; ItemSales ends in a
#: GrandTotal DATA row, not a Summary; TransactionListByVendor's sections carry
#: Header and Rows and no Summary; the sample on Intuit's TaxSummary page is
#: one Data row per group.
NO_SUMMARY_ROWS = frozenset({"AccountList", "ItemSales", "TaxSummary", "TransactionListByVendor"})

#: Fixtures whose structure carries no routing id: AccountList rows carry
#: none; ClassSales and DepartmentSales on a company with the features off
#: hold one "Not Specified" row without an id; the sample on Intuit's
#: TaxSummary page has none. A company with the features on needs the same
#: check.
NO_ROUTING_IDS = frozenset({"AccountList", "ClassSales", "DepartmentSales", "TaxSummary"})


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_shape_local_trimming_counts_upstream_first(endpoint: str) -> None:
    payload = ENDPOINT_FIXTURES[endpoint]()
    full = flatten(payload, report_name=endpoint, upstream_partial=False)
    trimmed = flatten(payload, report_name=endpoint, upstream_partial=False, max_rows=1)
    assert trimmed.client_truncated is True
    assert len(trimmed.rows) == 1
    assert trimmed.upstream_row_count == full.upstream_row_count
    assert trimmed.upstream_cell_count == full.upstream_cell_count
    assert trimmed.completeness == "partial"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_shape_an_upstream_partial_signal_makes_every_total_unknown(endpoint: str) -> None:
    report = flatten(ENDPOINT_FIXTURES[endpoint](), report_name=endpoint, upstream_partial=True)
    assert report.completeness == "unknown"
    summaries = [r for r in report.rows if r.row_type == R.ROW_SECTION_SUMMARY]
    if endpoint not in NO_SUMMARY_ROWS:
        assert summaries, "the observed structure carries a summary row"
    assert all(s.covers == R.COVERS_UNKNOWN for s in summaries)
    for column in report.columns:
        if column.is_money:
            assert R.computed_total(report, column.key).status == "unknown"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_shape_routing_ids_survive_where_the_report_exposes_them(endpoint: str) -> None:
    """A detail row keeps txn_type and id; a hierarchical row keeps its
    entity id on the label cell; row and parent ids at every depth."""
    report = flatten(ENDPOINT_FIXTURES[endpoint](), report_name=endpoint, upstream_partial=False)
    shape = R.REPORT_SHAPES[endpoint]
    if endpoint in NO_ROUTING_IDS:
        pytest.skip("the observed structure carries no routing id to keep (NO_ROUTING_IDS)")
    if shape.style == R.STYLE_DETAIL:
        typed = [by_key(r)["txn_type"] for r in data_rows(report) if "txn_type" in by_key(r)]
        assert typed and all(c.entity_id for c in typed if c.text not in ("", None) and c.text != "Beginning Balance")
    else:
        labelled = [r.cells[0] for r in data_rows(report) if r.cells and r.group is None]
        assert labelled and all(c.entity_id for c in labelled)
    for row in report.rows:
        if row.parent_row_id is not None:
            assert any(other.row_id.startswith(row.parent_row_id) for other in report.rows)


# ==========================================================================
# Percent columns declared Money (observed 2026-09-25)
# ==========================================================================


def _item_sales_with(percent_text: str, key: str = "PercentSales") -> dict[str, Any]:
    """ItemSales with every row's ``key`` leaf carrying ``percent_text``."""
    payload = S.item_sales()
    index = 1 + [k for k, _title, _dated in S.ITEM_SALES_SUBCOLUMNS].index(key)
    for row in payload["Rows"]["Row"]:
        row["ColData"][index] = S.cell(percent_text)
    return payload


def test_a_money_typed_percent_column_parses_as_an_exact_decimal_with_unit_percent() -> None:
    """Observed 2026-09-25 on both sandbox companies: ItemSales declares
    ``PercentSales`` ("% of Sales") and ``GrossMarginPerc`` ("Gross Margin %")
    as Money, and the cells read ``'30.69 %'`` -- a space before the sign.
    Read as money, each is a malformed_amount and every run is PARTIAL. A
    report that is always partial teaches its reader to ignore PARTIAL, so a
    percent column's cells are read exactly, with ``unit: percent``, no
    currency and no anomaly."""
    report = flatten(_item_sales_with("30.69 %"), report_name="ItemSales", upstream_partial=False)
    cell = by_key(report.rows[0])["total/PercentSales"]
    assert (cell.text, cell.amount, cell.currency, cell.unit) == ("30.69 %", "30.69", None, "percent")
    margin = by_key(report.rows[0])["total/GrossMarginPerc"]
    assert (margin.text, margin.amount, margin.currency, margin.unit) == ("60.00 %", "60.00", None, "percent")
    assert kinds(report) == []
    assert report.completeness == "complete"
    as_json = cell.to_json()
    assert as_json["unit"] == "percent" and "currency" not in as_json


def test_a_money_cell_carries_no_unit_and_keeps_its_currency() -> None:
    report = flatten(S.item_sales(), report_name="ItemSales", upstream_partial=False)
    amount = by_key(report.rows[0])["total/Amount"]
    assert (amount.amount, amount.currency, amount.unit) == ("20.00", "USD", None)
    assert "unit" not in amount.to_json()


def test_an_empty_percent_cell_is_neither_an_amount_nor_an_anomaly() -> None:
    """The live ``Gross Margin %`` cells were empty on every row (COGS not
    tracked): text null, no amount, nothing disclosed -- as for any column."""
    report = flatten(_item_sales_with("", key="GrossMarginPerc"), report_name="ItemSales", upstream_partial=False)
    margin = by_key(report.rows[0])["total/GrossMarginPerc"]
    assert (margin.text, margin.amount, margin.unit) == (None, None, None)
    assert kinds(report) == []


@pytest.mark.parametrize(
    ("key", "title", "percent"),
    [
        ("PercentSales", "% of Sales", True),
        ("GrossMarginPerc", "Gross Margin %", True),
        ("Share", "Share %", True),
        ("MarginPerc", "Margin", True),
        ("Amount", "Amount", False),
        ("Percentile", "Rank", False),
        ("PercentSalesTotal", "Total", False),
    ],
)
def test_a_percent_column_is_money_typed_with_a_perc_or_percentsales_key_or_a_sign_in_its_title(
    key: str, title: str, percent: bool
) -> None:
    money = R.ReportColumn(index=0, key=f"total/{key}", title=title, type="Money", leaf_key=key)
    assert money.is_percent is percent, (key, title)
    text = R.ReportColumn(index=0, key=f"total/{key}", title=title, type="String", leaf_key=key)
    assert text.is_percent is False, "only a Money-typed column can be a percent column"


@pytest.mark.parametrize(
    ("text", "exact"),
    [("30.69 %", "30.69"), ("30.69%", "30.69"), ("100.00 %", "100.00"), ("-5.00 %", "-5.00"),
     ("0.00 %", "0.00"), ("1,234.50 %", "1234.50"), ("(12.5) %", "-12.5")],
)
def test_the_percent_shapes_are_read_exactly(text: str, exact: str) -> None:
    report = flatten(_item_sales_with(text), report_name="ItemSales", upstream_partial=False)
    cell = by_key(report.rows[0])["total/PercentSales"]
    assert (cell.text, cell.amount, cell.unit, cell.currency) == (text, exact, "percent", None)
    assert kinds(report) == []


@pytest.mark.parametrize("text", ["30.69", "abc %", "30.69 %%", "%", "30,6 %", "1e2 %", "% 30.69", "NaN %"])
def test_any_other_text_in_a_percent_column_stays_a_malformed_amount(text: str) -> None:
    """The rule is exact: a decimal followed by the sign. A bare
    number in a percent column is not silently read as a percent, and a
    non-decimal before the sign is not read at all; both are disclosed."""
    report = flatten(_item_sales_with(text), report_name="ItemSales", upstream_partial=False)
    cell = by_key(report.rows[0])["total/PercentSales"]
    assert (cell.text, cell.amount, cell.unit, cell.currency) == (text, None, None, None)
    assert ("0", "total/PercentSales", "malformed_amount") in kinds(report)
    assert report.completeness == "partial"


def test_a_percent_sign_in_a_money_column_that_is_not_a_percent_column_stays_an_anomaly() -> None:
    report = flatten(_item_sales_with("30.69 %", key="Amount"), report_name="ItemSales", upstream_partial=False)
    cell = by_key(report.rows[0])["total/Amount"]
    assert (cell.text, cell.amount, cell.unit, cell.currency) == ("30.69 %", None, None, None)
    assert ("0", "total/Amount", "malformed_amount") in kinds(report)
    assert report.completeness == "partial"


def test_a_percent_column_is_never_totalled() -> None:
    """Percentages of a whole do not add up to anything a reader wants; a
    computed total over a percent column is unknown, with the reason."""
    report = flatten(S.item_sales(), report_name="ItemSales", upstream_partial=False)
    total = R.computed_total(report, "total/PercentSales")
    assert (total.status, total.amount) == ("unknown", None)
    assert "percent" in (total.reason or "")
    assert R.computed_total(report, "total/Amount").status == "computed", "the control: a money column still totals"


def test_an_entity_named_with_a_percent_sign_does_not_make_its_grouping_column_a_percent_column() -> None:
    """A grouping column's title is an ENTITY'S NAME -- user data -- not a
    statement about the column's unit (the sixth shape). Read literally, the
    title rule would turn a customer named '10% Club' into a percent column
    and every amount in it into a malformed_amount: a false PARTIAL caused by
    a name. Only the key rule and a title QuickBooks writes identify a
    percent column; an entity's name never does."""
    payload = S.profit_and_loss_by_customers()
    for column in payload["Columns"]["Column"]:
        if column.get("ColTitle") in ("Example Customer B", "Total Example Customer B"):
            column["ColTitle"] = column["ColTitle"].replace("Example Customer B", "Example 10% Club")
    report = flatten(payload, report_name="ProfitAndLoss", upstream_partial=False, entity_grouping="Customers")
    own = next(c for c in report.columns if c.key == "7")
    total = next(c for c in report.columns if c.key == "7 (total)")
    assert own.title == "Example 10% Club" and total.title == "Total Example 10% Club"
    assert own.is_grouping and total.is_grouping
    assert own.is_percent is False and total.is_percent is False
    assert kinds(report) == []
    lighting = by_key(next(r for r in data_rows(report) if r.row_id == "0.0.0.0"))
    cell = lighting["7 (total)"]
    assert (cell.text, cell.amount, cell.currency, cell.unit) == ("275.00", "275.00", "USD", None)
    assert report.completeness == "complete"


def test_the_cell_schema_states_the_percent_rule_as_built() -> None:
    """A client reads the rule from the published schema, so the ``unit``
    field's description must state it as built, grouping exception included;
    a description that omits it misstates when a cell is a percent."""
    from qbo_multicompany_mcp.envelopes import ReportCellBlock

    description = ReportCellBlock.model_fields["unit"].description or ""
    for needle in ("Perc", "PercentSales", "'%'", "grouping", "no currency"):
        assert needle in description, needle
