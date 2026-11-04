"""Structure-only fixtures for every report `run_report` serves.

Built from live sandbox responses of 2026-09-24: each
builder keeps the response's STRUCTURE -- column keys, types, titles and
metadata names; the row tree with its Section/Data kinds, group names,
Header/Summary presence, nesting, cell counts and which cells carry an id --
and none of its VALUES: every amount, label, date and id is synthetic, and
each Rows list is trimmed to its first two rows plus its last, so a trailing
GrandTotal survives.

TaxSummary was refused with fault 5020 on the sandbox company, so no live
response exists; its builder is hand-written from the sample on Intuit's page
(a keyless String label, a keyless Money "Total", one Data row per "Case NN"
group) and is labelled documentation-derived. TrialBalance, ItemSales and
InventoryValuationDetail have their builders in ``report_shapes.py``.
"""

from __future__ import annotations

from typing import Any


def account_list() -> dict[str, Any]:
    """reports/AccountList: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "AccountList",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "Account",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "account_name"
                        }
                    ]
                },
                {
                    "ColTitle": "Type",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "account_type"
                        }
                    ]
                },
                {
                    "ColTitle": "Detail type",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "detail_acc_type"
                        }
                    ]
                },
                {
                    "ColTitle": "Description",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "account_desc"
                        }
                    ]
                },
                {
                    "ColTitle": "Balance",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "account_bal"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Data",
                    "ColData": [
                        {
                            "value": "Example Account 1"
                        },
                        {
                            "value": "Example Type 2"
                        },
                        {
                            "value": "Example Detail type 3"
                        },
                        {
                            "value": ""
                        },
                        {
                            "value": "10.00"
                        }
                    ]
                },
                {
                    "type": "Data",
                    "ColData": [
                        {
                            "value": "Example Account 4"
                        },
                        {
                            "value": "Example Type 5"
                        },
                        {
                            "value": "Example Detail type 6"
                        },
                        {
                            "value": ""
                        },
                        {
                            "value": "20.00"
                        }
                    ]
                },
                {
                    "type": "Data",
                    "ColData": [
                        {
                            "value": "Example Account 7"
                        },
                        {
                            "value": "Example Type 8"
                        },
                        {
                            "value": "Example Detail type 9"
                        },
                        {
                            "value": ""
                        },
                        {
                            "value": ""
                        }
                    ]
                }
            ]
        }
    }


def cash_flow() -> dict[str, Any]:
    """reports/CashFlow: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "CashFlow",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "SummarizeColumnsBy": "Total",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "",
                    "ColType": "Account",
                    "MetaData": []
                },
                {
                    "ColTitle": "Total",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "total"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Section",
                    "group": "OperatingActivities",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 1"
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "group": "NetIncome",
                                "ColData": [
                                    {
                                        "value": "Example Label 2"
                                    },
                                    {
                                        "value": "10.00"
                                    }
                                ]
                            },
                            {
                                "type": "Section",
                                "group": "OperatingAdjustments",
                                "Header": {
                                    "ColData": [
                                        {
                                            "value": "Example Section 3"
                                        },
                                        {
                                            "value": ""
                                        }
                                    ]
                                },
                                "Rows": {
                                    "Row": [
                                        {
                                            "type": "Data",
                                            "ColData": [
                                                {
                                                    "value": "Example Label 4",
                                                    "id": "1"
                                                },
                                                {
                                                    "value": "20.00"
                                                }
                                            ]
                                        },
                                        {
                                            "type": "Data",
                                            "ColData": [
                                                {
                                                    "value": "Example Label 5",
                                                    "id": "2"
                                                },
                                                {
                                                    "value": "30.00"
                                                }
                                            ]
                                        },
                                        {
                                            "type": "Data",
                                            "ColData": [
                                                {
                                                    "value": "Example Label 6",
                                                    "id": "3"
                                                },
                                                {
                                                    "value": "40.00"
                                                }
                                            ]
                                        }
                                    ]
                                },
                                "Summary": {
                                    "ColData": [
                                        {
                                            "value": "Total Example 7"
                                        },
                                        {
                                            "value": "50.00"
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Example Section 8"
                            },
                            {
                                "value": "60.00"
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "group": "FinancingActivities",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 9"
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "Example Label 10",
                                        "id": "4"
                                    },
                                    {
                                        "value": "70.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Example Section 11"
                            },
                            {
                                "value": "80.00"
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "group": "EndingCash",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Example Section 12"
                            },
                            {
                                "value": "90.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def customer_balance() -> dict[str, Any]:
    """reports/CustomerBalance: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "CustomerBalance",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "DateMacro": "all",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "",
                    "ColType": "Customer",
                    "MetaData": []
                },
                {
                    "ColTitle": "Total",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "total"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "ColData": [
                        {
                            "value": "Example Label 1",
                            "id": "1"
                        },
                        {
                            "value": "10.00"
                        }
                    ]
                },
                {
                    "ColData": [
                        {
                            "value": "Example Label 2",
                            "id": "2"
                        },
                        {
                            "value": "20.00"
                        }
                    ]
                },
                {
                    "type": "Section",
                    "group": "GrandTotal",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 3"
                            },
                            {
                                "value": "30.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def customer_balance_detail() -> dict[str, Any]:
    """reports/CustomerBalanceDetail: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "CustomerBalanceDetail",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "DateMacro": "all",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "Date",
                    "ColType": "Date",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "tx_date"
                        }
                    ]
                },
                {
                    "ColTitle": "Transaction Type",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "txn_type"
                        }
                    ]
                },
                {
                    "ColTitle": "Num",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "doc_num"
                        }
                    ]
                },
                {
                    "ColTitle": "Due Date",
                    "ColType": "Date",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "due_date"
                        }
                    ]
                },
                {
                    "ColTitle": "Amount",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "subt_amount"
                        }
                    ]
                },
                {
                    "ColTitle": "Open Balance",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "subt_open_bal"
                        }
                    ]
                },
                {
                    "ColTitle": "Balance",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "rbal_open_bal"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 1",
                                "id": "1"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 2",
                                        "id": "2"
                                    },
                                    {
                                        "value": "3"
                                    },
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "10.00"
                                    },
                                    {
                                        "value": "20.00"
                                    },
                                    {
                                        "value": "30.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 3"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "40.00"
                            },
                            {
                                "value": "50.00"
                            },
                            {
                                "value": ""
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 4",
                                "id": "3"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 5",
                                        "id": "4"
                                    },
                                    {
                                        "value": "3"
                                    },
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "60.00"
                                    },
                                    {
                                        "value": "70.00"
                                    },
                                    {
                                        "value": "80.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 6"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "90.00"
                            },
                            {
                                "value": "100.00"
                            },
                            {
                                "value": ""
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 7"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "110.00"
                            },
                            {
                                "value": "120.00"
                            },
                            {
                                "value": ""
                            }
                        ]
                    }
                }
            ]
        }
    }


def customer_income() -> dict[str, Any]:
    """reports/CustomerIncome: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "CustomerIncome",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "ReportBasis": "Accrual",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "",
                    "ColType": "Customer",
                    "MetaData": []
                },
                {
                    "ColTitle": "Income",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "income"
                        }
                    ]
                },
                {
                    "ColTitle": "Expenses",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "expense"
                        }
                    ]
                },
                {
                    "ColTitle": "Net Income",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "total"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "ColData": [
                        {
                            "value": "Example Label 1",
                            "id": "1"
                        },
                        {
                            "value": "10.00"
                        },
                        {
                            "value": ""
                        },
                        {
                            "value": "20.00"
                        }
                    ]
                },
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 2",
                                "id": "2"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "30.00"
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "Example Label 3",
                                        "id": "3"
                                    },
                                    {
                                        "value": "40.00"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "50.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 4"
                            },
                            {
                                "value": "60.00"
                            },
                            {
                                "value": "70.00"
                            },
                            {
                                "value": "80.00"
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "group": "GrandTotal",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 5"
                            },
                            {
                                "value": "90.00"
                            },
                            {
                                "value": "100.00"
                            },
                            {
                                "value": "110.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def inventory_valuation_summary() -> dict[str, Any]:
    """reports/InventoryValuationSummary: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "InventoryValuationSummary",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "DateMacro": "today",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "",
                    "ColType": "ProductsAndService",
                    "MetaData": []
                },
                {
                    "ColTitle": "SKU",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "Item/ItemSKU"
                        }
                    ]
                },
                {
                    "ColTitle": "Qty",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "Quantity"
                        }
                    ]
                },
                {
                    "ColTitle": "Asset Value",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "HomeAmount"
                        }
                    ]
                },
                {
                    "ColTitle": "Calc. Avg",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "AvgCost"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Data",
                    "ColData": [
                        {
                            "value": "Example Label 1",
                            "id": "1"
                        },
                        {
                            "value": ""
                        },
                        {
                            "value": "10.00"
                        },
                        {
                            "value": "20.00"
                        },
                        {
                            "value": "30.00"
                        }
                    ]
                },
                {
                    "type": "Data",
                    "ColData": [
                        {
                            "value": "Example Label 2",
                            "id": "2"
                        },
                        {
                            "value": ""
                        },
                        {
                            "value": "40.00"
                        },
                        {
                            "value": "50.00"
                        },
                        {
                            "value": "60.00"
                        }
                    ]
                },
                {
                    "type": "Section",
                    "group": "GrandTotal",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 3"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "70.00"
                            },
                            {
                                "value": "80.00"
                            },
                            {
                                "value": "90.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def journal_report() -> dict[str, Any]:
    """reports/JournalReport: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "JournalReport",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "Date",
                    "ColType": "Date",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "tx_date"
                        }
                    ]
                },
                {
                    "ColTitle": "Transaction Type",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "txn_type"
                        }
                    ]
                },
                {
                    "ColTitle": "Num",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "doc_num"
                        }
                    ]
                },
                {
                    "ColTitle": "Name",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "name"
                        }
                    ]
                },
                {
                    "ColTitle": "Memo/Description",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "memo"
                        }
                    ]
                },
                {
                    "ColTitle": "Account",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "account_name"
                        }
                    ]
                },
                {
                    "ColTitle": "Debit",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "debt_amt"
                        }
                    ]
                },
                {
                    "ColTitle": "Credit",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "credit_amt"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Data",
                    "ColData": [
                        {
                            "value": "2026-01-15"
                        },
                        {
                            "value": "Example Transaction Type 1",
                            "id": "1"
                        },
                        {
                            "value": ""
                        },
                        {
                            "value": "",
                            "id": ""
                        },
                        {
                            "value": "Example Memo/Description 2"
                        },
                        {
                            "value": "Example Account 3",
                            "id": "2"
                        },
                        {
                            "value": "10.00"
                        },
                        {
                            "value": ""
                        }
                    ]
                },
                {
                    "type": "Data",
                    "ColData": [
                        {
                            "value": "2026-01-15"
                        },
                        {
                            "value": "",
                            "id": "1"
                        },
                        {
                            "value": ""
                        },
                        {
                            "value": "",
                            "id": ""
                        },
                        {
                            "value": ""
                        },
                        {
                            "value": "Example Account 4",
                            "id": "3"
                        },
                        {
                            "value": ""
                        },
                        {
                            "value": "20.00"
                        }
                    ]
                },
                {
                    "type": "Section",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 5"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "30.00"
                            },
                            {
                                "value": "40.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def profit_and_loss_detail() -> dict[str, Any]:
    """reports/ProfitAndLossDetail: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "ProfitAndLossDetail",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "ReportBasis": "Accrual",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "Date",
                    "ColType": "Date",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "tx_date"
                        }
                    ]
                },
                {
                    "ColTitle": "Transaction Type",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "txn_type"
                        }
                    ]
                },
                {
                    "ColTitle": "Num",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "doc_num"
                        }
                    ]
                },
                {
                    "ColTitle": "Name",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "name"
                        }
                    ]
                },
                {
                    "ColTitle": "Memo/Description",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "memo"
                        }
                    ]
                },
                {
                    "ColTitle": "Split",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "split_acc"
                        }
                    ]
                },
                {
                    "ColTitle": "Amount",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "subt_nat_amount"
                        }
                    ]
                },
                {
                    "ColTitle": "Balance",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "rbal_nat_amount"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 1"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Section",
                                "Header": {
                                    "ColData": [
                                        {
                                            "value": "Example Section 2"
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        }
                                    ]
                                },
                                "Rows": {
                                    "Row": [
                                        {
                                            "type": "Section",
                                            "Header": {
                                                "ColData": [
                                                    {
                                                        "value": "Example Section 3",
                                                        "id": "1"
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    }
                                                ]
                                            },
                                            "Rows": {
                                                "Row": [
                                                    {
                                                        "type": "Section",
                                                        "Rows": {
                                                            "Row": [
                                                                {
                                                                    "type": "Data",
                                                                    "ColData": [
                                                                        {
                                                                            "value": "2026-01-15"
                                                                        },
                                                                        {
                                                                            "value": "Example Transaction Type 4",
                                                                            "id": "2"
                                                                        },
                                                                        {
                                                                            "value": "3"
                                                                        },
                                                                        {
                                                                            "value": "Example Name 5",
                                                                            "id": "3"
                                                                        },
                                                                        {
                                                                            "value": "Example Memo/Description 6"
                                                                        },
                                                                        {
                                                                            "value": "Example Split 7",
                                                                            "id": "4"
                                                                        },
                                                                        {
                                                                            "value": "10.00"
                                                                        },
                                                                        {
                                                                            "value": "20.00"
                                                                        }
                                                                    ]
                                                                },
                                                                {
                                                                    "type": "Data",
                                                                    "ColData": [
                                                                        {
                                                                            "value": "2026-01-15"
                                                                        },
                                                                        {
                                                                            "value": "Example Transaction Type 8",
                                                                            "id": "5"
                                                                        },
                                                                        {
                                                                            "value": "3"
                                                                        },
                                                                        {
                                                                            "value": "Example Name 9",
                                                                            "id": "6"
                                                                        },
                                                                        {
                                                                            "value": "Example Memo/Description 10"
                                                                        },
                                                                        {
                                                                            "value": "Example Split 11",
                                                                            "id": "4"
                                                                        },
                                                                        {
                                                                            "value": "30.00"
                                                                        },
                                                                        {
                                                                            "value": "40.00"
                                                                        }
                                                                    ]
                                                                },
                                                                {
                                                                    "type": "Data",
                                                                    "ColData": [
                                                                        {
                                                                            "value": "2026-01-15"
                                                                        },
                                                                        {
                                                                            "value": "Example Transaction Type 12",
                                                                            "id": "7"
                                                                        },
                                                                        {
                                                                            "value": "3"
                                                                        },
                                                                        {
                                                                            "value": "Example Name 13",
                                                                            "id": "3"
                                                                        },
                                                                        {
                                                                            "value": "Example Memo/Description 14"
                                                                        },
                                                                        {
                                                                            "value": "Example Split 15",
                                                                            "id": "4"
                                                                        },
                                                                        {
                                                                            "value": "50.00"
                                                                        },
                                                                        {
                                                                            "value": "60.00"
                                                                        }
                                                                    ]
                                                                }
                                                            ]
                                                        },
                                                        "Summary": {
                                                            "ColData": [
                                                                {
                                                                    "value": "Total Example 16"
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": "70.00"
                                                                },
                                                                {
                                                                    "value": ""
                                                                }
                                                            ]
                                                        }
                                                    },
                                                    {
                                                        "type": "Section",
                                                        "Header": {
                                                            "ColData": [
                                                                {
                                                                    "value": "Example Section 17"
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                }
                                                            ]
                                                        },
                                                        "Rows": {
                                                            "Row": [
                                                                {
                                                                    "type": "Section",
                                                                    "Header": {
                                                                        "ColData": [
                                                                            {
                                                                                "value": "Example Section 18",
                                                                                "id": "8"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            }
                                                                        ]
                                                                    },
                                                                    "Rows": {
                                                                        "Row": [
                                                                            {
                                                                                "type": "Data",
                                                                                "ColData": [
                                                                                    {
                                                                                        "value": "2026-01-15"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Transaction Type 19",
                                                                                        "id": "7"
                                                                                    },
                                                                                    {
                                                                                        "value": "3"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Name 20",
                                                                                        "id": "3"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Memo/Description 21"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Split 22",
                                                                                        "id": "4"
                                                                                    },
                                                                                    {
                                                                                        "value": "80.00"
                                                                                    },
                                                                                    {
                                                                                        "value": "90.00"
                                                                                    }
                                                                                ]
                                                                            },
                                                                            {
                                                                                "type": "Data",
                                                                                "ColData": [
                                                                                    {
                                                                                        "value": "2026-01-15"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Transaction Type 23",
                                                                                        "id": "9"
                                                                                    },
                                                                                    {
                                                                                        "value": "3"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Name 24",
                                                                                        "id": "10"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Memo/Description 25"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Split 26",
                                                                                        "id": "4"
                                                                                    },
                                                                                    {
                                                                                        "value": "100.00"
                                                                                    },
                                                                                    {
                                                                                        "value": "110.00"
                                                                                    }
                                                                                ]
                                                                            }
                                                                        ]
                                                                    },
                                                                    "Summary": {
                                                                        "ColData": [
                                                                            {
                                                                                "value": "Total Example 27"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": "120.00"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            }
                                                                        ]
                                                                    }
                                                                },
                                                                {
                                                                    "type": "Section",
                                                                    "Header": {
                                                                        "ColData": [
                                                                            {
                                                                                "value": "Example Section 28",
                                                                                "id": "11"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            }
                                                                        ]
                                                                    },
                                                                    "Rows": {
                                                                        "Row": [
                                                                            {
                                                                                "type": "Data",
                                                                                "ColData": [
                                                                                    {
                                                                                        "value": "2026-01-15"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Transaction Type 29",
                                                                                        "id": "2"
                                                                                    },
                                                                                    {
                                                                                        "value": "3"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Name 30",
                                                                                        "id": "3"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Memo/Description 31"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Split 32",
                                                                                        "id": "4"
                                                                                    },
                                                                                    {
                                                                                        "value": "130.00"
                                                                                    },
                                                                                    {
                                                                                        "value": "140.00"
                                                                                    }
                                                                                ]
                                                                            }
                                                                        ]
                                                                    },
                                                                    "Summary": {
                                                                        "ColData": [
                                                                            {
                                                                                "value": "Total Example 33"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": "150.00"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            }
                                                                        ]
                                                                    }
                                                                },
                                                                {
                                                                    "type": "Section",
                                                                    "Header": {
                                                                        "ColData": [
                                                                            {
                                                                                "value": "Example Section 34",
                                                                                "id": "12"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            }
                                                                        ]
                                                                    },
                                                                    "Rows": {
                                                                        "Row": [
                                                                            {
                                                                                "type": "Data",
                                                                                "ColData": [
                                                                                    {
                                                                                        "value": "2026-01-15"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Transaction Type 35",
                                                                                        "id": "9"
                                                                                    },
                                                                                    {
                                                                                        "value": "3"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Name 36",
                                                                                        "id": "10"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Memo/Description 37"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Split 38",
                                                                                        "id": "4"
                                                                                    },
                                                                                    {
                                                                                        "value": "160.00"
                                                                                    },
                                                                                    {
                                                                                        "value": "170.00"
                                                                                    }
                                                                                ]
                                                                            },
                                                                            {
                                                                                "type": "Data",
                                                                                "ColData": [
                                                                                    {
                                                                                        "value": "2026-01-15"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Transaction Type 39",
                                                                                        "id": "9"
                                                                                    },
                                                                                    {
                                                                                        "value": "3"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Name 40",
                                                                                        "id": "10"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Memo/Description 41"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Split 42",
                                                                                        "id": "4"
                                                                                    },
                                                                                    {
                                                                                        "value": "180.00"
                                                                                    },
                                                                                    {
                                                                                        "value": "190.00"
                                                                                    }
                                                                                ]
                                                                            }
                                                                        ]
                                                                    },
                                                                    "Summary": {
                                                                        "ColData": [
                                                                            {
                                                                                "value": "Total Example 43"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": "200.00"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            }
                                                                        ]
                                                                    }
                                                                }
                                                            ]
                                                        },
                                                        "Summary": {
                                                            "ColData": [
                                                                {
                                                                    "value": "Total Example 44"
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": "210.00"
                                                                },
                                                                {
                                                                    "value": ""
                                                                }
                                                            ]
                                                        }
                                                    }
                                                ]
                                            },
                                            "Summary": {
                                                "ColData": [
                                                    {
                                                        "value": "Total Example 45"
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": "220.00"
                                                    },
                                                    {
                                                        "value": ""
                                                    }
                                                ]
                                            }
                                        },
                                        {
                                            "type": "Section",
                                            "Header": {
                                                "ColData": [
                                                    {
                                                        "value": "Example Section 46",
                                                        "id": "13"
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    }
                                                ]
                                            },
                                            "Rows": {
                                                "Row": [
                                                    {
                                                        "type": "Data",
                                                        "ColData": [
                                                            {
                                                                "value": "2026-01-15"
                                                            },
                                                            {
                                                                "value": "Example Transaction Type 47",
                                                                "id": "2"
                                                            },
                                                            {
                                                                "value": "3"
                                                            },
                                                            {
                                                                "value": "Example Name 48",
                                                                "id": "3"
                                                            },
                                                            {
                                                                "value": "Example Memo/Description 49"
                                                            },
                                                            {
                                                                "value": "Example Split 50",
                                                                "id": "4"
                                                            },
                                                            {
                                                                "value": "230.00"
                                                            },
                                                            {
                                                                "value": "240.00"
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "type": "Data",
                                                        "ColData": [
                                                            {
                                                                "value": "2026-01-15"
                                                            },
                                                            {
                                                                "value": "Example Transaction Type 51",
                                                                "id": "5"
                                                            },
                                                            {
                                                                "value": "3"
                                                            },
                                                            {
                                                                "value": "Example Name 52",
                                                                "id": "6"
                                                            },
                                                            {
                                                                "value": "Example Memo/Description 53"
                                                            },
                                                            {
                                                                "value": "Example Split 54",
                                                                "id": "4"
                                                            },
                                                            {
                                                                "value": "250.00"
                                                            },
                                                            {
                                                                "value": "260.00"
                                                            }
                                                        ]
                                                    }
                                                ]
                                            },
                                            "Summary": {
                                                "ColData": [
                                                    {
                                                        "value": "Total Example 55"
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": "270.00"
                                                    },
                                                    {
                                                        "value": ""
                                                    }
                                                ]
                                            }
                                        }
                                    ]
                                },
                                "Summary": {
                                    "ColData": [
                                        {
                                            "value": "Total Example 56"
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": "280.00"
                                        },
                                        {
                                            "value": ""
                                        }
                                    ]
                                }
                            },
                            {
                                "type": "Section",
                                "Header": {
                                    "ColData": [
                                        {
                                            "value": "Example Section 57"
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        }
                                    ]
                                },
                                "Rows": {
                                    "Row": [
                                        {
                                            "type": "Section",
                                            "Header": {
                                                "ColData": [
                                                    {
                                                        "value": "Example Section 58"
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    }
                                                ]
                                            },
                                            "Rows": {
                                                "Row": [
                                                    {
                                                        "type": "Section",
                                                        "Header": {
                                                            "ColData": [
                                                                {
                                                                    "value": "Example Section 59"
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                }
                                                            ]
                                                        },
                                                        "Rows": {
                                                            "Row": [
                                                                {
                                                                    "type": "Section",
                                                                    "Header": {
                                                                        "ColData": [
                                                                            {
                                                                                "value": "Example Section 60",
                                                                                "id": "14"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            }
                                                                        ]
                                                                    },
                                                                    "Rows": {
                                                                        "Row": [
                                                                            {
                                                                                "type": "Data",
                                                                                "ColData": [
                                                                                    {
                                                                                        "value": "2026-01-15"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Transaction Type 61",
                                                                                        "id": "15"
                                                                                    },
                                                                                    {
                                                                                        "value": "3"
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Name 62",
                                                                                        "id": "12"
                                                                                    },
                                                                                    {
                                                                                        "value": ""
                                                                                    },
                                                                                    {
                                                                                        "value": "Example Split 63",
                                                                                        "id": "16"
                                                                                    },
                                                                                    {
                                                                                        "value": "290.00"
                                                                                    },
                                                                                    {
                                                                                        "value": "300.00"
                                                                                    }
                                                                                ]
                                                                            }
                                                                        ]
                                                                    },
                                                                    "Summary": {
                                                                        "ColData": [
                                                                            {
                                                                                "value": "Total Example 64"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            },
                                                                            {
                                                                                "value": "310.00"
                                                                            },
                                                                            {
                                                                                "value": ""
                                                                            }
                                                                        ]
                                                                    }
                                                                }
                                                            ]
                                                        },
                                                        "Summary": {
                                                            "ColData": [
                                                                {
                                                                    "value": "Total Example 65"
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": "320.00"
                                                                },
                                                                {
                                                                    "value": ""
                                                                }
                                                            ]
                                                        }
                                                    }
                                                ]
                                            },
                                            "Summary": {
                                                "ColData": [
                                                    {
                                                        "value": "Total Example 66"
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": "330.00"
                                                    },
                                                    {
                                                        "value": ""
                                                    }
                                                ]
                                            }
                                        },
                                        {
                                            "type": "Section",
                                            "Header": {
                                                "ColData": [
                                                    {
                                                        "value": "Example Section 67"
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    }
                                                ]
                                            },
                                            "Rows": {
                                                "Row": [
                                                    {
                                                        "type": "Section",
                                                        "Header": {
                                                            "ColData": [
                                                                {
                                                                    "value": "Example Section 68",
                                                                    "id": "17"
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                }
                                                            ]
                                                        },
                                                        "Rows": {
                                                            "Row": [
                                                                {
                                                                    "type": "Data",
                                                                    "ColData": [
                                                                        {
                                                                            "value": "2026-01-15"
                                                                        },
                                                                        {
                                                                            "value": "Example Transaction Type 69",
                                                                            "id": "18"
                                                                        },
                                                                        {
                                                                            "value": "3"
                                                                        },
                                                                        {
                                                                            "value": "Example Name 70",
                                                                            "id": "11"
                                                                        },
                                                                        {
                                                                            "value": ""
                                                                        },
                                                                        {
                                                                            "value": "Example Split 71",
                                                                            "id": "19"
                                                                        },
                                                                        {
                                                                            "value": "340.00"
                                                                        },
                                                                        {
                                                                            "value": "350.00"
                                                                        }
                                                                    ]
                                                                }
                                                            ]
                                                        },
                                                        "Summary": {
                                                            "ColData": [
                                                                {
                                                                    "value": "Total Example 72"
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": "360.00"
                                                                },
                                                                {
                                                                    "value": ""
                                                                }
                                                            ]
                                                        }
                                                    },
                                                    {
                                                        "type": "Section",
                                                        "Header": {
                                                            "ColData": [
                                                                {
                                                                    "value": "Example Section 73",
                                                                    "id": "20"
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                }
                                                            ]
                                                        },
                                                        "Rows": {
                                                            "Row": [
                                                                {
                                                                    "type": "Data",
                                                                    "ColData": [
                                                                        {
                                                                            "value": "2026-01-15"
                                                                        },
                                                                        {
                                                                            "value": "Example Transaction Type 74",
                                                                            "id": "21"
                                                                        },
                                                                        {
                                                                            "value": ""
                                                                        },
                                                                        {
                                                                            "value": "Example Name 75",
                                                                            "id": "11"
                                                                        },
                                                                        {
                                                                            "value": ""
                                                                        },
                                                                        {
                                                                            "value": "Example Split 76",
                                                                            "id": "22"
                                                                        },
                                                                        {
                                                                            "value": "370.00"
                                                                        },
                                                                        {
                                                                            "value": "380.00"
                                                                        }
                                                                    ]
                                                                }
                                                            ]
                                                        },
                                                        "Summary": {
                                                            "ColData": [
                                                                {
                                                                    "value": "Total Example 77"
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": ""
                                                                },
                                                                {
                                                                    "value": "390.00"
                                                                },
                                                                {
                                                                    "value": ""
                                                                }
                                                            ]
                                                        }
                                                    }
                                                ]
                                            },
                                            "Summary": {
                                                "ColData": [
                                                    {
                                                        "value": "Total Example 78"
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": ""
                                                    },
                                                    {
                                                        "value": "400.00"
                                                    },
                                                    {
                                                        "value": ""
                                                    }
                                                ]
                                            }
                                        }
                                    ]
                                },
                                "Summary": {
                                    "ColData": [
                                        {
                                            "value": "Total Example 79"
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": "410.00"
                                        },
                                        {
                                            "value": ""
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Example Section 80"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "420.00"
                            },
                            {
                                "value": ""
                            }
                        ]
                    }
                }
            ]
        }
    }


def class_sales() -> dict[str, Any]:
    """reports/ClassSales: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "ClassSales",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "ReportBasis": "Accrual",
            "SummarizeColumnsBy": "Total",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "",
                    "ColType": "Class",
                    "MetaData": []
                },
                {
                    "ColTitle": "Total",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "total"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "group": "**",
                    "ColData": [
                        {
                            "value": "Example Label 1"
                        },
                        {
                            "value": "10.00"
                        }
                    ]
                },
                {
                    "type": "Section",
                    "group": "GrandTotal",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 2"
                            },
                            {
                                "value": "20.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def customer_sales() -> dict[str, Any]:
    """reports/CustomerSales: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "CustomerSales",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "ReportBasis": "Accrual",
            "SummarizeColumnsBy": "Total",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "",
                    "ColType": "Customer",
                    "MetaData": []
                },
                {
                    "ColTitle": "Total",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "total"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "ColData": [
                        {
                            "value": "Example Label 1",
                            "id": "1"
                        },
                        {
                            "value": "10.00"
                        }
                    ]
                },
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 2",
                                "id": "2"
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "Example Label 3",
                                        "id": "3"
                                    },
                                    {
                                        "value": "20.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 4"
                            },
                            {
                                "value": "30.00"
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "group": "GrandTotal",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 5"
                            },
                            {
                                "value": "40.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def department_sales() -> dict[str, Any]:
    """reports/DepartmentSales: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "DepartmentSales",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "ReportBasis": "Accrual",
            "SummarizeColumnsBy": "Total",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "",
                    "ColType": "Department",
                    "MetaData": []
                },
                {
                    "ColTitle": "Total",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "total"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "group": "**",
                    "ColData": [
                        {
                            "value": "Example Label 1"
                        },
                        {
                            "value": "10.00"
                        }
                    ]
                },
                {
                    "type": "Section",
                    "group": "GrandTotal",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 2"
                            },
                            {
                                "value": "20.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def transaction_list_by_customer() -> dict[str, Any]:
    """reports/TransactionListByCustomer: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "TransactionListByCustomer",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "Date",
                    "ColType": "Date",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "tx_date"
                        }
                    ]
                },
                {
                    "ColTitle": "Transaction Type",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "txn_type"
                        }
                    ]
                },
                {
                    "ColTitle": "Num",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "doc_num"
                        }
                    ]
                },
                {
                    "ColTitle": "Posting",
                    "ColType": "Boolean",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "is_no_post"
                        }
                    ]
                },
                {
                    "ColTitle": "Memo/Description",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "memo"
                        }
                    ]
                },
                {
                    "ColTitle": "Account",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "account_name"
                        }
                    ]
                },
                {
                    "ColTitle": "Amount ",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "amount"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 1",
                                "id": "1"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 2",
                                        "id": "2"
                                    },
                                    {
                                        "value": "3"
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "Example Account 3",
                                        "id": "3"
                                    },
                                    {
                                        "value": "10.00"
                                    }
                                ]
                            },
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 4",
                                        "id": "4"
                                    },
                                    {
                                        "value": "3"
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "Example Account 5",
                                        "id": "5"
                                    },
                                    {
                                        "value": "20.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 6"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "30.00"
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 7"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Section",
                                "Header": {
                                    "ColData": [
                                        {
                                            "value": "Example Section 8",
                                            "id": "6"
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        }
                                    ]
                                },
                                "Rows": {
                                    "Row": [
                                        {
                                            "type": "Data",
                                            "ColData": [
                                                {
                                                    "value": "2026-01-15"
                                                },
                                                {
                                                    "value": "Example Transaction Type 9",
                                                    "id": "7"
                                                },
                                                {
                                                    "value": "3"
                                                },
                                                {
                                                    "value": "true"
                                                },
                                                {
                                                    "value": ""
                                                },
                                                {
                                                    "value": "Example Account 10",
                                                    "id": "3"
                                                },
                                                {
                                                    "value": "40.00"
                                                }
                                            ]
                                        },
                                        {
                                            "type": "Data",
                                            "ColData": [
                                                {
                                                    "value": "2026-01-15"
                                                },
                                                {
                                                    "value": "Example Transaction Type 11",
                                                    "id": "8"
                                                },
                                                {
                                                    "value": ""
                                                },
                                                {
                                                    "value": "true"
                                                },
                                                {
                                                    "value": ""
                                                },
                                                {
                                                    "value": "Example Account 12",
                                                    "id": "9"
                                                },
                                                {
                                                    "value": "50.00"
                                                }
                                            ]
                                        },
                                        {
                                            "type": "Data",
                                            "ColData": [
                                                {
                                                    "value": "2026-01-15"
                                                },
                                                {
                                                    "value": "Example Transaction Type 13",
                                                    "id": "10"
                                                },
                                                {
                                                    "value": "3"
                                                },
                                                {
                                                    "value": "true"
                                                },
                                                {
                                                    "value": ""
                                                },
                                                {
                                                    "value": "Example Account 14",
                                                    "id": "3"
                                                },
                                                {
                                                    "value": "60.00"
                                                }
                                            ]
                                        }
                                    ]
                                },
                                "Summary": {
                                    "ColData": [
                                        {
                                            "value": "Total Example 15"
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": ""
                                        },
                                        {
                                            "value": "70.00"
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 16"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "80.00"
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 17",
                                "id": "11"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 18",
                                        "id": "12"
                                    },
                                    {
                                        "value": "3"
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "Example Account 19",
                                        "id": "3"
                                    },
                                    {
                                        "value": "90.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 20"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "100.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def transaction_list_by_vendor() -> dict[str, Any]:
    """reports/TransactionListByVendor: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "TransactionListByVendor",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "Date",
                    "ColType": "Date",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "tx_date"
                        }
                    ]
                },
                {
                    "ColTitle": "Transaction Type",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "txn_type"
                        }
                    ]
                },
                {
                    "ColTitle": "Num",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "doc_num"
                        }
                    ]
                },
                {
                    "ColTitle": "Posting",
                    "ColType": "Boolean",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "is_no_post"
                        }
                    ]
                },
                {
                    "ColTitle": "Memo/Description",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "memo"
                        }
                    ]
                },
                {
                    "ColTitle": "Account",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "account_name"
                        }
                    ]
                },
                {
                    "ColTitle": "Amount",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "nat_amount"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 1",
                                "id": "1"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 2",
                                        "id": "2"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": "Example Memo/Description 3"
                                    },
                                    {
                                        "value": "Example Account 4",
                                        "id": "3"
                                    },
                                    {
                                        "value": "10.00"
                                    }
                                ]
                            },
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 5",
                                        "id": "4"
                                    },
                                    {
                                        "value": "3"
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "Example Account 6",
                                        "id": "5"
                                    },
                                    {
                                        "value": "20.00"
                                    }
                                ]
                            },
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    }
                                ]
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 7",
                                "id": "6"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 8",
                                        "id": "7"
                                    },
                                    {
                                        "value": "3"
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "Example Account 9",
                                        "id": "8"
                                    },
                                    {
                                        "value": "30.00"
                                    }
                                ]
                            },
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": ""
                                    }
                                ]
                            }
                        ]
                    }
                }
            ]
        }
    }


def transaction_list_with_splits() -> dict[str, Any]:
    """reports/TransactionListWithSplits: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "TransactionListWithSplits",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "Date",
                    "ColType": "Date",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "tx_date"
                        }
                    ]
                },
                {
                    "ColTitle": "Transaction Type",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "txn_type"
                        }
                    ]
                },
                {
                    "ColTitle": "No.",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "doc_num"
                        }
                    ]
                },
                {
                    "ColTitle": "Posting",
                    "ColType": "Boolean",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "is_no_post"
                        }
                    ]
                },
                {
                    "ColTitle": "Name",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "name"
                        }
                    ]
                },
                {
                    "ColTitle": "Memo/Description",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "memo"
                        }
                    ]
                },
                {
                    "ColTitle": "Account",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "account_name"
                        }
                    ]
                },
                {
                    "ColTitle": "Amount",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "nat_amount"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 1",
                                "id": "1"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 2",
                                        "id": "2"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": "Example Name 3",
                                        "id": "3"
                                    },
                                    {
                                        "value": "Example Memo/Description 4"
                                    },
                                    {
                                        "value": "Example Account 5",
                                        "id": "1"
                                    },
                                    {
                                        "value": "10.00"
                                    }
                                ]
                            },
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 6",
                                        "id": "2"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": "Example Name 7",
                                        "id": "3"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "Example Account 8",
                                        "id": "4"
                                    },
                                    {
                                        "value": "20.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 9"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "30.00"
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 10",
                                "id": "5"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 11",
                                        "id": "6"
                                    },
                                    {
                                        "value": "3"
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": "Example Name 12",
                                        "id": "7"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "Example Account 13",
                                        "id": "5"
                                    },
                                    {
                                        "value": "40.00"
                                    }
                                ]
                            },
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 14",
                                        "id": "6"
                                    },
                                    {
                                        "value": "3"
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": "Example Name 15",
                                        "id": "7"
                                    },
                                    {
                                        "value": "Example Memo/Description 16"
                                    },
                                    {
                                        "value": "Example Account 17",
                                        "id": "8"
                                    },
                                    {
                                        "value": "50.00"
                                    }
                                ]
                            },
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 18",
                                        "id": "9"
                                    },
                                    {
                                        "value": "3"
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": "Example Name 19",
                                        "id": "10"
                                    },
                                    {
                                        "value": "Example Memo/Description 20"
                                    },
                                    {
                                        "value": "Example Account 21",
                                        "id": "11"
                                    },
                                    {
                                        "value": "60.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 22"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "70.00"
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 23",
                                "id": "12"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 24",
                                        "id": "13"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": "Example Name 25",
                                        "id": "14"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "Example Account 26",
                                        "id": "12"
                                    },
                                    {
                                        "value": "80.00"
                                    }
                                ]
                            },
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 27",
                                        "id": "13"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "true"
                                    },
                                    {
                                        "value": "Example Name 28",
                                        "id": "14"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "Example Account 29",
                                        "id": "5"
                                    },
                                    {
                                        "value": "90.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 30"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "100.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def vendor_balance() -> dict[str, Any]:
    """reports/VendorBalance: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "VendorBalance",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "DateMacro": "all",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "",
                    "ColType": "Vendor",
                    "MetaData": []
                },
                {
                    "ColTitle": "Total",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "total"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "ColData": [
                        {
                            "value": "Example Label 1",
                            "id": "1"
                        },
                        {
                            "value": "10.00"
                        }
                    ]
                },
                {
                    "ColData": [
                        {
                            "value": "Example Label 2",
                            "id": "2"
                        },
                        {
                            "value": "20.00"
                        }
                    ]
                },
                {
                    "type": "Section",
                    "group": "GrandTotal",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 3"
                            },
                            {
                                "value": "30.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def vendor_balance_detail() -> dict[str, Any]:
    """reports/VendorBalanceDetail: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "VendorBalanceDetail",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "DateMacro": "all",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "Date",
                    "ColType": "Date",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "tx_date"
                        }
                    ]
                },
                {
                    "ColTitle": "Transaction Type",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "txn_type"
                        }
                    ]
                },
                {
                    "ColTitle": "Num",
                    "ColType": "String",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "doc_num"
                        }
                    ]
                },
                {
                    "ColTitle": "Due Date",
                    "ColType": "Date",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "due_date"
                        }
                    ]
                },
                {
                    "ColTitle": "Amount",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "subt_neg_amount"
                        }
                    ]
                },
                {
                    "ColTitle": "Open Balance",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "subt_neg_open_bal"
                        }
                    ]
                },
                {
                    "ColTitle": "Balance",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "rbal_neg_open_bal"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 1",
                                "id": "1"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 2",
                                        "id": "2"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "10.00"
                                    },
                                    {
                                        "value": "20.00"
                                    },
                                    {
                                        "value": "30.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 3"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "40.00"
                            },
                            {
                                "value": "50.00"
                            },
                            {
                                "value": ""
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "Header": {
                        "ColData": [
                            {
                                "value": "Example Section 4",
                                "id": "3"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            }
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "Example Transaction Type 5",
                                        "id": "4"
                                    },
                                    {
                                        "value": ""
                                    },
                                    {
                                        "value": "2026-01-15"
                                    },
                                    {
                                        "value": "60.00"
                                    },
                                    {
                                        "value": "70.00"
                                    },
                                    {
                                        "value": "80.00"
                                    }
                                ]
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 6"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "90.00"
                            },
                            {
                                "value": "100.00"
                            },
                            {
                                "value": ""
                            }
                        ]
                    }
                },
                {
                    "type": "Section",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 7"
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": ""
                            },
                            {
                                "value": "110.00"
                            },
                            {
                                "value": "120.00"
                            },
                            {
                                "value": ""
                            }
                        ]
                    }
                }
            ]
        }
    }


def vendor_expenses() -> dict[str, Any]:
    """reports/VendorExpenses: structure of a live sandbox response, with every value synthetic."""
    return {
        "Header": {
            "ReportName": "VendorExpenses",
            "Currency": "USD",
            "Time": "2026-09-24T00:00:00-07:00",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-06-30",
            "ReportBasis": "Accrual",
            "SummarizeColumnsBy": "Total",
            "Option": [
                {
                    "Name": "NoReportData",
                    "Value": "false"
                }
            ]
        },
        "Columns": {
            "Column": [
                {
                    "ColTitle": "",
                    "ColType": "Vendor",
                    "MetaData": []
                },
                {
                    "ColTitle": "Total",
                    "ColType": "Money",
                    "MetaData": [
                        {
                            "Name": "ColKey",
                            "Value": "total"
                        }
                    ]
                }
            ]
        },
        "Rows": {
            "Row": [
                {
                    "ColData": [
                        {
                            "value": "Example Label 1",
                            "id": "1"
                        },
                        {
                            "value": "10.00"
                        }
                    ]
                },
                {
                    "ColData": [
                        {
                            "value": "Example Label 2",
                            "id": "2"
                        },
                        {
                            "value": "20.00"
                        }
                    ]
                },
                {
                    "type": "Section",
                    "group": "GrandTotal",
                    "Summary": {
                        "ColData": [
                            {
                                "value": "Total Example 3"
                            },
                            {
                                "value": "30.00"
                            }
                        ]
                    }
                }
            ]
        }
    }


def tax_summary() -> dict[str, Any]:
    """reports/TaxSummary: DOCUMENTATION-DERIVED from the sample on Intuit's page
    (never observed live: the sandbox company refused it with fault 5020 on
    2026-09-24). Two keyless columns -- a String label and a Money "Total" --
    and one Data row per "Case NN" group."""
    return {
        "Header": {"ReportName": "TaxSummary", "Currency": "USD", "Time": "2026-09-24T00:00:00-07:00",
                   "StartPeriod": "2026-01-01", "EndPeriod": "2026-06-30", "ReportBasis": "Accrual",
                   "Option": [{"Name": "NoReportData", "Value": "false"}]},
        "Columns": {"Column": [{"ColTitle": "", "ColType": "String", "MetaData": []},
                               {"ColTitle": "Total", "ColType": "Money", "MetaData": []}]},
        "Rows": {"Row": [
            {"ColData": [{"value": "Case 01 Example"}, {"value": "10.00"}], "group": "Case 01"},
            {"ColData": [{"value": "Case 02 Example"}, {"value": ""}], "group": "Case 02"},
            {"ColData": [{"value": "Case 22 Example"}, {"value": "20.00"}], "group": "Case 22"},
        ]},
    }
