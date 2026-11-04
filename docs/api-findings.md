# QuickBooks API findings

What the QuickBooks Online Accounting API did in live probes, and how this server
handles it. Intuit's documentation doesn't cover most of this. The probes this
file reports ran on two sandbox companies between 2026-09-21 and 2026-11-04, and
the production findings are from 2026-09-28. Observations say which environment they came from.
Numbers here come from sandbox sample data.

| # | Question | Status |
|---|---|---|
| 0 | How do report filters handle a bad value? | **[Settled: three classes, per parameter](#validation-is-not-uniform)** |
| 1 | Does TransactionList accept `accounting_method`? | Not established: no observed effect in one window. Not sent. |
| 2 | Entity-name case policy | **Resolved by design** |
| 3 | Per-entity filterable and sortable fields | **Partly settled** |
| 4 | Does `Active IN (true, false)` work per entity? | **Partly settled: works on Vendor and Customer** |
| 5 | `PaymentType` / `PayType` value sets | **Settled: every documented value confirmed** |
| 6 | Does `CreditChargeInfo` appear on payment reads? | **Settled: absent in sandbox** |
| 7 | How are deletions marked in change data capture, what shape is a response, and does it page? | Partly settled |
| 8 | `cleared` and `source_account_type` value sets | **Settled: every documented value confirmed, pinned** |
| 9 | TransactionList column keys and types | **Settled for the 16 columns sandbox returns**; 10 feature-gated columns unconfirmed |
| 10 | A stable sort key for the payment walks | **Settled: `TxnDate` honoured** |
| 11 | The live column shapes of the 29 reports | **Settled: five shapes the samples did not show, all handled** |
| 12 | A report the company cannot run | **Settled: fault 5020, a fixed error** |
| 13 | `summarize_column_by` entity groupings | **Settled: columns keyed by entity id** |
| 14 | What the undefined date macros mean | **Settled: observed in sandbox** |
| 15 | Percent columns on SalesByProduct | **Settled: declared Money, text `'30.69 %'`, read as a percent** |
| 16 | Reference entities: `ORDERBY Name`, disabled features | **Settled: accepted and in order; off features are empty** |
| 17 | The attachment storage host and download | **Settled in sandbox**; production unobserved |
| 18 | The refresh token's hard expiry | **Settled: reported on request; a refresh does not restart it** |
| 19 | What production showed that sandbox did not | Three findings |

---

## Validation is not uniform

**One parameter's validation behaviour is no evidence for another's.** Three
classes, observed in sandbox:

| What is wrong | What the endpoint does | What the caller sees |
|---|---|---|
| Unknown parameter **name** | discards it | **everything** |
| **Enumerated** value, unaccepted | `HTTP 400 Invalid Enumeration : <value>` | a loud, specific error |
| **Id or free-text** value, no match | `200`, zero rows | **nothing** |

Enumerated: `transaction_type`, `source_account_type`, `cleared`.
Id or free text: `vendor`, `customer`, `docnum`, `memo`.

Each documented filter narrowed the rows in sandbox. A probe over one window
says nothing about later responses, so results carry `filters_sent`, what was sent, and never
a claim that a filter was applied.

### Ids and free text fail silently

An id's **shape** is validated and its **existence** is not:

- `vendor=not-an-id` → `400 Invalid Reference Id`
- `vendor=999999999` (well-formed, nonexistent) → `200`, zero rows

`docnum` and `memo` are free text, with nothing to validate at all. For these
four, an empty report is indistinguishable from a value that matched nothing.
So `list_transactions` re-checks a filtered request that returns zero rows against
the same window unfiltered, and the resulting `interpretation` names the id and
free-text filters specifically rather than implying any filter can mismatch quietly.

### Enumerated values are pinned

An unaccepted enumerated value produces a `400`, so **a `200` proves
membership** whether or not the window holds matching rows. It doesn't make a
vocabulary exhaustive, since a value nobody tried could still be accepted, so
each pinned list is the set proven to work and may be narrower than what the API
allows. Each is an enum in the published input schema, so this server refuses a
value outside it before any request, and it never becomes a runtime `400`.

### `transaction_type`: a third vocabulary

Not the display labels in the `txn_type` column. Not the entity names used by
`run_query` and `get_transaction`.

**Accepted:** `Bill`, `Charge`, `Check`, `CreditCardCharge`, `CreditMemo`,
`Deposit`, `Estimate`, `Invoice`, `JournalEntry`, `SalesReceipt`

**Refused** (`400`), despite appearing in the data or in this server's own
tools:

- *Display labels from the `txn_type` column:* `Payment`, `Expense`,
  `Cash Expense`, `Credit Card Expense`, `Credit Card Credit`,
  `Bill Payment (Check)`, `Bill Payment (Credit Card)`, `Journal Entry`,
  `Sales Receipt`, `Credit Memo`, `Sales Tax Payment`, `Purchase Order`,
  `Time Charge`, `Billable Expense Charge`, `Inventory Qty Adjust`, `Refund`
- *Entity names from `run_query` and `get_transaction`:* `Purchase`,
  `BillPayment`, `Transfer`, `VendorCredit`

`Estimate` is accepted here although this server doesn't serve Estimate as a
queryable entity. That gap is documented rather than closed, since adding it would
mean deciding which of its fields are safe to return.

This server sends `minorversion=75`. With and without it, `Purchase` and `Expense`
answered `400` and `Bill` answered `200`.

### `source_account_type`: the AccountType enum without spaces

`Bank`, `AccountsReceivable`, `OtherCurrentAsset`, `FixedAsset`, `OtherAsset`,
`AccountsPayable`, `CreditCard`, `LongTermLiability`, `OtherCurrentLiability`,
`Equity`, `Income`, `CostOfGoodsSold`, `Expense`, `OtherExpense`, `OtherIncome`

`AccountsPayable` filters; `Accounts Payable` is a `400`. **The chart of
accounts reports the spaced form**, including this server's own `list_accounts`,
whose `account_type` passes it through verbatim. So results carry
`filterable_source_account_types`, the values that filter, as they carry
`filterable_transaction_types`.

`list_accounts`' own `account_type` filter takes the spaced form. All fifteen
spaced spellings answered 200 on 2026-09-26, and the de-spaced `FixedAsset`
answered `400`.

### `cleared`

`Cleared`, `Uncleared`, `Reconciled`, `Deposited`. Confirmed live, not merely
documented.

## Sandbox is not production

`ORDERBY Id` **is honoured in sandbox**. Intuit's change log says `Id` stopped
being sortable in production on 2026-01-27. Production wasn't probed for this.

It stays rejected, and the rejection message says why, so a sandbox test doesn't
lead anyone to re-enable it.

The same caution applies to everything below marked *settled*: a value set
confirmed in sandbox is a good default, not a contract. Unlisted values are
still surfaced as `unclassified` rather than bucketed.

---

## 1. `accounting_method`: not sent; its effect is not established

Not sent, and not offered as a parameter. Whether TransactionList honours it is
**not established**: in sandbox on 2026-09-24, sending it made no observed
difference to the rows in one window. That shows neither that it's ignored nor
that it's honoured, so this server claims no cash-versus-accrual distinction on
TransactionList. A parameter that may do nothing while appearing to work is worse
than an absent one.

It's absent from
`netpolicy.ACCOUNTING_QUERY_KEYS_BY_TAIL["reports/TransactionList"]`, so the
network policy refuses any request carrying it.

## 2. Entity-name case: resolved by design

Intuit's query syntax page calls entity names case-sensitive in its parameter
table and case-insensitive further down. The parser accepts either spelling and
always emits the canonical one upstream, which is correct under both readings.

## 3. Filterable and sortable fields: partly settled

**Confirmed honoured by live `ORDERBY` probes:**

| Entity | Field |
|---|---|
| `Purchase` | `TxnDate` |
| `BillPayment` | `TxnDate` |
| `Vendor` | `MetaData.LastUpdatedTime` |
| `Vendor` | `DisplayName` |

`DisplayName` remains the default name-list sort even though
`MetaData.LastUpdatedTime` is confirmed sortable: QuickBooks enforces
`DisplayName` unique per company, and a timestamp is not unique, so two rows
sharing one can straddle a page boundary.

Still unconfirmed: sortability on `Customer` and on the other transaction
types, and the filterable set generally. No address, contact, tax id or note field
is filterable on any entity, and a test asserts that across the whole registry:
a filterable field is a read oracle even when it's never a returned field.

## 4. `Active IN (true, false)`: partly settled

Documented as the way to include inactive rows. **Works on Vendor and Customer**
(sandbox, 2026-09-21). `list_reference_data` sends it for `active=null` on the
seven reference kinds, where it hasn't been observed live.

## 5. `PaymentType` and `PayType`: settled, every documented value confirmed

- `Purchase.PaymentType`: **Cash, Check, CreditCard**, each confirmed.
- `BillPayment.PayType`: **Check, CreditCard**, each confirmed. There is no
  documented cash bill payment.

An unlisted value still produces an `unclassified` row rather than a guess, per
[Sandbox is not production](#sandbox-is-not-production).

Also observed: **2 of 35 sandbox purchases have no payee.** A non-empty
`needs_review.no_payee` list doesn't indicate a fault.

## 6. `CreditChargeInfo`: settled, absent in sandbox

Absent from all 16 sandbox payments. That says little about production: a
sandbox company has no real card on file to return. It stays excluded from the
transaction projection whatever the answer, because it carries card holder
name, number, expiry and authorisation codes.

## 7. Deletions, response shape and paging in change data capture: partly settled

Deleted objects are read from a `status` of `"Deleted"` and returned with no
projected row. Whether change data capture pages, and how, is still unconfirmed,
so the aggregate 1,000-object count is treated as a saturation signal and
`possibly_truncated` is set rather than assuming one response is the whole
answer.

The response shape was observed on both sandbox companies for the default types
(Vendor, Customer and Account). On 2026-11-02 each response held one block with
one entry per requested type: `{}` for a type with no changes, and a type's
objects with `startPosition` and `maxResults` beside them. On 2026-11-04 both
companies' responses passed `list_changes`' check for that shape. Production and
the other types are unobserved. A response whose entries don't account for each
requested type once, or hold anything but a type's objects and its paging
fields, is refused whole.

## 8. `cleared` and `source_account_type`: every candidate accepted, pinned

`cleared`: 4 candidates, 4 accepted. `source_account_type`: 15 candidates, 15
accepted, the AccountType enum with spaces removed. Both are pinned enums: see
[Validation is not uniform](#validation-is-not-uniform).

## 9. TransactionList columns: settled for what sandbox returns

With no `columns` parameter the report returns **9 columns**, with
**`subt_nat_amount`** as the amount key. `columns` is **honoured exactly**, so
a requested subset really is what comes back.

On 2026-09-23, on both sandbox companies, a request sent directly with all 26
allowlisted keys (more than the tool itself sends) answered 200 with 16 columns. 14 matched the documentation
samples. `create_date` and `last_mod_date` arrived typed **`TimeStamp`**, a type
in no sample. The parser reads `TimeStamp`, and the 16 columns parse with no
anomaly.

Still open: **10 of the 26 keys never came back** (the department, class,
currency, home-currency, tax and split columns), because neither sandbox company
has those features on. Their live types are unconfirmed. An unrecognised key is
excluded from request and response and named in `excluded_column_keys`, so a
different live vocabulary shows as fewer columns, not a leak.

## 10. A stable sort key for the payment walks: settled

`TxnDate` is honoured on both `Purchase` and `BillPayment`. It's **not unique**,
since many transactions share a date, so a page boundary can still repeat or
skip a row. The complete-or-loud walk compares unique ids after deduplication
against a `COUNT(*)` anchor, so a skip or repeat fails loudly instead of
returning a plausible wrong answer.

Whether `MetaData.LastUpdatedTime` is sortable on transaction entities, which
would give a finer key, is unconfirmed.

## 11. The live column shapes of the reports: settled

On 2026-09-24, in sandbox, one request went out per report, and 28 of 29
answered 200 (TaxSummary is item 12). The responses showed five things the
documentation samples don't, each handled by the parser before the anomaly
rules run:

- nested columns (ItemSales groups seven leaves under one parent; rows carry one
  cell per leaf);
- period columns with dynamic keys under `summarize_column_by` (`Jan 2026`,
  `Jan - Mar, 2026`), admitted by their StartDate/EndDate metadata;
- keyless label columns on fourteen reports, and none at all on TrialBalance;
- no ColType at all on an empty report;
- a `Quantity` type.

Nine report paths differ from their page titles (`ClassSales`, `CustomerSales`,
`DepartmentSales`, `ItemSales`, `AccountList`, and the four `Aged*` paths).
Every report tool ran over stdio against both sandbox companies on 2026-09-25.
Apart from items 14 and 15, the results were clean.

## 12. A report the company cannot run: settled

TaxSummary answered **HTTP 400 with fault 5020** ("Permission Denied") on
2026-09-24, and again through `run_report` on 2026-09-25. It's a fixed,
actionable error naming the code, never an empty report. 5020 is distinct from
5030 (the company's plan lacks the feature) and 6190 (the subscription).

## 13. `summarize_column_by` entity groupings: settled

Observed 2026-09-25 in sandbox. By Customers, Vendors or ProductsAndServices,
ProfitAndLoss returns one Money column per entity **keyed by the entity's id**
with its name as the title, plus `not_specified` and `total`, and no period
metadata. By Classes, Departments or Employees, with those features off, only
`not_specified` and `total`. These columns are admitted by that shape under the
request that named the grouping, each carrying `entity_id` and `entity_type`.

## 14. The date macros no page defines: settled

Observed in sandbox on Friday 2026-09-25, from the `start_period` and
`end_period` QuickBooks returned for each macro. The resolver follows these
readings.

| Macro | QuickBooks window | Reading |
|---|---|---|
| This Week | 09-20..09-26 | A week starts on Sunday |
| Last Fiscal Quarter-to-date | 04-01..06-25 | Ends at the same calendar position in the previous period |
| Last Month-to-date | 08-01..08-25 | Ends at the same calendar position in the previous period |
| Next 4 Weeks | 09-27..10-24 | Starts on the next week's Sunday and runs four weeks |

Unobserved and labelled in the code: what "Next 4 Weeks" does when today is a
Sunday. None of these readings can move a window across the six-month or
200-day limit.

## 15. Percent columns on SalesByProduct: settled

Observed 2026-09-25 on both sandbox companies. ItemSales declares `% of Sales`
(`PercentSales`) and `Gross Margin %` (`GrossMarginPerc`) as **Money**, and the
cells read `'30.69 %'`, a space before the sign. Read as money, each such cell
would be malformed, so a Money column whose key ends in `Perc` or `PercentSales`,
or whose title carries `%` (never an entity grouping's column by its title, which
is the entity's name), is read as a percent: exact decimal, `unit: percent`, no
currency, no anomaly. Anything else in such a column is still disclosed as
malformed. Gross Margin % was empty on every row here, as was COGS, so its filled
shape is inferred from its twin.

## 16. Reference entities: settled

Observed 2026-09-25 on both sandbox companies. Every kind's `ORDERBY Name ASC`
answered 200, and the five non-empty kinds (items, terms, payment methods, tax
codes, tax rates) came back in name order. Each walk was one page, so paging
under the sort isn't shown; the count anchor would catch a broken walk. Classes
and departments, with the features off, are an empty list with
`feature_enabled: false`, never an error.

## 17. The attachment storage host and download: settled in sandbox

Observed in sandbox on 2026-09-25. The temporary link's host is
**`financialdocument.platform.intuit.com`**; Intuit's page doesn't name it. An
attachment added to an expense in the QuickBooks UI was linked to the
**BillPayment** that expense applied to, not to a Purchase. The Attachable had no thumbnail link.

On 2026-09-26, with that host pinned, `list_attachments` and `get_attachment`
ran over stdio. The returned file matched that attachment by sha256, no link
appeared in any field, and no redirect occurred. Production is unobserved: read
one attachment's host there before pinning.

## 18. The refresh token's hard expiry: settled

Observed in sandbox on both companies. With the documented opt-in header, every
token response carries `x_refresh_token_hard_expires_in`. The hard expiry is
five years from the consent, and a refresh doesn't move it (2026-09-24). The
100-day inactivity clock restarts when Intuit issues a new refresh-token value,
which its Authorization FAQ says happens at most once per 24 hours. A refresh
inside those 24 hours returns the same value and expiry (2026-09-26).

## 19. What production showed that sandbox did not

Observed 2026-09-28.

- **TransactionListByCustomer faults on `columns`.** Any value, even a single
  column, got HTTP 200 carrying SystemFault 10000, an internal error on Intuit's
  side. Without `columns` the report answers. The sandbox companies accept the
  same columns, and TransactionListByVendor takes its columns in production. So
  this server sends none and refuses a caller's with the reason.
- **A general ledger over a near-empty window can arrive untyped.** One such
  response came back with no ColType on any column, while a wider window on the
  same company arrived typed and clean. The rows are still returned, each untyped
  cell as text with no amount, and the result is partial.
- **`payment_method` takes each company's own names.** A known name is accepted
  and a made-up one gets HTTP 400, so there's no closed enum to pin. Every id
  seen in production was a run of digits, which is the rule this server applies
  to report id filters.

---

## Verified without credentials

- Both Intuit OpenID discovery documents, confirming the pinned authorize,
  token and revocation endpoints for sandbox and production.
- `invalid_client` handling against the live token endpoint: a configuration
  error, exit 2, stored credential untouched rather than marked
  `needs_consent`.
