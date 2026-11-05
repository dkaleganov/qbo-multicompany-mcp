# Changelog

## 0.1.1

A listing release: the server behaves as in 0.1.0. In the server only text changed, and in the tool list only
the tool descriptions and the `entity` parameter's description.

- Each tool's description says which sibling fits a nearby need, following the "Choosing a tool" table in
  [docs/tools.md](docs/tools.md).
- The `entity` parameter's description carries the rule that the key is exact, with no default company, so the
  tool descriptions no longer repeat it.
- `server_info` lists what it reports more plainly, and `list_reference_data` drops a field list its output
  schema already carries.
- `server.json` for the MCP Registry, and `glama.json`. The repository hygiene test accepts the two new hosts
  they name, `pypi.org` and `glama.ai`.
- The README names the server for the MCP Registry, uses absolute links so they work on PyPI, and shows how to
  run the package with `uvx`.

## 0.1.0

First public release: a read-only stdio MCP server that serves several QuickBooks Online company files
to one AI session, every call routed by an explicit `entity` key. Eighteen tools by default and one
opt-in ([docs/tools.md](docs/tools.md)):

- **Diagnostics, no request:** `list_entities`, `server_info`.
- **Company and lists:** `get_company_info`, `list_accounts`, `list_vendors`, `list_customers`,
  `list_reference_data` (classes, departments, items, terms, payment methods, tax codes, tax rates).
- **Transactions:** `list_transactions`, `get_transaction`, `run_query`, `list_changes`,
  `summarize_vendor_payments` (candidate totals only).
- **Reports:** `report_profit_and_loss`, `report_balance_sheet`, `report_general_ledger`, `report_aging`,
  and `run_report` over the other 21 documented reports.
- **Attachments:** `list_attachments` (metadata, never a link) and the opt-in `get_attachment`, registered
  only with `--allow-documents` and refusing until a storage host is pinned in `QBO_ATTACHMENT_HOSTS`.

Underneath: a per-request network policy that makes read-only a property of the code; an encrypted token
store with a journal and cross-process locking; the auth CLI and the keepalive with both expiry clocks; list
walks that check their row count; a report parser that discloses every anomaly instead of guessing; and
480 mutation-gate entries that each delete a safety behaviour and require the tests to notice.

### Verified live in sandbox

Over real stdio, against two sandbox companies, from 2026-09-21 to 2026-11-04:

- OAuth consent per company, and token refresh: a new refresh-token value restarts the inactivity clock, and
  the hard expiry stays where the consent set it.
- Two keepalive processes refreshing at once, with the lock serialising them and the token chain intact.
- Every tool, including `run_report` on 20 of 21 reports. TaxSummary has never returned rows: sandbox answers
  fault 5020, and this server reports that as a plain refusal.
- On 2026-11-04, this release's code: every default tool on both companies, and `run_report` on the same 20
  reports.
- `get_attachment` through every gate: the returned file matched, by sha256, the one attached in the QuickBooks UI.

### Verified live in production

Read-only, over real stdio, on 2026-09-28:

- The guard's name match and every default tool.
  `run_report` ran on every report except TaxSummary.
- What production showed that sandbox didn't is in [docs/api-findings.md](docs/api-findings.md), item 19.

### Tests only

Built and tested against a loopback mock or synthetic fixtures, and never observed live:

- Payables aging on a company without accounts payable (fault 5030).
- Class and Department rows, and class or department report filters.
- The multi-currency refusal in `summarize_vendor_payments`, and a Customer or Employee payee on a purchase.
- Change-data-capture saturation and paging, which needs more than 1,000 changed objects, and a change
  response in any shape but the documented one.
- An object where Intuit documents one value: in a change, a payment, or a report's header or row group.
- A change object with no id, or with metadata that isn't an object.
- A report row with nothing this server reads, and two report columns whose keys would be the same.
- Token-endpoint failures, response loss, recovery and quarantine, and a lock wait that leaves too little of
  the budget to begin a refresh: fault injection only. Among them, a success whose tokens are blank.
- A token response that names a different company than the callback did: refused, and the pair revoked.
- A refresh that never left the machine, because its client couldn't be built or the policy refused it.
- `revoke` on a stored row this build can't read, and a store that fails partway through `revoke --all`.
- A `revoke` whose HTTP client can't be built, a consent that lands during a revoke, and an interrupted command.
- A `revoke --all` answer that sets a cookie, and a pair stored mid-revoke under another app's credentials.
- A log handler installed before this server's own, or added later on one of its loggers; a library that
  replaces the log record factory; a logger class that makes its own records; and a malformed exception passed
  to a logger.
- `revoke` against Intuit's live endpoint.
- Two rate limiters sharing one counters file, and a counters file they can't read or write.
- A callback port held by another process on the IPv6 loopback only.
- A connection to the callback listener that idles, sends a byte at a time or arrives in a flood.
- Ten feature-gated TransactionList columns and the documented report columns no live company returned.

### Not done

- `get_attachment` in production. The storage host there hasn't been observed.
- Transaction rows through `run_query` and `list_changes`. Both refuse them, because no projection is registered
  for those routes. `COUNT(*)`, `list_transactions` and `get_transaction` work.
- A hand reconciliation of candidate vendor payments against a bank ledger.
- Any test on Linux.
