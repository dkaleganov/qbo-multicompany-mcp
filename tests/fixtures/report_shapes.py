"""Synthetic report payloads in the shapes live sandbox responses showed.

Every builder here mirrors the STRUCTURE of one live sandbox response. Names,
ids and dates are invented, short and obviously fake, and amounts are invented
or come from Intuit's sample company, because a fixture that looked real would
eventually be mistaken for live data.

What each shape proves about the parser is in ``tests/test_report_shapes.py``.
"""

from __future__ import annotations

from typing import Any


def col(
    key: str | None,
    ctype: str | None,
    title: str = "",
    *,
    start: str | None = None,
    end: str | None = None,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One column definition as Intuit writes it: ``ColKey`` lives in the
    ``MetaData`` name/value list, period bounds beside it, and a column that
    groups sub-columns carries them under ``Columns.Column``."""
    meta: list[dict[str, str]] = []
    if start is not None:
        meta.append({"Name": "StartDate", "Value": start})
    if end is not None:
        meta.append({"Name": "EndDate", "Value": end})
    if key is not None:
        meta.append({"Name": "ColKey", "Value": key})
    out: dict[str, Any] = {"ColTitle": title, "MetaData": meta}
    if ctype is not None:
        out["ColType"] = ctype
    if children is not None:
        out["Columns"] = {"Column": children}
    return out


def cell(value: str, id_: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"value": value}
    if id_ is not None:
        out["id"] = id_
    return out


def header(
    name: str,
    *,
    start: str | None = "2026-01-01",
    end: str | None = "2026-06-30",
    basis: str | None = "Accrual",
    summarize: str | None = "Total",
    date_macro: str | None = None,
    no_data: bool = False,
    options: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"ReportName": name, "Currency": "USD", "Time": "2026-09-24T00:00:00-07:00"}
    if start is not None:
        out["StartPeriod"] = start
    if end is not None:
        out["EndPeriod"] = end
    if basis is not None:
        out["ReportBasis"] = basis
    if summarize is not None:
        out["SummarizeColumnsBy"] = summarize
    if date_macro is not None:
        out["DateMacro"] = date_macro
    out["Option"] = [*(options or []), {"Name": "NoReportData", "Value": "true" if no_data else "false"}]
    return out


def section(
    group: str | None,
    *,
    header_cells: list[dict[str, Any]] | None,
    rows: list[dict[str, Any]] | None,
    summary_cells: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "Section"}
    if group is not None:
        out["group"] = group
    if header_cells is not None:
        out["Header"] = {"ColData": header_cells}
    if rows is not None:
        out["Rows"] = {"Row": rows}
    if summary_cells is not None:
        out["Summary"] = {"ColData": summary_cells}
    return out


def data(cells: list[dict[str, Any]], group: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "Data", "ColData": cells}
    if group is not None:
        out["group"] = group
    return out


# --------------------------------------------------------------------------
# Nested columns (ItemSales)
# --------------------------------------------------------------------------

ITEM_SALES_SUBCOLUMNS = (
    ("Quantity", "Quantity", True), ("Amount", "Amount", True), ("PercentSales", "% of Sales", False),
    ("AvgPrice", "Avg Price", False), ("Cogs", "COGS", True), ("GrossMargin", "Gross Margin", False),
    ("GrossMarginPerc", "Gross Margin %", False),
)


def item_sales() -> dict[str, Any]:
    """Two top-level columns; the second, ``total``, groups seven sub-columns,
    three of them with period metadata. Rows carry EIGHT cells, one per leaf.
    The last row is a ``GrandTotal`` data row with no id. The two percent
    columns (``PercentSales`` "% of Sales", ``GrossMarginPerc`` "Gross
    Margin %") are declared Money and their cells carry the live shape, a
    decimal, a space and the sign (``'30.69 %'``, observed 2026-09-25 on both
    sandbox companies)."""
    children = [
        col(key, "Money", title, start="2026-01-01" if dated else None, end="2026-06-30" if dated else None)
        for key, title, dated in ITEM_SALES_SUBCOLUMNS
    ]
    columns = [col(None, "ProductsAndService", ""), col("total", "Money", "Total", children=children)]
    eight = lambda name, id_: [cell(name, id_), cell("2"), cell("20.00"), cell("50.00 %"), cell("10.00"), cell("8.00"), cell("12.00"), cell("60.00 %")]  # noqa: E731
    return {
        "Header": header("ItemSales"),
        "Columns": {"Column": columns},
        "Rows": {"Row": [
            data(eight("Example Item A", "1")),
            data(eight("Example Item B", "2")),
            data([cell("TOTAL"), cell("4"), cell("40.00"), cell("100.00 %"), cell(""), cell("16.00"), cell("24.00"), cell("60.00 %")], group="GrandTotal"),
        ]},
    }


# --------------------------------------------------------------------------
# Period columns with dynamic keys
#     ProfitAndLoss by Month, BalanceSheet by Quarter
# --------------------------------------------------------------------------

PNL_MONTHS = (("Jan 2026", "2026-01-01", "2026-01-31"), ("Feb 2026", "2026-02-01", "2026-02-28"), ("Mar 2026", "2026-03-01", "2026-03-31"))


def _pnl_columns(periods) -> list[dict[str, Any]]:
    return [
        col("account", "Account", ""),
        *[col(label, "Money", label, start=s, end=e) for label, s, e in periods],
        col("total", "Money", "Total"),
    ]


def _money_row(label: str, amounts: list[str], id_: str | None = None) -> list[dict[str, Any]]:
    return [cell(label, id_), *[cell(a) for a in amounts]]


def profit_and_loss_by_month() -> dict[str, Any]:
    """Income (Section, three levels deep at its deepest) / GrossProfit
    (Summary only) / Expenses / NetOperatingIncome / NetIncome, as observed."""
    n = len(PNL_MONTHS) + 1
    blank = [cell("")] * n
    return {
        "Header": header("ProfitAndLoss", start="2026-01-01", end="2026-03-31", summarize="Month",
                         options=[{"Name": "AccountingStandard", "Value": "GAAP"}]),
        "Columns": {"Column": _pnl_columns(PNL_MONTHS)},
        "Rows": {"Row": [
            section("Income",
                    header_cells=[cell("Income"), *blank],
                    rows=[
                        section(None,
                                header_cells=_money_row("Example Sales", [""] * n, "10"),
                                rows=[
                                    section(None,
                                            header_cells=_money_row("Example Sales Sub", [""] * n, "11"),
                                            rows=[data(_money_row("Example Product Income", ["10.00", "20.00", "30.00", "60.00"], "12"))],
                                            summary_cells=_money_row("Total Example Sales Sub", ["10.00", "20.00", "30.00", "60.00"])),
                                ],
                                summary_cells=_money_row("Total Example Sales", ["10.00", "20.00", "30.00", "60.00"])),
                        data(_money_row("Example Services", ["1.00", "2.00", "3.00", "6.00"], "13")),
                    ],
                    summary_cells=_money_row("Total Income", ["11.00", "22.00", "33.00", "66.00"])),
            section("GrossProfit", header_cells=None, rows=None,
                    summary_cells=_money_row("Gross Profit", ["11.00", "22.00", "33.00", "66.00"])),
            section("Expenses",
                    header_cells=[cell("Expenses"), *blank],
                    rows=[data(_money_row("Example Rent", ["5.00", "5.00", "5.00", "15.00"], "20"))],
                    summary_cells=_money_row("Total Expenses", ["5.00", "5.00", "5.00", "15.00"])),
            section("NetOperatingIncome", header_cells=None, rows=None,
                    summary_cells=_money_row("Net Operating Income", ["6.00", "17.00", "28.00", "51.00"])),
            section("NetIncome", header_cells=None, rows=None,
                    summary_cells=_money_row("Net Income", ["6.00", "17.00", "28.00", "51.00"])),
        ]},
    }


# --------------------------------------------------------------------------
# Entity groupings of summarize_column_by (ProfitAndLoss by Customers)
# --------------------------------------------------------------------------

#: The observed column keys, in the observed order: the account label, then
#: one Money column PER ENTITY keyed by the entity's id with its name as the
#: title, a sub-customer nested after its parent, the parent's key REPEATED
#: for its total, then `not_specified` and `total`. No period metadata on any
#: of them. Names and amounts here are synthetic.
PNL_CUSTOMER_COLUMNS = (
    ("2", "Example Customer A"), ("7", "Example Customer B"), ("8", "Example Sub B"),
    ("7", "Total Example Customer B"), ("20", "Example Customer C"),
    ("not_specified", "Not Specified"), ("total", "TOTAL"),
)


def _grouped_columns(pairs) -> list[dict[str, Any]]:
    return [col("account", "Account", ""), *[col(key, "Money", title) for key, title in pairs]]


def profit_and_loss_by_customers() -> dict[str, Any]:
    """P&L summarised by Customers: eight columns, the observed section tree
    (Income > Example Sales > Example Sales Sub > data), a customer's own
    column, its sub-customer's, and its repeated-key total column."""
    n = len(PNL_CUSTOMER_COLUMNS)
    blank = [cell("")] * n
    return {
        "Header": header("ProfitAndLoss", start="2026-01-01", end="2026-06-30", summarize="Customers",
                         options=[{"Name": "AccountingStandard", "Value": "GAAP"}]),
        "Columns": {"Column": _grouped_columns(PNL_CUSTOMER_COLUMNS)},
        "Rows": {"Row": [
            section("Income",
                    header_cells=[cell("Income"), *blank],
                    rows=[
                        section(None,
                                header_cells=_money_row("Example Sales", [""] * n, "45"),
                                rows=[
                                    section(None,
                                            header_cells=_money_row("Example Sales Sub", [""] * n, "46"),
                                            rows=[
                                                data(_money_row("Example Lighting", ["", "", "275.00", "275.00", "48.00", "", "323.00"], "48")),
                                                data(_money_row("Example Soil", ["", "", "131.25", "131.25", "", "", "131.25"], "49")),
                                            ],
                                            summary_cells=_money_row("Total Example Sales Sub", ["0.00", "0.00", "406.25", "406.25", "48.00", "0.00", "454.25"])),
                                ],
                                summary_cells=_money_row("Total Example Sales", ["0.00", "0.00", "406.25", "406.25", "48.00", "0.00", "454.25"])),
                        data(_money_row("Example Pest Control", ["35.00", "", "35.00", "35.00", "", "", "70.00"], "54")),
                    ],
                    summary_cells=_money_row("Total Income", ["35.00", "0.00", "441.25", "441.25", "48.00", "0.00", "524.25"])),
            section("GrossProfit", header_cells=None, rows=None,
                    summary_cells=_money_row("Gross Profit", ["35.00", "0.00", "441.25", "441.25", "48.00", "0.00", "524.25"])),
            section("Expenses",
                    header_cells=[cell("Expenses"), *blank],
                    rows=[data(_money_row("Example Accounting", ["", "", "", "", "", "250.00", "250.00"], "69"))],
                    summary_cells=_money_row("Total Expenses", ["0.00", "0.00", "0.00", "0.00", "0.00", "250.00", "250.00"])),
            section("NetOperatingIncome", header_cells=None, rows=None,
                    summary_cells=_money_row("Net Operating Income", ["35.00", "0.00", "441.25", "441.25", "48.00", "-250.00", "274.25"])),
            section("NetIncome", header_cells=None, rows=None,
                    summary_cells=_money_row("Net Income", ["35.00", "0.00", "441.25", "441.25", "48.00", "-250.00", "274.25"])),
        ]},
    }


def profit_and_loss_by_classes_unpopulated() -> dict[str, Any]:
    """P&L summarised by Classes on a company with class tracking off: only
    `not_specified` and `total` beside the label."""
    return {
        "Header": header("ProfitAndLoss", start="2026-01-01", end="2026-06-30", summarize="Classes",
                         options=[{"Name": "AccountingStandard", "Value": "GAAP"}]),
        "Columns": {"Column": _grouped_columns((("not_specified", "Not Specified"), ("total", "TOTAL")))},
        "Rows": {"Row": [
            section("Income",
                    header_cells=[cell("Income"), cell(""), cell("")],
                    rows=[data(_money_row("Example Lighting", ["323.00", "323.00"], "48"))],
                    summary_cells=_money_row("Total Income", ["323.00", "323.00"])),
            section("NetIncome", header_cells=None, rows=None,
                    summary_cells=_money_row("Net Income", ["323.00", "323.00"])),
        ]},
    }


BS_QUARTERS = (("Jan - Mar, 2026", "2026-01-01", "2026-03-31"), ("Apr - Jun, 2026", "2026-04-01", "2026-06-30"))


def balance_sheet_by_quarter() -> dict[str, Any]:
    """Four levels deep (TotalLiabilitiesAndEquity > Liabilities >
    CurrentLiabilities > AP > data), period columns keyed by quarter label, and
    a ``NetIncome`` DATA row (not a section) inside Equity -- all as observed."""
    n = len(BS_QUARTERS)
    blank = [cell("")] * n
    r = lambda label, amounts, id_=None: _money_row(label, amounts, id_)  # noqa: E731
    return {
        "Header": header("BalanceSheet", start="2026-01-01", end="2026-06-30", summarize="Quarter",
                         date_macro="this calendar year-to-date",
                         options=[{"Name": "AccountingStandard", "Value": "GAAP"}]),
        "Columns": {"Column": [col("account", "Account", ""), *[col(l, "Money", l, start=s, end=e) for l, s, e in BS_QUARTERS]]},
        "Rows": {"Row": [
            section("TotalAssets", header_cells=[cell("ASSETS"), *blank],
                    rows=[
                        section("CurrentAssets", header_cells=[cell("Current Assets"), *blank],
                                rows=[
                                    section("BankAccounts", header_cells=[cell("Bank Accounts"), *blank],
                                            rows=[data(r("Example Checking", ["100.00", "110.00"], "1"))],
                                            summary_cells=r("Total Bank Accounts", ["100.00", "110.00"])),
                                    section("AR", header_cells=[cell("Accounts Receivable"), *blank],
                                            rows=[data(r("Example Receivable", ["20.00", "25.00"], "2"))],
                                            summary_cells=r("Total Accounts Receivable", ["20.00", "25.00"])),
                                ],
                                summary_cells=r("Total Current Assets", ["120.00", "135.00"])),
                    ],
                    summary_cells=r("TOTAL ASSETS", ["120.00", "135.00"])),
            section("TotalLiabilitiesAndEquity", header_cells=[cell("LIABILITIES AND EQUITY"), *blank],
                    rows=[
                        section("Liabilities", header_cells=[cell("Liabilities"), *blank],
                                rows=[
                                    section("CurrentLiabilities", header_cells=[cell("Current Liabilities"), *blank],
                                            rows=[
                                                section("AP", header_cells=[cell("Accounts Payable"), *blank],
                                                        rows=[data(r("Example Payable", ["30.00", "35.00"], "3"))],
                                                        summary_cells=r("Total Accounts Payable", ["30.00", "35.00"])),
                                            ],
                                            summary_cells=r("Total Current Liabilities", ["30.00", "35.00"])),
                                ],
                                summary_cells=r("Total Liabilities", ["30.00", "35.00"])),
                        section("Equity", header_cells=[cell("Equity"), *blank],
                                rows=[
                                    data(r("Example Opening Balance", ["50.00", "50.00"], "4")),
                                    data(r("Net Income", ["40.00", "50.00"]), group="NetIncome"),
                                ],
                                summary_cells=r("Total Equity", ["90.00", "100.00"])),
                    ],
                    summary_cells=r("TOTAL LIABILITIES AND EQUITY", ["120.00", "135.00"])),
        ]},
    }


# --------------------------------------------------------------------------
# Keyless label column, aging buckets
#     AgedPayables, AgedReceivables
# --------------------------------------------------------------------------

AGING_BUCKETS = (("current", "Current"), ("0", "1 - 30"), ("1", "31 - 60"), ("2", "61 - 90"), ("3", "91 and over"), ("total", "Total"))


def _aging_columns(label_type: str) -> list[dict[str, Any]]:
    return [col(None, label_type, ""), *[col(key, "Money", title) for key, title in AGING_BUCKETS]]


def _aging_row(name: str, id_: str | None, amounts: list[str]) -> list[dict[str, Any]]:
    return [cell(name, id_), *[cell(a) for a in amounts]]


def aged_payables() -> dict[str, Any]:
    """A keyless ``Vendor`` label column, six bucket columns keyed
    current/0/1/2/3/total with the human label in the title, flat data rows
    carrying the vendor id in cell 0, and a GrandTotal section with a Summary
    only."""
    return {
        "Header": header("AgedPayables", start="2026-09-24", end="2026-09-24", basis=None, date_macro="today",
                         options=[{"Name": "report_date", "Value": "2026-09-24"}]),
        "Columns": {"Column": _aging_columns("Vendor")},
        "Rows": {"Row": [
            data(_aging_row("Example Vendor A", "1", ["10.00", "", "5.00", "", "", "15.00"])),
            data(_aging_row("Example Vendor B", "2", ["", "7.00", "", "", "", "7.00"])),
            section("GrandTotal", header_cells=None, rows=None,
                    summary_cells=_aging_row("TOTAL", None, ["10.00", "7.00", "5.00", "0.00", "0.00", "22.00"])),
        ]},
    }


def aged_receivables() -> dict[str, Any]:
    """As payables, with a ``Customer`` label and one nested section: a parent
    customer whose Header carries the id in cell 0 and whose sub-customers are
    data rows beneath it -- the observed AR shape."""
    return {
        "Header": header("AgedReceivables", start="2026-09-24", end="2026-09-24", basis=None, date_macro="today",
                         options=[{"Name": "report_date", "Value": "2026-09-24"}]),
        "Columns": {"Column": _aging_columns("Customer")},
        "Rows": {"Row": [
            data(_aging_row("Example Customer A", "1", ["3.00", "", "", "", "", "3.00"])),
            section(None,
                    header_cells=_aging_row("Example Customer B", "2", ["", "", "", "", "", ""]),
                    rows=[data(_aging_row("Example Customer B:Job 1", "3", ["", "4.00", "", "", "", "4.00"]))],
                    summary_cells=_aging_row("Total Example Customer B", None, ["0.00", "4.00", "0.00", "0.00", "0.00", "4.00"])),
            section("GrandTotal", header_cells=None, rows=None,
                    summary_cells=_aging_row("TOTAL", None, ["3.00", "4.00", "0.00", "0.00", "0.00", "7.00"])),
        ]},
    }


def trial_balance() -> dict[str, Any]:
    """TrialBalance: no ColKey on ANY column. Structure of a live sandbox
    response, with every value synthetic."""
    return {
        "Header": header("TrialBalance", start="2026-01-01", end="2026-06-30", date_macro="this calendar year-to-date"),
        "Columns": {"Column": [col(None, "Account", ""), col(None, "Money", "Debit"), col(None, "Money", "Credit")]},
        "Rows": {"Row": [
            data([cell("Example Checking", "1"), cell("100.00"), cell("")]),
            data([cell("Example Payable", "3"), cell(""), cell("100.00")]),
            # The live response ends in a GrandTotal Section carrying only a Summary.
            section("GrandTotal", header_cells=None, rows=None,
                    summary_cells=[cell("TOTAL"), cell("100.00"), cell("100.00")]),
        ]},
    }


# --------------------------------------------------------------------------
# Absent ColType on an empty report; the Quantity type
#     InventoryValuationDetail, empty and with rows
# --------------------------------------------------------------------------

IVD_COLUMNS = (
    ("tx_date", "Date", "Date"), ("txn_type", "String", "Transaction Type"), ("doc_num", "String", "Num"),
    ("name", "String", "Name"), ("quantity", "Quantity", "Qty"), ("rate", "Money", "Rate"),
    ("home_amount", "Money", "Inventory Cost"), ("qty_on_hand", "String", "Qty On Hand"),
    ("home_asset_value", "Money", "Asset Value"),
)


def inventory_valuation_detail_empty() -> dict[str, Any]:
    """Nine columns, none with a ColType, ``Rows`` an empty object, and
    ``NoReportData`` true: the shape an empty window returned live."""
    return {
        "Header": header("InventoryValuationDetail", basis=None, summarize=None, no_data=True),
        "Columns": {"Column": [col(key, None, title) for key, _, title in IVD_COLUMNS]},
        "Rows": {},
    }


def inventory_valuation_detail_with_rows() -> dict[str, Any]:
    """The same nine columns fully typed, ``quantity`` as ``Quantity``, one
    section per item with the item name in the header and ids on the
    transaction-type and name cells."""
    row = lambda: data([cell("2026-02-01"), cell("Invoice", "7"), cell("00123"), cell("Example Customer", "9"), cell("-3"), cell("2.50"), cell("-7.50"), cell("12"), cell("30.00")])  # noqa: E731
    blank = [cell("")] * (len(IVD_COLUMNS) - 1)
    return {
        "Header": header("InventoryValuationDetail", basis=None, summarize=None),
        "Columns": {"Column": [col(key, ctype, title) for key, ctype, title in IVD_COLUMNS]},
        "Rows": {"Row": [
            section(None, header_cells=[cell("Example Item A"), *blank], rows=[row(), row()],
                    summary_cells=[cell("Total for Example Item A"), *blank]),
        ]},
    }


# --------------------------------------------------------------------------
# The detail reports the four tools serve
#     GeneralLedger, AgedPayableDetail, AgedReceivableDetail
# --------------------------------------------------------------------------

GL_COLUMNS = (
    ("tx_date", "Date", "Date"), ("txn_type", "String", "Transaction Type"), ("doc_num", "String", "Num"),
    ("name", "String", "Name"), ("memo", "String", "Memo/Description"), ("split_acc", "String", "Split"),
    ("subt_nat_amount", "Money", "Amount"), ("rbal_nat_amount", "Money", "Balance"),
)


def gl_data(date: str, txn_type: str, txn_id: str, doc: str, amount: str, balance: str) -> dict[str, Any]:
    """A GeneralLedger data row: ids on the transaction type, the name and the
    split account, as observed (id positions 1, 3, 5)."""
    return data([cell(date), cell(txn_type, txn_id), cell(doc), cell("Example Name", "5"), cell("memo text"),
                 cell("Example Split", "6"), cell(amount), cell(balance)])


def general_ledger() -> dict[str, Any]:
    """One section per account, the account name and id in the header's first
    cell, a beginning-balance line, data rows, a total; and one account whose
    sub-account section has Rows and a Summary but NO Header, as observed."""
    blank = [cell("")] * (len(GL_COLUMNS) - 1)
    total = lambda label, amount: [cell(label), cell(""), cell(""), cell(""), cell(""), cell(""), cell(amount), cell(amount)]  # noqa: E731
    return {
        "Header": header("GeneralLedger", summarize=None),
        "Columns": {"Column": [col(k, t, title) for k, t, title in GL_COLUMNS]},
        "Rows": {"Row": [
            section(None, header_cells=[cell("Example Checking", "1"), *blank],
                    rows=[
                        data([cell("Beginning Balance"), cell(""), cell(""), cell(""), cell(""), cell(""), cell(""), cell("100.00")]),
                        gl_data("2026-02-01", "Deposit", "41", "", "10.00", "110.00"),
                        gl_data("2026-02-02", "Check", "42", "00123", "-4.00", "106.00"),
                    ],
                    summary_cells=total("Total for Example Checking", "6.00")),
            section(None, header_cells=[cell("Example Expense", "2"), *blank],
                    rows=[
                        section(None, header_cells=None,
                                rows=[gl_data("2026-02-02", "Check", "42", "00123", "4.00", "4.00")],
                                summary_cells=total("Total", "4.00")),
                    ],
                    summary_cells=total("Total for Example Expense", "4.00")),
        ]},
    }


AP_DETAIL_COLUMNS = (
    ("tx_date", "Date", "Date"), ("txn_type", "String", "Transaction Type"), ("doc_num", "String", "Num"),
    ("vend_name", "String", "Vendor"), ("due_date", "Date", "Due Date"), ("past_due", "String", "Past Due"),
    ("subt_neg_amount", "Money", "Amount"), ("subt_neg_open_bal", "Money", "Open Balance"),
)
AR_DETAIL_COLUMNS = (
    ("tx_date", "Date", "Date"), ("txn_type", "String", "Transaction Type"), ("doc_num", "String", "Num"),
    ("cust_name", "String", "Customer"), ("due_date", "Date", "Due Date"),
    ("subt_amount", "Money", "Amount"), ("subt_open_bal", "Money", "Open Balance"),
)


def _aging_detail(name: str, columns, bucket_labels: list[str], rows_per_bucket) -> dict[str, Any]:
    n = len(columns)
    blank = [cell("")] * (n - 1)
    total = lambda label, amount: [cell(label), *[cell("")] * (n - 3), cell(amount), cell(amount)]  # noqa: E731
    return {
        "Header": header(name, start="2026-09-24", end="2026-09-24", basis=None, summarize=None, date_macro="today",
                         options=[{"Name": "report_date", "Value": "2026-09-24"}]),
        "Columns": {"Column": [col(k, t, title) for k, t, title in columns]},
        "Rows": {"Row": [
            *[section(None, header_cells=[cell(label), *blank], rows=rows_per_bucket(label),
                      summary_cells=total(f"Total for {label}", "9.00"))
              for label in bucket_labels],
            section(None, header_cells=None, rows=None, summary_cells=total("TOTAL", "18.00")),
        ]},
    }


def aged_payable_detail() -> dict[str, Any]:
    """Sections per bucket, ids on the transaction type and the vendor name
    (positions 1 and 3), a trailing Summary-only section."""
    def rows(label: str) -> list[dict[str, Any]]:
        return [data([cell("2026-08-01"), cell("Bill", "51"), cell("00123"), cell("Example Vendor", "5"),
                      cell("2026-08-31"), cell("24"), cell("-9.00"), cell("-9.00")])]
    return _aging_detail("AgedPayableDetail", AP_DETAIL_COLUMNS, ["Current", "1 - 30"], rows)


def aged_receivable_detail() -> dict[str, Any]:
    def rows(label: str) -> list[dict[str, Any]]:
        return [data([cell("2026-08-01"), cell("Invoice", "61"), cell("00456"), cell("Example Customer", "7"),
                      cell("2026-08-31"), cell("9.00"), cell("9.00")])]
    return _aging_detail("AgedReceivableDetail", AR_DETAIL_COLUMNS, ["Current", "1 - 30"], rows)


# --------------------------------------------------------------------------
# Fault 5020 -- the TaxSummary refusal, observed in sandbox on 2026-09-24:
#     HTTP 400, "Permission Denied Error: To access this, sign in again or
#     contact an administrator." The Message and Detail here are decoys the
#     error text must never echo.
# --------------------------------------------------------------------------


def fault_body(code: str) -> dict[str, Any]:
    return {
        "Fault": {
            "Error": [{"Message": "should-not-appear", "Detail": "should-not-appear", "code": code}],
            "type": "ValidationFault",
        },
        "time": "2026-09-24T00:00:00Z",
    }
