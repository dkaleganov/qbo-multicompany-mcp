# Tools

Every Intuit-backed tool takes an explicit `entity`
key (from `list_entities`) and echoes it back with `environment`,
`display_name_from_registry`, `realm_id_last4`, `request_started_at` and
`fetched_at`, so a reader can always say which company file an answer came
from and how old it is. There is no default company.

This page covers the eighteen default tools and the one opt-in. Start with
"Choosing a tool" if you're deciding which to call. [design.md](design.md)
explains the design behind them; this is the reference for what each tool takes
and returns.

Conventions every tool shares:

- Dates are `YYYY-MM-DD` book dates, passed through unconverted. Pass timestamps
  with an offset or `Z`: `list_changes` reads one without as UTC.
- `limit`, on the walked lists that take one, is 1 to 5,000 (default 200) and trims after a
  complete walk; `max_rows` on a report is 1 to 5,000 and trims locally, and
  both say so (`truncated`, `client_truncated`).
- A refusal happens before any request where the input alone decides it,
  and its text is fixed: it names the parameter, never the caller's value.
  A `date_macro` is resolved once the company's settings are read, because a
  fiscal macro needs them, so the window it produces is checked then.
- Every tool that returns text from a company file says in its description
  that the data can reach the AI client's model provider and that the text
  is untrusted input.
- In the published schema every parameter has a description, every
  `YYYY-MM-DD` argument declares `format: date`, and every tool carries
  `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`
  and a short title. `openWorldHint` is `true` on
  every tool that can reach QuickBooks and `false` on the two that make no
  request, `list_entities` and `server_info`. The hints
  describe the code; the network policy is what enforces read-only.

## Choosing a tool

| You want | Use | Not |
|---|---|---|
| Which companies this server can answer for, and their auth state | `list_entities` | anything else first |
| This server's build, environment, read-only status and whether documents are enabled | `server_info` | `list_entities` (that is per company) |
| One company's profile, settings and derived capabilities | `get_company_info` | `list_entities` (no request; cached capabilities only) |
| The chart of accounts with balances, and account ids for filters | `list_accounts` | `run_report(AccountListDetail)` (the report view, no ids on rows) |
| Vendors with the 1099 flag and whether a tax id is on file | `list_vendors` | `summarize_vendor_payments` (what was paid, not who) |
| Customers with their balances | `list_customers` | `report_aging(receivables)` (buckets, not the list) |
| Transactions in a window, one row each, to find ids | `list_transactions` | `report_general_ledger` |
| One transaction's lines | `get_transaction` | a report |
| Filters the list tools lack, on a list object, or a `COUNT(*)` on any served type | `run_query` | a report |
| Candidate vendor payments for a year | `summarize_vendor_payments` | `report_aging` (what is owed, not what was paid) |
| Recent changes and deletions on list objects (30 days) | `list_changes` | `run_query` on a timestamp (cannot see deletions); tax codes and rates (outside change data capture) |
| Classes, departments, items, terms, payment methods, tax codes or tax rates, with the ids the report filters take | `list_reference_data` | `list_accounts` (accounts) |
| Whether a transaction has a receipt or a note, and what it is | `list_attachments` | `get_attachment` (the file itself, opt-in) |
| Income and expense totals for a period, by section | `report_profit_and_loss` | `report_general_ledger` (postings, not totals) |
| Balances as of a date or a date macro, by section | `report_balance_sheet` | `list_accounts` (current balances only, no history) |
| The postings behind an account, with running balances | `report_general_ledger` | `list_transactions` (one flat row per transaction, no account grouping) |
| What is owed to vendors or by customers, and how old it is | `report_aging` | `list_vendors` / `list_customers` (balances, no buckets) |
| Any other documented report: cash flow, trial balance, journal, the transaction detail behind a P&L or a customer or vendor, sales by class, customer, department or product, customer or vendor balances and their details, inventory valuation, customer income, vendor expenses, tax summary, the account list | `run_report` | the named tools for their own reports; `list_transactions` for TransactionList |
| The file behind an attachment (only when this server was started with `--allow-documents` and a storage host is pinned) | `get_attachment` | `list_attachments` (metadata; always available) |

## `list_entities`

No parameters; makes no request. Reads the registry and the token store and
returns, per configured company: `entity` (the key every other tool takes),
`display_name_from_registry`, `environment`, `realm_id_last4` (diagnostic
only), `auth_state`, `last_refresh_at`, `refresh_age_seconds` and
`stale_refresh` (per realm, a passive observation: over an hour since the last refresh), `days_to_refresh_token_expiry`
(the 100-day inactivity clock), `refresh_token_hard_expiry` (the five-year
clock: `basis` is `observed` when Intuit reported it, otherwise
`upper_bound_from_consent` or `unknown`), `days_to_access_token_expiry`, the
last name check, and the capabilities a guard pass stored for this consent
(null until one has run), with accounts payable as this process knows it
(`unknown` until established). Also `token_store_available`, and when the store cannot be
opened or read, a note giving the store's own refusal: a store in a git working
copy or a sync folder says that, and only the plaintext refusal names
`--allow-plaintext-store`.

## `server_info`

No parameters; makes no request. `name`, `version`, `build`, `environment`,
`api_base`, `read_only: true`, `writes_enabled: false`, `documents_enabled`,
`entity_count`, `authorized_entity_count`, `token_store_protection`,
`token_store_available` with the same note as `list_entities`,
`credential_use_enabled` (false, with a note naming the two variables, when
the server started without app credentials), `any_stale` and
`stale_entities`. Stale means over an hour without a refresh, which is normal
between daily keepalive runs.

## `get_company_info`

`entity`. CompanyInfo projected (no address, telephone, email or web
address; `employer_id_last4` only) plus Preferences as `settings`:
`report_basis`, `fiscal_year_start_month`, `book_close_date`,
`home_currency`, `multi_currency_enabled`, `class_tracking_enabled`,
`department_tracking_enabled`, `department_terminology`. `capabilities` is
derived from them (`class_tracking`, `department_tracking`, `inventory`,
`multi_currency`, `accounting_basis`, `fiscal_year_start_month`) plus
`accounts_payable`, which is established per process by first use.
`guard_checked_at` and `company_name_matches_registry` report the guard,
which compares the company's name with the registry's exact `display_name`
before any other read. On a mismatch this tool returns the name QuickBooks
reports, so the registry can be corrected, with the difference class and a note
quoting the registry's name. Every other tool refuses and names neither.

## `list_accounts`

`entity`, `active?` (default `true`; `null` sends `Active IN (true, false)`,
the documented way to include inactive rows, as `list_vendors` does),
`account_type?` and `classification?`. The chart of
accounts as `SELECT * FROM Account WHERE <filters>` returns it, walked to the
end (count-anchored with the same WHERE, deduplicated,
`consistency: non_snapshot`).

`account_type` is the Account entity's own vocabulary, WITH spaces:
`Bank`, `Accounts Receivable`, `Other Current Asset`, `Fixed Asset`,
`Other Asset`, `Accounts Payable`, `Credit Card`, `Long Term Liability`,
`Other Current Liability`, `Equity`, `Income`, `Cost of Goods Sold`,
`Expense`, `Other Expense`, `Other Income`: the value each row's
`account_type` carries. The report filters (`list_transactions`'
`source_account_type`, `report_general_ledger`'s `account_type`) take the
same values WITHOUT spaces (`AccountsPayable`), and each refuses the other's
spelling. Intuit's pages show only some of these spellings. All fifteen
answered 200 in sandbox on 2026-09-26, and the de-spaced `FixedAsset` answered
HTTP 400.
`classification`: `Asset`, `Equity`, `Expense`, `Liability`, `Revenue`.

Each row: `id`, `name`,
`fully_qualified_name`, `description`, `account_type` (the SPACED form, as
QuickBooks reports it), `account_sub_type`, `classification`, `active`,
`sub_account`, `parent`, `current_balance`,
`current_balance_with_sub_accounts` (which already contains the children:
never add the two), `currency`, and `acct_num` truncated to its last four on
Bank and Credit Card accounts.

## `list_vendors`

`entity`, `active?` (default `true`; `null` includes inactive vendors),
`vendor_1099?`, `updated_since?` (a date, or a date and time to the second such as
`2026-09-01T00:00:00`, with an optional fraction and `Z` or an offset), `limit`. Walked by
`DisplayName`: `id`, `display_name`, `company_name`, `active`,
`vendor_1099`, `balance`, `terms`, `currency`, and `tax_id_on_file` --
`true`, `false`, or `null` with `tax_id_on_file_reason` when QuickBooks's
omission makes presence unknowable. Never the identifier, and presence
is not evidence of a valid tax form.

## `list_customers`

`entity`, `active?` (default `true`; `null` for both), `updated_since?` (as
for `list_vendors`), `limit`. `id`, `display_name`, `company_name`, `active`, `job`, `balance`,
`balance_with_jobs`, `parent`, `currency`. No tax ids, notes or contact
fields.

## `list_transactions`

`entity`, `start_date`, `end_date` (inclusive; at most six calendar months,
refused before any request otherwise), `transaction_type?` (the TransactionList
vocabulary, pinned: Bill, Charge, Check, CreditCardCharge, CreditMemo,
Deposit, Estimate, Invoice, JournalEntry, SalesReceipt: a third vocabulary,
neither the display labels nor the entity names), `vendor?` and `customer?`
(lists of ids, sent comma-joined), `cleared?` (Cleared, Uncleared, Reconciled, Deposited),
`source_account_type?` (the AccountType enum with the spaces removed,
pinned), `docnum?`, `memo?`, `columns?` (restricted to the allowlist, named
by position when refused; `tx_date` and `txn_type` are always added),
`max_rows` (default 200). Without `columns`, the request names the nine
columns QuickBooks itself returns when sent none, in its order: `tx_date`,
`txn_type`, `doc_num`, `is_no_post`, `name`, `memo`, `account_name`,
`other_account`, `subt_nat_amount` (observed 2026-09-24). A caller
may ask for any allowlisted keys; as on the report tools, a list of 25 or
more, or a request that would carry 25 or more once the two routing columns
are added, is refused before any request: Intuit warns that 25 or more
columns risks a 504, and a report is never retried on one.
No `accounting_method`: it isn't among the parameters Intuit documents for
TransactionList, and the network policy refuses it.

One logical report request (`GET reports/TransactionList`), flattened by the
report contract below: each routable row keeps `txn_type` with its
`entity_id`, which is how a row reaches `get_transaction`: see that tool for
turning a label into a type. `filters_sent`
names what was sent. A filtered request that returns zero rows is re-checked
over the same window unfiltered (`zero_row_recheck`), because an id or
free-text filter that matches nothing answers 200 with no rows, exactly like
a window with nothing in it. Results carry `filterable_transaction_types` and
`filterable_source_account_types`, since the value read off a row is not the
value that filters on it. `completeness` is `unknown` unless an anomaly or a
trim makes it `partial`: TransactionList publishes no row total and no cap
signal.

## `get_transaction`

A row's `txn_type` is a display label, and this tool takes an entity name. Checked on
sandbox on 2026-11-01:

| Row label | `transaction_type` |
|---|---|
| `Expense`, `Cash Expense`, `Credit Card Expense`, `Credit Card Credit`, `Check` | `Purchase` |
| `Bill Payment (Check)`, `Bill Payment (Credit Card)` | `BillPayment` |
| `Journal Entry` | `JournalEntry` |
| `Sales Receipt` | `SalesReceipt` |
| `Credit Memo` | `CreditMemo` |
| `Refund` | `RefundReceipt` |
| `Purchase Order` | `PurchaseOrder` |
| `Bill`, `Invoice`, `Payment`, `Deposit` | The same name |

`Billable Expense Charge`, `Charge`, `Estimate`, `Inventory Qty Adjust`, `Sales Tax Payment`
and `Time Charge` rows have no route here.

`entity`, `transaction_type` (one of Bill, BillPayment, CreditMemo, Deposit,
Invoice, JournalEntry, Payment, Purchase, PurchaseOrder, RefundReceipt,
SalesReceipt, Transfer, VendorCredit), `id`. One read by id through the
query endpoint, projected with its lines: amounts, dates, document number,
party and account references, tax references (`TaxCodeRef`,
`TxnTaxCodeRef`, `TaxRateRef`), `CurrencyRef`, the linked transactions and
`private_note` (free text from the company file). No addresses, email or
card details (`CreditChargeInfo`).

## `run_query`

`entity`, `statement`, `with_count?` (default `false`). One `SELECT * FROM
<Entity>` or `SELECT COUNT(*) FROM <Entity>`, with `WHERE` clauses joined by
`AND`, `ORDERBY`, `STARTPOSITION` and `MAXRESULTS` (at most 1,000), and
nothing else: no field lists, no subqueries, no comments, no second
statement, at most 4,000 characters. The parser consumes the whole input,
checks every field and operator against the entity's filterable and
sortable tables, and rebuilds a canonical statement, which it returns as
`statement`; the entity name is always sent in its canonical case.
`ORDERBY Id` is refused: Intuit's change log says production stopped on
2026-01-27, though sandbox still honours it.
Served entities: Account, Attachable, Bill, BillPayment, Class, CreditMemo,
Customer, Department, Deposit, Invoice, Item, JournalEntry, Payment,
PaymentMethod, Purchase, PurchaseOrder, RefundReceipt, SalesReceipt,
TaxCode, TaxRate, Term, Transfer, Vendor, VendorCredit. `SELECT *` returns rows
for Account, Attachable, Class, Customer, Department, Item, PaymentMethod,
TaxCode, TaxRate, Term and Vendor, through the projection each one's list tool
uses. The transaction entities answer `COUNT(*)` only, in `total_count`. `SELECT *` on
one is refused before any request, naming `list_transactions` and
`get_transaction`, which serve those rows. Without `STARTPOSITION` or `MAXRESULTS` it walks every page. With
either, rows are the one requested page and `truncated` is always true.
`with_count` returns the `COUNT(*)` for the same `WHERE` (`total_count`): the
walk's own anchor, or one more request for an explicit page. It is an
observation, not a snapshot guarantee. GET
only: Intuit's read-only `POST /query` is deliberately not used.

## `summarize_vendor_payments`

`entity`, `year` (calendar year), `vendor?` (one vendor id), `include_card?`
(default `true`: shows card detail rows; card totals are reported either
way), `detail?` (`summary` or `rows`). Walks Purchase and BillPayment for
the year by `TxnDate` (`date_basis`), and JournalEntry lines touching a
vendor, which are returned for review and never summed. Per vendor:
`candidate_non_card_amount`, `candidate_card_amount`, `payment_count`,
`sources`. `sources` at the top level says which sources were walked, so a
company without accounts payable reports a Purchase-only walk as such.
`needs_review` (`no_payee`: transaction ids with no payee, in no total;
`journal_entry_vendor_lines`: signed lines touching a vendor, never summed),
`unclassified` (an unlisted payment type, a missing or unreadable amount:
never a zero), `excluded_summary`, and the two notes that say what a
candidate total is not: bills are never added to their payments, one
economic payment recorded twice is outside deduplication, and payment type
is an accounting signal, not evidence of settlement or of any filing
obligation. Refused for a multi-currency company (amounts in different
currencies are never added). No threshold, no upper bound.

## `list_changes`

`entity`, `since` (a timestamp within the last 30 days), `types?` (Account,
Attachable, Class, Customer, Department, Item, PaymentMethod, Term or Vendor;
default Vendor, Customer and Account), `full?` (default
`false`: include each object's projected row), `limit`. One change-data-capture
request: `objects` (each `entity_type`, `id`, `status`, which is `Deleted` for a
deletion with no row, and `last_updated_time`), `upstream_object_count`,
`returned_count`, `truncated`. The 1,000-object cap is shared across all
requested types, so a saturated response marks
every requested type uncertain (`possibly_truncated`, `uncertain_types`,
`completeness_reason`) and `checkpoint_safe: false`. A response that isn't the
documented shape is refused whole rather than read in part. TaxCode and TaxRate are
refused: change data capture does not cover them. Transaction entities are
refused too, naming the missing projection. A query on a
modification time is not an equivalent fallback: it cannot see deletions.

## The report contract (all report tools)

Every report tool returns the same envelope (`ReportEnvelope`):

- `summary` first: one sentence; begins `PARTIAL RESULT` whenever any anomaly
  exists.
- `columns`: the published LEAF columns, each with its `key` (identity in this
  result), `leaf_key` (the ColKey as declared, or null), `title`, `type`,
  `parent_title` for a nested column, `period_start`/`period_end` for a period
  column, and `synthetic_key: true` when this server named a keyless label
  column (`account`, `customer`, `vendor`, `item`, `class`, `department`,
  `label`). `excluded_column_keys` names every column that existed upstream and
  was withheld, which differs from it not existing.
- `rows`: `row_id` and `parent_row_id` (positional paths, stable for a
  payload), `row_type` (`data`, `section_header`, `section_summary`), `depth`,
  `group` (QuickBooks's section group: Income, NetIncome, GrandTotal, ...).
  Summary rows carry `scope` (what the total is a total of) and `covers`
  (`upstream_section`: the total QuickBooks computed over the whole section;
  `unknown` is kept for a response that signals a cap, and no report this
  server calls publishes one).
- cells: `column_key`, verbatim `text`, `amount` (exact decimal, money,
  quantity and percent columns only; money also carries `currency`; a
  percent cell carries `unit: percent` and no currency; a column keyed
  `Quantity` is a quantity whatever its declared type (InventoryValuationSummary
  and SalesByProduct declare it Money), so its cells carry no currency and
  it enters no money total), `entity_id` where
  upstream supplied one (the routing id on a transaction-type cell, the
  account/customer/vendor/item id on a label cell), `label` where the key
  alone says nothing (an aging bucket keyed `1` is titled `31 - 60`), and
  `period` (`start/end`) under a period column.
- `anomalies`: every cell-, row- and column-level problem, over every upstream
  row including trimmed ones. An affected cell keeps its text and has no
  amount; a row with the wrong number of cells has every value withheld;
  a total QuickBooks states is kept as stated and flagged when
  the returned cells cannot reproduce it. `completeness` reads `partial` while
  any exist.
- `disclosures`: facts that are not anomalies and do not make the result
  partial (an empty report whose columns declared no type).
- `completeness`: `unknown` unless something establishes otherwise: these
  reports publish no row total and no documented cap signal; an anomaly or a
  local trim makes it `partial`. `upstream_no_report_data` is QuickBooks's own
  `NoReportData` option, reported as stated, never inferred from an empty rows
  array.
- `parameters_sent` (names only), `accounting_method_sent` with
  `accounting_method_source` (`caller`, or `company_report_basis` when the
  company's `Preferences.ReportPrefs.ReportBasis` was sent as the default),
  `capabilities_checked`.

Rules shared by every report tool: `date_macro` and explicit
dates are mutually exclusive; `summarize_column_by` takes the 12 documented
values plus `FiscalYear`; on a report with a date window, `Days` needs a window
of at most 200 days, from explicit dates or a macro, which is resolved first; `columns` lists of 25 or
more are refused, and so is a request that would carry 25 or more once
`tx_date` and `txn_type` are added; `max_rows` defaults to 500 and is at most 5,000, trimming
locally and saying so; a parameter Intuit doesn't document for the report
is refused before any request, and the network policy refuses it again
per path. A `class` or `department` filter requires the company to track
classes or departments; without the capability the filter is refused with a
fixed error, never dropped. Detail reports carry the six-calendar-month
window, on explicit dates and on a resolved macro alike. The two balance
details take no macro and may omit their window.

Every report id filter takes a list of ids: `customer`,
`vendor`, `class`, `department`, `item`, `employee` and `account`, wherever a
report takes them, and `list_transactions`' `customer` and `vendor`. Intuit's
pages document them as "One or more comma separated ... IDs", so the
list is sent as that one comma-separated value. Each element must be a run of
digits. That's this package's rule: the pages type an id only as a String, but
every id seen has been digits, in sandbox and in production, and QuickBooks
refuses a non-id shape. So a comma or a space can never turn one id into two; an
empty list or a malformed element is refused before any request, naming the
parameter and its position, never the value. Two report filters are not id
lists, by their own pages: `agency_id` is TaxSummary's one required id, and
`payment_method` takes a payment method's name (Cash, Check, Visa, ...).

**Date macros are resolved before they are checked.** A macro is
turned into a window in the company's book calendar: fiscal macros against
`FirstMonthOfFiscalYear` from Preferences, calendar macros against January --
as of the operator's local date, and that window is what the six-month rule
and the 200-day `Days` rule read. The request still sends the macro;
QuickBooks resolves it for real, and the response's `start_period`/`end_period`
are the authority. The result carries `date_macro_sent`, `resolved_start`,
`resolved_end` and `resolution_basis`, which says the date and the fiscal year
start it used. A fiscal macro on a company whose Preferences carry no readable
fiscal year start is refused, never resolved against a January nobody stated.
Three things Intuit's pages don't define were probed live on 2026-09-25
against QuickBooks's own `start_period`/`end_period`: a week starts on Sunday
(This Week 09-20..09-26, equal); a previous period's "-to-date" ends at the
same calendar position (Last Fiscal Quarter-to-date 04-01..06-25 and Last
Month-to-date 08-01..08-25, equal); and "Next 4 Weeks" starts on the next
week's Sunday and runs 28 days (09-27..10-24 on Friday 09-25). What "Next 4 Weeks"
does when today is itself a Sunday is not shown and is labelled an
assumption (the week starting seven days on, as for "Next Week").
Twenty-two report pages list the same 23 macros; TransactionList,
TransactionListByCustomer and TransactionListByVendor list 33 (ten Calendar
macros more); four pages list none. Each report admits its own page's list.

**Entity groupings of `summarize_column_by`** (Customers, Vendors, Classes,
Departments, Employees, ProductsAndServices; observed 2026-09-25) return one
Money column per entity keyed by the entity's id with its name as the title,
plus `not_specified` and `total`. They are admitted under the request that
named the grouping; each such column carries `entity_id` and `entity_type`
(Customer, Vendor, Class, Department, Employee, Item: where the id routes),
and a repeated entity key is that entity's total column, published as
`<id> (total)` with `entity_total: true`. Every published column says how it
was admitted (`admitted_by`: `key`, `period`, `grouping`, `label_position` or
`title`).

**Percent columns declared Money** (observed 2026-09-25 on both sandbox
companies). SalesByProduct's `% of Sales` (`total/PercentSales`) and `Gross Margin %`
(`total/GrossMarginPerc`) are declared Money and carry `'30.69 %'`. A
Money-typed column whose key ends in `Perc` or `PercentSales`, or whose
title carries `%`, is a percent column, except that an entity grouping's
column is never one by its title, because that title is the entity's name
(a customer called "10% Club" is not a percentage). A cell of a decimal followed by the
sign has `amount` `30.69`, `unit: percent` and no currency, and is no
anomaly. Any other text in such a column, a bare number included, is a
`malformed_amount`, and a `%` in a Money column that is not a percent column
stays one. A percent column is never totalled by this server.

Fault 5020 ("Permission Denied") from any report is a fixed, actionable error
naming the code, never an empty report; 5030 means the company's QuickBooks
plan lacks the feature, and 6190 is the subscription.
Fault 10000 is an internal error on Intuit's side, which Intuit lists as
non-recoverable; the error says retrying the same request unchanged is unlikely
to help. A fault this server has no hint for is named by its codes
(`fault=6000`): one to seven digits each, at most five, never the fault's
message or detail, which can echo the request.

## `report_profit_and_loss`

`GET reports/ProfitAndLoss`.

| Parameter | Notes |
|---|---|
| `start_date`, `end_date` | `YYYY-MM-DD`; not with `date_macro`; end not before start |
| `date_macro` | one of the 23 documented values |
| `accounting_method` | `Cash` or `Accrual`; defaults to the company's report basis |
| `summarize_column_by` | `Total`, `Month`, `Week`, `Days`, `Quarter`, `Year`, `FiscalYear`, `Customers`, `Vendors`, `Classes`, `Departments`, `Employees`, `ProductsAndServices` |
| `customer`, `vendor` | lists of ids, sent as one comma-separated value |
| `class`, `department` | lists of ids; require `class_tracking` / `department_tracking` |
| `max_rows` | default 500 |

Columns: `account` (Account) and `total` (Money) by default; summarised by a
period, one Money column per period keyed by its label (`Jan 2026`) with
`period_start`/`period_end`, admitted by that shape; summarised by an entity
grouping, one Money column per entity keyed by its id (see the shared rules).
Row groups seen live: `Income`, `GrossProfit`, `Expenses`, `NetOperatingIncome`,
`NetIncome`.

## `report_balance_sheet`

`GET reports/BalanceSheet`.

| Parameter | Notes |
|---|---|
| `as_of` | `YYYY-MM-DD`; sent as `end_date`. One of `as_of` and `date_macro` is required; not both |
| `date_macro` | one of the 23 documented values; its end is the balance sheet date; not with `as_of` or `start_date` |
| `start_date` | optional with `as_of`; needed with `summarize_column_by=Days` unless a macro gives the window |
| `accounting_method`, `summarize_column_by`, `class`, `department`, `max_rows` | as for the profit and loss |

`as_of` or `date_macro`, one required, mutually exclusive; the macro resolves
like every other. Four levels of nesting were observed live
(`TotalLiabilitiesAndEquity > Liabilities > CurrentLiabilities > AP > data`);
`NetIncome` inside Equity arrives as a DATA row with a group, not a section.

## `report_general_ledger`

`GET reports/GeneralLedger`.

| Parameter | Notes |
|---|---|
| `start_date`, `end_date` | required; at most six calendar months |
| `account` | list of account ids, sent comma-joined |
| `account_type` | the AccountType enum WITH THE SPACES REMOVED plus `NonPosting`; `list_accounts` reports the spaced form |
| `columns` | allowlisted keys; `tx_date` and `txn_type` are always requested; fewer than 25 in the request |
| `accounting_method`, `class`, `department`, `max_rows` | as above |

Default columns (QuickBooks's own default set, in order): `tx_date`, `txn_type`,
`doc_num`, `name`, `memo`, `split_acc`, `subt_nat_amount`, `rbal_nat_amount`.
Also requestable (documented, not yet seen live): `account_name`, `cust_name`,
`vend_name`, `emp_name`, `item_name`, `inv_date`, `is_adj`, `is_ap_paid`,
`is_ar_paid`, `is_cleared`, `quantity`, `rate`, `net_amount`, `tax_amount`,
`tax_code`, `klass_name`, `dept_name`, `create_date`, `last_mod_date`. Never:
`account_num` (an account number), `chk_print_state`, `create_by`,
`last_mod_by`. One section per account; the header's first cell carries the
account name and id; a sub-account section can arrive with rows and a total but
no header. Each posting row's `txn_type` cell carries the transaction id for
`get_transaction`.

## `report_aging`

One tool, four endpoints, four allowlists:

| `side` | `detail` | Endpoint |
|---|---|---|
| `payables` | `summary` | `reports/AgedPayables` |
| `receivables` | `summary` | `reports/AgedReceivables` |
| `payables` | `detail` | `reports/AgedPayableDetail` |
| `receivables` | `detail` | `reports/AgedReceivableDetail` |

| Parameter | Summaries | AP detail | AR detail |
|---|---|---|---|
| `aging_method` (`Report_Date`, `Current`) | yes | **no** | yes |
| `accounting_method` | no | yes (defaults to the company basis) | **no** |
| `report_date` | yes (not with `date_macro`) | yes | yes |
| `date_macro` | yes | no | no |
| `sort_order` | yes | no | no |
| `department` (a list of ids) | yes (requires tracking) | no | no |
| `vendor` (a list of ids) | payables only | yes | no |
| `customer` (a list of ids) | receivables only | no | yes |
| `start_duedate`, `end_duedate` | no | together, at most six months | together, at most six months |
| `past_due`, `aging_period`, `num_periods` | no | yes | yes |
| `columns` | no | side's allowlist | side's allowlist |

A combination the endpoint does not accept is refused before any request. A
due-date window given on one side only is refused (unbounded); with neither,
the detail report covers every open item as of `report_date`.

Summary columns: a keyless `Vendor`/`Customer` label (admitted by position and
type, published as `vendor`/`customer` with the party's id on the cell) and the
buckets `current`, `0`, `1`, `2`, `3`, `total`, each cell carrying its title
(`Current`, `1 - 30`, `31 - 60`, `61 - 90`, `91 and over`, `Total`). Detail
columns as observed: AP `tx_date`, `txn_type`, `doc_num`, `vend_name`,
`due_date`, `past_due`, `subt_neg_amount`, `subt_neg_open_bal`; AR `tx_date`,
`txn_type`, `doc_num`, `cust_name`, `due_date`, `subt_amount`, `subt_open_bal`;
plus the documented `memo`, `term_name`, `dept_name`, `create_date`,
`last_mod_date`, and per side `vend_comp_name` / `cust_comp_name`,
`sale_sent_state`. Never the address, email, telephone, contact and
customer-message columns.

`report_aging(payables)` on a company without accounts payable is an error via
the capability: the first refusal with fault 5030 records
`accounts_payable: unavailable` for this process and later calls refuse before
any request; a payables report that answers records it `available`.

## `run_report`

One tool over the 21 documented reports without a named tool. `report` is the
title of Intuit's page; the path sent is the one that page names, and five of
them differ (`AccountList`, `ClassSales`, `CustomerSales`,
`DepartmentSales`, `ItemSales`). The four `Aged*` paths belong to `report_aging`.
The enum is disjoint from the named tools: their reports are
refused here with a pointer to the tool.

| `report` | Path | Window | Detail | Macros | Takes |
|---|---|---|---|---|---|
| `AccountListDetail` | `AccountList` | dates, optional | no | none | `account_type`, `sort_order`, `columns` |
| `CashFlow` | `CashFlow` | dates or macro | no | 23 | `summarize_column_by`, `customer`, `vendor`, `class`, `item`, `department`, `sort_order` |
| `CustomerBalance` | `CustomerBalance` | `report_date` or macro | no | 23 | `customer`, `accounting_method`, `sort_order`, `summarize_column_by`, `department` |
| `CustomerBalanceDetail` | `CustomerBalanceDetail` | due-date window, at most six months, both or neither; `report_date` | yes | none | `customer`, `sort_order`, `aging_method`, `department`, `columns` |
| `CustomerIncome` | `CustomerIncome` | dates or macro | no | 23 | `customer`, `vendor`, `accounting_method`, `class`, `sort_order`, `summarize_column_by`, `department` |
| `InventoryValuationDetail` | `InventoryValuationDetail` | dates or macro, required | yes | 23 | `columns` |
| `InventoryValuationSummary` | `InventoryValuationSummary` | `report_date` or macro | no | 23 | `item`, `sort_order`, `summarize_column_by` |
| `JournalReport` | `JournalReport` | dates or macro, required | yes | 23 | `sort_order`, `columns` |
| `ProfitAndLossDetail` | `ProfitAndLossDetail` | dates or macro, required | yes | 23 | `customer`, `vendor`, `account`, `account_type`, `accounting_method`, `class`, `payment_method`, `employee`, `department`, `sort_order`, `columns` |
| `SalesByClassSummary` | `ClassSales` | dates or macro | no | 23 | `customer`, `accounting_method`, `class`, `item`, `summarize_column_by`, `department` |
| `SalesByCustomer` | `CustomerSales` | dates or macro | no | 23 | as ClassSales plus `sort_order` |
| `SalesByDepartment` | `DepartmentSales` | dates or macro | no | 23 | as SalesByCustomer |
| `SalesByProduct` | `ItemSales` | dates or macro | no | 23 | as SalesByCustomer |
| `TaxSummary` | `TaxSummary` | dates or macro | no | 23 | `agency_id` (**required**), `accounting_method`, `sort_order` |
| `TransactionListByCustomer` | `TransactionListByCustomer` | dates or macro, required | yes | 33 | `customer`, `transaction_type`, `source_account_type`, `cleared`, `docnum`, `memo`, `payment_method`, `department`, `sort_order`; no `columns` (below) |
| `TransactionListByVendor` | `TransactionListByVendor` | dates or macro, required | yes | 33 | as ByCustomer with `vendor` for `customer`, plus `columns` |
| `TransactionListWithSplits` | `TransactionListWithSplits` | dates or macro, required | yes | 23 | `docnum`, `payment_method`, `source_account_type`, `transaction_type`, `sort_order`, `columns` |
| `TrialBalance` | `TrialBalance` | dates or macro | no | 23 | `accounting_method`, `sort_order`, `summarize_column_by` |
| `VendorBalance` | `VendorBalance` | `report_date` or macro | no | 23 | `vendor`, `accounting_method`, `sort_order`, `summarize_column_by`, `department` |
| `VendorBalanceDetail` | `VendorBalanceDetail` | due-date window, at most six months, both or neither; `report_date` | yes | none exposed | `vendor`, `accounting_method`, `sort_order`, `department`, `columns` |
| `VendorExpenses` | `VendorExpenses` | dates or macro | no | 23 | `customer`, `vendor`, `accounting_method`, `class`, `sort_order`, `summarize_column_by`, `department` |

Every report accepts only the parameters its row lists, a subset of
what Intuit documents for it; a parameter a report does not take is refused by name before any request, and the network policy
refuses it again per path. CashFlow and TrialBalance are not exempt:
CashFlow takes no `accounting_method`, TrialBalance does and defaults to the
company basis. A detail member needs its window, dates or a macro, and the
window is at most six calendar months, on explicit dates and on the resolved
macro alike; a summary report may omit it, and the two balance details take due
dates only and may omit them. `max_rows`, `accounting_method`'s
default, the class and department capability checks and the report contract
are as for the named tools. TaxSummary stays in the enum, though it has never
returned rows here: sandbox answers fault 5020, and it needs a tax agency id.

**TransactionListByCustomer takes no `columns`**, though its page lists them.
In production (observed 2026-09-28) Intuit answered it with fault 10000, an
internal error inside an HTTP 200, whenever columns were sent, even a single
column, and answered it without them. The sandbox companies accept the same
columns, and TransactionListByVendor takes its columns in production. So it is
sent none, and returns its own default set: on both sandbox companies
`tx_date`, `txn_type`, `doc_num`, `is_no_post`, `memo`, `account_name`,
`amount`. A caller's
`columns` is refused before any request with that reason, and the network
policy refuses `columns` on its path.

Columns follow what each report returned live on 2026-09-24
(`reports.REPORT_SHAPES`), plus the documented requestable keys each page lists, named there as
documented-not-seen. A custom `columns` list gains the routing keys `tx_date`
and `txn_type` only where the report's own vocabulary has them (every
transaction detail report, never AccountListDetail), and the list that would go out, a default one included, is checked
against that vocabulary before any request. The privacy allowlist governs the request and the returned
cells: the address, email, telephone, message, tracking and
account-number columns the aging, balance-detail, transaction-list and journal
pages document (`bill_addr`, `ship_addr`, `deliv_addr`, `cust_bill_email`,
`cust_msg`, `cust_tel`, `cust_phone_other`, `vend_bill_addr`, `vend_pri_cont`,
`vend_pri_email`, `vend_pri_tel`, `acct_num_with_extn`, `account_num`, ...) can
be neither requested nor returned, and `create_by`/`last_mod_by` (user
identities) are not allowlisted. A detail report with `columns` sends its
default set when none is asked for, and always `tx_date` and
`txn_type`. TrialBalance declares no ColKey on any column: the label is
admitted by position and its `Debit` and `Credit` Money columns by title,
published as `debit` and `credit` with `admitted_by: title`. TaxSummary's
column shape is documentation-derived (never seen live) and every result says
so in `disclosures`.

## `list_reference_data`

`kind` is one of `classes`, `departments`, `items`, `terms`, `payment_methods`,
`tax_codes`, `tax_rates`; `active` follows `list_vendors` (`true` by default,
`null` for both); `limit` trims after the walk. Each kind is walked by the
shared count-anchored, deduplicating walker (`count`, `truncated`,
`duplicates_dropped`, `pages_walked`, the walk window and
`consistency: non_snapshot`) and projected through its allowlist
(`projections.REFERENCE_SPECS`):

| `kind` | Entity | Kept |
|---|---|---|
| `classes` | Class | id, name, active, sub_class, parent id, fully_qualified_name |
| `departments` | Department | id, name, active, sub_department, parent id, fully_qualified_name |
| `items` | Item | id, name, fully_qualified_name, active, type, sku, description, purchase_description, taxable, unit_price, purchase_cost, qty_on_hand, track_qty_on_hand, inv_start_date, sub_item, level, parent id, class_ref, income_account, expense_account, asset_account |
| `terms` | Term | id, name, active, type, due_days, discount_days, discount_percent, day_of_month_due, due_next_month_days, discount_day_of_month |
| `payment_methods` | PaymentMethod | id, name, active, type |
| `tax_codes` | TaxCode | id, name, active, taxable, tax_group, hidden, config_type, description, and from the two rate lists only the rate ids |
| `tax_rates` | TaxRate | id, name, active, rate_value, special_tax_type, display_type, description, agency id |

MetaData, SyncToken, LastModifiedByRef and every field not named do not
travel. Descriptions are the item's or code's own labels: free text from the
company file. Item, Term, PaymentMethod, TaxCode and TaxRate were checked
against live responses on 2026-09-24; Class and Department returned
zero rows there because the sandbox company has those features off, so their
projections follow Intuit's pages and every result for them says
`documentation_derived: true`.
A company whose Preferences turn class or department tracking off gets an
empty list with `feature_enabled: false` and a note, never an error. The
seven entities are also served by `run_query` through the same projections.

## `list_attachments`

Attachable metadata through the ordinary accounting client: `file_name`, `content_type`, `size`, `note`,
`category`, `tag`, `refs` (the linked object's `entity_type` and `entity_id`,
routable through `get_transaction` for a served type, and `include_on_send`),
`created_at`, `last_updated_at`. **No link.** `TempDownloadUri` and
`ThumbnailTempDownloadUri` (temporary signed URLs),
`FileAccessUri` and `ThumbnailFileAccessUri` (full paths), `Lat`, `Long` and
`PlaceName` never leave this process. `transaction_type` and `transaction_id`
filter together or not at all, as the documented query does. The
result says whether `get_attachment` is available on this server
(`download_available`, `download_note`). An attachment can be linked to a
different transaction type from the one it was entered on: in sandbox, one
attachment entered on an expense that QuickBooks applied to an open bill was
linked to the resulting BillPayment ([api-findings.md](api-findings.md), item
17). Filter on the type and id the row reports.

## `get_attachment` (opt-in)

`entity`, `attachable_id` (an id from `list_attachments`). Present only when the server was started with `--allow-documents` (or
`QBO_ALLOW_DOCUMENTS=1`); the default surface is the eighteen tools above.
It refuses before any request unless a storage host is pinned in
`QBO_ATTACHMENT_HOSTS` (comma-separated, exact or `*.suffix`; or
`--attachment-host`, repeatable). Nothing is pinned by default. The host a
sandbox `TempDownloadUri` named was observed on 2026-09-25:
`financialdocument.platform.intuit.com`, and
every no-host refusal names it with that qualifier. Production has not been
observed and may name another host. To find it, pin a placeholder host and call this tool once: the refusal names the host the link asked for, and no storage host is contacted. It reads the Attachable, decides from
the recorded content type and size whether it will fetch the file, and hands
the temporary link to a separate download client:

- no bearer, cookie or proxy authorization on any request; the client and
  its transport read nothing from the environment (no proxy, CA file or TLS
  key log), and the client never consults a cookie jar;
- https on the default port to a pinned host only; an IP literal is never a
  pinned host; redirects are followed by this package, at most three hops,
  each destination put through the same policy first;
- the pinned name is resolved before the request and every address must be
  globally routable: private, loopback, link-local, unspecified and reserved
  destinations are refused (a pre-check, not a proof: the connection resolves
  again);
- a supported-MIME allowlist (`application/pdf`, `image/png`, `image/jpeg`,
  `image/gif`, `text/plain`, `text/csv`) decided from the recorded type, the
  response header required to agree, the bytes checked against each type's
  own signature;
- the stream capped at three quarters of the 10 MiB output cap, so the
  base64 blob stays under it; the recorded size is refused before the request
  when it is over, and a body of any other length than the recorded size is
  refused. A size written as text is read as its number; one that is not a
  non-negative whole number refuses the download before the request. With no
  recorded size, only the stream cap bounds the body.

Against a loopback API base outside production, which is the test setup, a
pinned loopback IP literal is admitted over http on any port, and a pinned name
may resolve to loopback. A pinned name still needs https and the default port.

The result is two blocks: the metadata as a text block (and as the structured
content: the attachable, `content_type`, `bytes`, `base64_length`, `sha256`,
`download_host`, `redirects_followed`, `resource_uri`), then the file as an
embedded blob resource at `resource_uri` with its MIME type. A refusal says
which gate refused and names the host at most, never the signed URL. With the
observed host pinned, a sandbox fetch on 2026-09-26 passed every gate, and the
returned file matched, by sha256, the one attached in the QuickBooks UI. A deployment where
a gate refuses can run on `list_attachments` alone.
