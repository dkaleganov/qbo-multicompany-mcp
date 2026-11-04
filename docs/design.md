# Design

How this server keeps its promises, and where each promise stops. Where it
matters, statements about Intuit's behaviour here and in the source say whether
Intuit documents them, whether they were observed live, or that they're
assumptions.

## Read-only, and what backs it

Intuit issues one OAuth token pair per company file (a "realm"), and its
`com.intuit.quickbooks.accounting` scope covers **reading and writing alike**.
Intuit's scope list offers one Accounting API scope and no read-only variant, so
there's nothing to ask for that would make a credential read-only.

So read-only is a property of *this code*, and what enforces it is
[`netpolicy.py`](../src/qbo_multicompany_mcp/netpolicy.py): every request is
inspected as a fully built `httpx.Request` **immediately before it is sent**
(method, origin, path, realm and query) against a per-class policy that denies
anything it doesn't recognise.

| Class | Allowed |
|---|---|
| Accounting | `GET` only, to the selected Intuit origin, the exact bound realm, an allowlisted path, with no body, no method-override header and a `Host` matching the URL |
| Token refresh | `POST` to the fixed token endpoint |
| Exchange, revoke | `POST` to their fixed endpoints, auth CLI only: the CLI is their one caller, and a test keeps it that way |
| Discovery | `GET` to its fixed origin; may confirm pinned values, never rewrite a secret-bearing destination |
| Attachment (opt-in) | `GET` with no bearer, cookies or proxy auth, over https to a storage host the operator pinned, never an IP literal; private, loopback and link-local destinations refused; every redirect destination re-checked |

The request goes where the policy checked it. Every HTTP client this package
uses is built in `netpolicy.py` and ignores the environment's network settings: no
proxy (the `*_PROXY` variables, or on macOS the system settings), no CA file
or directory (`SSL_CERT_FILE`, `SSL_CERT_DIR`), only certifi's bundle. TLS session-key
logging is off: Python's `ssl` module still opens a file `SSLKEYLOGFILE` names, and no
secret reaches it. A path it can't open stops the client being built. A proxy inherited from a shell, with a CA to match, could otherwise
read the bearer after every check had passed.

The accounting path allowlist is exactly `companyinfo/{realm}`, `preferences`,
`query`, `cdc` and twenty-nine reports by exact name (`ACCOUNTING_PATH_TAILS` in
`netpolicy.py`): the eight the named tools use and the twenty-one `run_report`
serves. Reports are allowlisted by exact name, never by prefix, and each carries
its own query-parameter allowlist.

A custom or loopback `--api-base` is **test-only**, and three independent checks
keep a real credential away from one: it's refused outright in production; it
never receives a value this process actually holds (the client secret, or any
token received from Intuit or read from the store, matched by exact value); and
failing both of those, it accepts only a credential that couldn't be a real
Intuit token by its shape. The shape rule is only the fallback: it
admits a UUID, which the first two don't care about.

There is also an AST test over `src/`. It's **regression protection, not
proof**: it can see that a literal `"GET"` was written at a call site, but it
can't see what a variable holds at run time, and `.send()` takes a request
object rather than a method string at all. The runtime policy is where the
guarantee lives.

## Results and errors

### The shared list vocabulary

Every walked list (accounts, vendors, customers, reference data, attachments and
`run_query`) returns `count`, `rows`, `truncated` and `duplicates_dropped`, plus
`pages_walked`.

### Walks are complete-or-loud, and never claim a snapshot

A paged read is anchored on a `COUNT(*)` carrying the same `WHERE`, and the
**unique ids after deduplication** must equal it. Unique ids rather than a raw
row count, because a duplicate on one page boundary and a skip on another
leaves the length correct and the answer wrong in two directions.

What that can't give you is isolation. Count equality doesn't notice someone
editing an amount between page two and page three, because that changes no
cardinality. So every walk reports `walk_started_at`, `walk_finished_at` and
`consistency: non_snapshot`.

### `run_query`

```
SELECT (* | COUNT(*)) FROM <entity>
    [WHERE <cond> [AND <cond>]*] [ORDERBY <field> [ASC|DESC]]
    [STARTPOSITION n] [MAXRESULTS n<=1000]
```

The statement is parsed in full and a canonical one is rebuilt from the parse
tree; your text is never forwarded. No field lists: the documented grammar offers
only `SELECT *` and `COUNT(*)`, so this parser doesn't accept one. `Id` takes only
`=` and `IN`. Conditions join with `AND` only. `ORDERBY Id` is rejected. Filterable
and sortable fields are tabled **per entity**, and no address, contact, tax id or note
field is filterable anywhere: a filterable field is a read oracle even when it's
never a returned field.

This package is **`GET`-only by product choice**. Intuit also documents a read-only
`POST /query` that carries the statement in a body, so not every upstream `POST`
mutates data, but one `GET`-only path is a boundary that can be stated in a
sentence and enforced in one place. An over-long statement is an explicit error,
never a quiet `POST` fallback.

### A partial report says so first

An empty rows array never establishes that there is no data, and nothing becomes
a silent zero or null. A problem **confined to a cell or a row** (a malformed
amount, a row with too few or too many cells, a row with nothing this server reads,
rows held where it doesn't read them, anything else a row, its header or its total
holds that it doesn't read, a published column of a type this server doesn't
recognise) returns a **partial**
result:

- the affected cell keeps its original text and carries no amount;
- every problem is listed in `anomalies`, with its row, column key and kind;
- `completeness` reads `partial` while any exists, and a clean report reads `unknown`,
  because nothing in a report's response establishes that it's complete;
- the text a model reads **opens with a `summary` sentence** that starts
  `PARTIAL RESULT` whenever there is one;
- a total QuickBooks states is reported as stated, and flagged when the returned
  cells couldn't reproduce it.

Rows that `max_rows` trims locally are known loss, too: the result is `partial`,
`client_truncated` is true, and the summary sentence says how many rows were
withheld.

A row whose cell count doesn't match its columns has every value withheld, and the
row is listed: pairing by position would guess which column each value belongs to. A
problem that **breaks the mapping between cells and columns** (a malformed column
definition, two columns whose keys would be the same, a shape this server doesn't
support) fails the whole request, because then no cell can be trusted.

### `summarize_vendor_payments`

**A cross-check against a bank ledger.** It classifies nothing for filing. There's no
threshold flag and no upper-bound field: payment type is an accounting signal,
not evidence that money settled or that any tax rule applies, and a bound can't
be proven while any amount is unknown.

It walks Purchase and BillPayment. **Bills are never added to their payments**:
that's the same money twice. JournalEntry lines touching a vendor come back **for
review only and are never summed**, because a journal entry's header total is
documented as zero, so any amount must come from signed lines.

`Purchase.EntityRef` may identify a Customer, a Vendor *or an Employee*, so the
reference type is read rather than assumed. The `Credit` flag is documented for
credit-card refunds only and is applied there and nowhere else.

Deduplication is by source and id. **The same economic payment recorded under two
different ids is outside that** and would be counted twice; the envelope says so.
Multi-currency companies are refused outright for aggregate totals. Accounts
payable is established by first use, not guessed from the chart of accounts: a
BillPayment read that QuickBooks refuses with its feature fault (5030) records the
entity as having no accounts payable, the walk is Purchase-only and `sources` says
so; a read that succeeds records it as available; until either,
`capabilities.accounts_payable` is `unknown` and says so. A re-consent asks the
question again.

### `list_changes`

The 1,000-object cap applies to the **whole response across all types**: 600 of
one type and 400 of another saturates it with neither reaching 1,000. Saturation
therefore makes *every* requested type uncertain, and **a possibly incomplete
response is not a safe synchronization checkpoint**: `checkpoint_safe` says so
directly. Look-back is 30 days. A query on a modification timestamp isn't an
equivalent fallback, because a deleted row leaves nothing to match. A response
that isn't the documented shape at any level is refused whole, never read in
part: an entry skipped as unreadable would leave a count that looks complete.
In sandbox, a response held one entry per requested type, empty for a type with
no changes ([api-findings.md](api-findings.md), item 7). So a response is refused
when its entries don't account for each requested type once, since a type no entry
accounts for would read as unchanged, and when an entry holds anything but a type's
objects and its paging fields, since anything else could change what the entry
means.

### Every result names its source

Every Intuit-backed success carries `entity`, `environment`,
`display_name_from_registry`, `realm_id_last4`, `request_started_at` and
`fetched_at`. **This server puts only the last four digits of a realm id in a result,
and masks a full one in errors and logs.** Text from the company file passes through as
written, whatever it holds. The redactor keeps two long values whole, because a reader
needs them and neither is a secret: Intuit's support id, and the storage host a download
names when it isn't pinned.
An Accounting API failure names the `entity` and `environment` the call was bound to,
and carries Intuit's support id as `intuit_tid=...` when the failed response had
one: up to 80 letters, digits and hyphens. A fault is named by its code, `fault=<code>`: with a
fixed hint when this server knows the code, alone when it doesn't, and never with
the fault's own message or detail.

### What is deliberately not returned

- No address, phone, email or web-address fields. Free text can still hold one.
- `employer_id_last4` only: the employer id comes back from Intuit unmasked.
- `AcctNum` truncated to its last four on Bank and Credit Card accounts. It's a
  user-defined chart-of-accounts code on every account; the truncation is a policy
  for the two types users put real account numbers into, and the full value on the
  other types is whatever a user typed.

Projections walk an **allowlist spec, never the payload**, so a field Intuit adds
later can't appear in a result. Report columns pass by key the same way unless
`admitted_by` says otherwise. A keyless label column passes by its position, and a keyless
Money column by a title the report's shape lists. Two kinds have keys that change with the
request. A period column's key is its label (`Jan 2026`), so it passes on its shape: a
monetary type (Money, Amount or Rate) and two real dates in order. An entity grouping column's key is a record id, so
it passes only when the request named that grouping. Those two trust Intuit's column
metadata: a new Money column shaped like a period would be returned.

The other places a QuickBooks value becomes text take one value and nothing else: a
change's id, status and timestamp, a payment's references, dates and numbers, and a
report's header fields and row groups. An object there is left out of a payment, and
refuses a change list or a report whole, because its text would carry every field
nested in it.

Accounting data this server returns goes to the
AI client and can reach that client's model provider. Names, memos, descriptions
and report cells are free text from the company file: they can carry private
identifiers despite field projection, and they are untrusted input to the model
reading them, never instructions. Every tool that returns such text says both
things in its description.

### Documents (opt-in)

`list_attachments` is always available and returns attachment METADATA only: file
name, type, size, note, the linked transaction, timestamps. Never a link.

Fetching the file itself is off unless the operator turns it on, in two steps,
both required:

1. **`--allow-documents`** (or `QBO_ALLOW_DOCUMENTS=1`) registers `get_attachment`.
   Without it the tool is absent from `tools/list` and the server has the eighteen
   default tools.
2. **`QBO_ATTACHMENT_HOSTS`** (comma-separated, exact host or `*.suffix`; or
   `--attachment-host`, repeatable) pins the storage host the download client may
   contact. Nothing is pinned by default: with the flag and no host, `get_attachment`
   refuses before any request, and `list_attachments` says `download_available: false`.

The host a sandbox attachment link named was observed on 2026-09-25:
`financialdocument.platform.intuit.com`. Every no-host refusal names it, with the
qualifier that it's the SANDBOX observation. Production has not been observed and
may name another host. To find it, pin a placeholder host and call `get_attachment` once: the
refusal names the host the link asked for, and no storage host is contacted. Then pin that host.

The download runs on a separate client:

- no bearer, cookie or proxy authorization on any request, and no environment proxy;
- https on the default port to a pinned host only, never an IP literal;
- every redirect destination re-checked, at most three;
- the pinned name resolved first, and refused if any address is private, loopback,
  link-local or reserved;
- a MIME allowlist (PDF, PNG, JPEG, GIF, plain text, CSV), with each type's own byte
  signature checked;
- a body whose length differs from the attachment's recorded size refused (a size
  written as text is read as its number, and one that isn't a non-negative whole number
  refuses the download before any request). With no recorded size, only the stream cap
  bounds the body;
- a stream cap during the download, and a 10 MiB cap on the complete result, metadata
  and encoded file together.

The result is two blocks: the metadata, then the
file as an embedded resource with its sha256. A refusal says which gate refused,
names the company and environment, and never carries the realm id or the signed
link. If a gate refuses in a deployment, `list_attachments` alone is a supported
configuration.

One limit: the pinned name is resolved and checked once, and the connection resolves
it again, so the check is a pre-check rather than the address actually connected to.
It refuses the obvious cases and proves nothing about a name whose answer changes.
Treat production documents as off until you've weighed that.

Against a loopback API base outside production, which is the test setup, two of these
relax: a pinned loopback IP literal is admitted, over http and on any port, and a pinned
name may resolve to loopback. A pinned name still needs https and the default port.

### Two balances, never added together

Each account carries `current_balance` and `current_balance_with_sub_accounts`. The
second **already contains** every child's balance. Adding them double-counts each
child, and the result looks plausible. Hierarchy
is preserved through `parent.id` so you can do whichever roll-up you actually want.

### Capabilities

MCP advertises one tool list for the whole server, and this server holds several
companies, so capability can't live in a schema. It lives in results and in errors.
The first call per entity per process reads Preferences once and derives class
tracking, department tracking, inventory, multi-currency and the accounting basis.

Companies on lower QuickBooks plans genuinely lack features. A tool whose required
feature is known absent returns a fixed, actionable error naming the feature and the
entity, **before any request**. It never silently returns an empty result: "no
classes" and "this plan has no classes" are different answers. `list_reference_data` is
the one exception: it answers with an empty list, `feature_enabled: false` and a note.

### No error repeats a caller's value

The SDK validates arguments before a tool runs and its own message quotes the value;
this server rebuilds that message at the call boundary as the parameter and the
reason. A value you compose, a configured secret included, never comes back in an error:
no text, id, date or number you sent. An error can name a configured entity key or one of
this server's own option values, in its own spelling.
That holds for every refusal: the six-month window refusal names `start_date`,
`end_date` and the limit, not the dates, and a test sends a composed value through
every parameter of every tool and finds none repeated.

The command line gets the same rule. `argparse` prints what it refuses, so all three
entry points use a parser that keeps words shaped like an option and drops everything
else, and the auth CLI doesn't repeat a company key it doesn't know. A startup error does
name the file and, where it can, the key or field at fault, because you need that to fix
it, and it goes through the redactor on its way out. Each entry point tells the redactor
the secrets in its environment first, and again after loading an env file, because the
client secret is otherwise learned only once credentials are built, after the registry.
The redactor looks for each held secret inside longer text from 16 characters up, so a
short test value can't blank out ordinary words. A shorter one is masked when it is the
whole message or follows a name like `client_secret=`.

## The token store

One encrypted document per environment, keyed by realm. **A refresh may replace the
refresh token, and once a new one is issued the previous one has to be treated as dead.** Intuit's Authorization FAQ
says the value is updated periodically, every 24 hours or on the first refresh after
24 hours, and that the previous value then expires; Intuit's `oauth-jsclient` README
says previous refresh tokens expire 24 hours after you receive a new one.
The code depends on neither reading. What holds under both is that this package can
rely on at most one credential per realm at a time.

- **One canonical lock.** Every mutation (refresh, consent, import, revoke,
  guard-state writes) takes the same `flock` on a `tokens.lock` sidecar,
  plus an in-process mutex. Never held across human interaction.
- **Persist before use.** Temp file at the store's protection mode → `fsync` contents
  → `os.replace` → `fsync` the parent directory → *only then* use the new access
  token. A failed write discards the token and raises. Consent and import differ in
  one step: the new access token reads the company's name once before anything is
  stored, so a pair for the wrong company is never stored. When a step ends before
  the commit, refused or stopped by an error or an interrupt, or the commit fails
  with one of its own errors, all raised before the pair is written, the tool asks
  Intuit to revoke the new pair, best effort. Once the pair is
  stored the step never revokes it; a journal that can't be cleared after that is
  logged.
- **A write-ahead journal**, encrypted like the store. **Adoption is decided by
  generation, never by comparing token strings**: an unchanged refresh token can
  legitimately accompany a new access token and expiry, since the FAQ says the refresh
  value changes at most once a day.
- **Ambiguous outcomes stop.** A refresh whose response was lost, or came back without two
  usable tokens, marks the realm `refresh_uncertain` and is never retried automatically: a
  blank token can't replace the stored pair, and the old one may already be spent. Tools
  refuse that company until `verify` or a new consent. A journal whose contents make no
  sense counts as ambiguous for every company, because it may be the only record of an
  accepted refresh, and moves aside as `<journal>.unreadable-*`. A read that merely failed
  changes nothing, and calls fail, naming the file, until it reads again.
- **Configuration is not consent.** A row from the wrong environment or the wrong
  client id is a configuration error and never sets `needs_consent`.
- **A 401 is not proof of expiry.** The rejected generation is identified; a newer
  stored generation is used if one exists, otherwise exactly one refresh is forced.

### Where the store may live

The store refuses a path inside a git working copy or a known cloud-sync root
(`~/Dropbox`, `~/Library/CloudStorage`, `~/OneDrive`, and similar). A rotating
credential in a repository gets committed eventually; one in a sync root gets copied
to a second machine that then refreshes it independently, breaking the chain for
both.

**That check can't be complete: arbitrary sync software cannot be detected.** A backup agent, an rsync cron entry, a network
home directory or a filesystem snapshot will all copy this file without leaving any
marker to look for.

### No backups

There are none, on purpose. A backup taken before a rotation holds a token this
package must treat as dead, so restoring one can't be relied on and will usually make
the situation harder to reason about.

## Keeping credentials alive

A refresh token unused for 100 days expires for good. The keepalive refreshes every
connected company on a schedule ([production.md](production.md) has launchd and cron
snippets).

```bash
qbo-multicompany-mcp-keepalive --env-file .env.production                # refresh realms older than an hour
qbo-multicompany-mcp-keepalive --env-file .env.production --force
qbo-multicompany-mcp-keepalive --env-file .env.production --require-all  # every configured company must be connected
```

It shares the canonical lock and the store's own refresh, re-checks the one-hour
criterion *inside* the lock, and writes one line per entity after the store returns. A
company refreshed within the hour is left alone, and its line says `unchanged`. A lock timeout waits once more then exits 1 (transient), never 2.
A configuration error on one company goes on that company's line, the others are still
refreshed, and the run exits 2. The exception is Intuit refusing the app's own client id or
secret: every company would get the same answer, so no other refresh token goes out, and the run
exits 2. Every later company still gets a line: a malformed, never-authorized, needs-consent or
uncertain row says so, and every other one reads "not attempted", one refreshed within the hour
or bound to another configuration included.

A company in the registry with no stored credential is skipped by default ("never
authorized; skipped"), and on its own doesn't make the run exit nonzero: a registry
can name a company before its consent. Where every configured company is meant to be
connected, pass `--require-all`: such a company then prints "never authorized; needs
attention" and the run exits 1, so a scheduler reading the exit code doesn't report a
healthy run while one company can answer nothing.

A refresh token has two clocks, and the keepalive names each. The **inactivity** clock
is 100 days as Intuit documents it (the token response reported 101 in sandbox), and it
restarts when Intuit issues
a new refresh-token value, which Intuit's Authorization FAQ says happens at most once
per 24 hours: a refresh inside those 24 hours returns the same value and the same
expiry. Observed on both sandbox realms on 2026-09-26. The **hard** clock is a
five-year maximum that only a re-consent restarts. Intuit reports it only on request,
so every exchange and refresh sends the documented opt-in header
(`x-include-refresh-token-hard-expires-in: true`); observed live on 2026-09-24 against
two sandbox realms: every response carried the field, the clock counts down from the
grant, and a refresh doesn't restart it. It's reported as observed when a response
carries it, otherwise as unknown, with the five-year maximum from a `connect`'s consent
date as an upper bound. Both clocks are warned about, each by name; a warning exits 1.

`stale_refresh` is computed **per realm**, so a healthy realm never masks another's age,
and it's a **passive observation, not an alert**. It says "this realm's own
`last_refresh_at` is old". It can't say "the keepalive is not running", because a process
that never runs announces nothing. **Monitor the schedule itself.**

## Limits worth knowing

- **Filter validation is not uniform, and one filter proves nothing about another.**
  Three classes: an unknown parameter *name* is discarded (you get everything); an
  **enumerated** filter (`transaction_type`, `cleared`, `source_account_type`) refuses a
  bad value with `HTTP 400 Invalid Enumeration`; an **id or free-text** filter (`vendor`,
  `customer`, `docnum`, `memo`) returns `200` with zero rows. An id's *shape* is
  validated, its *existence* is not. Every report id filter takes a **list** of ids, each
  a run of digits, sent as the one comma-separated value the report pages document; the
  digits rule is this server's, since the pages type an id only as a string, and every id
  seen, in sandbox and in production, has been digits.
- **All three enumerations are pinned in the published schema**, and a value outside one
  is refused by this server before any request, never sent to become a runtime `400`. Each
  is the set proven to work in sandbox: because a bad value is refused, a `200` proves a
  value belongs. That proves the values tried, so a pin may be narrower than what
  QuickBooks accepts, and production may differ.
- **A filtered `list_transactions` request returning zero rows is re-checked unfiltered**
  before it reports "none", and the result names which filters could have matched nothing
  silently, the id and free-text ones. The enumerated filters can't produce that case.
- **The value you read is often not the value you filter on.** `transaction_type` is a
  third vocabulary: `JournalEntry` works, `Journal Entry`, `Purchase` and `BillPayment`
  don't. `source_account_type` is the AccountType enum **with the spaces removed**:
  `AccountsPayable` filters, `Accounts Payable` is refused, and `list_accounts` reports the
  *spaced* form. Results carry `filterable_transaction_types` and
  `filterable_source_account_types` for exactly this reason.
- **`list_transactions` doesn't send `accounting_method`**, and the network policy refuses
  it on TransactionList. Whether TransactionList honours it isn't established: a
  probe saw no effect on the rows in one window, which isn't evidence either way, so the
  cash-versus-accrual distinction isn't claimed there. The report tools that document it
  send it, defaulting to the company's report basis.
- **`ORDERBY Id` is rejected although sandbox honours it.** Intuit's change log says
  production stopped on 2026-01-27. More generally, a value set confirmed in sandbox may not
  hold in production, so unrecognised values are surfaced rather than bucketed. See
  [api-findings.md](api-findings.md).
- **Rate limits are shared across processes through one file**, against both documented
  windows (10/second and 500/minute per company). The tests use two limiters in one process. A 429 is reported as a bounded throttling-or-quota failure
  rather than slept through, because a 429 can also mean the monthly quota is exhausted and
  waiting doesn't replenish that. The shared counters file names each company by its entity
  key, never its realm id, and is written 0600 in the store's folder, which is created 0700
  whichever process reaches it first. A call the file can't record isn't sent, and a file
  that can't be read stops the call too, because empty state would open every window. A
  corrupt file is rebuilt, which forgets the last minute's calls and any 429 block it
  recorded, an hour at most.
- **Each tool call has one 150-second budget** covering lock waits, rate-limit waits, every
  page, every retry and its sleep; a request gets what's left of it, less five seconds held
  back to build the result. An attachment download spends the same budget on every redirect.
  A caller that cancels stops the reads at the next request. A token refresh is never cut
  short once submitted, since one stopped midway has an unknown outcome, so one isn't begun
  unless more than 35 seconds are left after the five held back, checked under the store
  lock after the wait for it. That reserve, one 30-second request timeout and 5 seconds for
  the write, is an estimate: the token response is read without the budget, and its read
  timeout restarts on every socket read, so a response that trickles in can run past it
  while holding the store lock that every other refresh and consent waits for. Three other
  waits can outlast the budget, because no timeout here covers them whole: the operating
  system's DNS lookup; the connect, the TLS handshake and the first byte, since each phase
  gets the whole remainder; and a response whose headers trickle in, since the read timeout
  restarts on every socket read. The call ends at the next check after any of them.
- **Response bodies are capped after decoding**: 32 MiB, 16 MiB for a report. Accounting and
  attachment requests ask for `Accept-Encoding: identity`; whether Intuit honours that is unknown, which is why the
  cap counts decoded bytes. The cap is checked per chunk, so one compressed chunk can inflate
  past its wire size before the check fires, so the bound is loose.
- **A 5xx on a read is retried** three times with full-jitter backoff (base one second,
  factor two). A 504 or a read timeout on a report isn't retried: re-issuing a report that
  already timed out adds load without improving the odds.
- **PKCE is not claimed.** `code_challenge` is deliberately not sent; add it only once a
  sandbox consent has been *observed* to succeed with it present.
- **`run_query` returns rows for list entities only.** It parses the transaction entities and
  answers `COUNT(*)` on them, but refuses `SELECT *` on one before any request, and
  `list_changes` refuses them too, because no projection is registered for those routes. Use
  `list_transactions` and `get_transaction` for transaction rows.
- **Some report rules are interpretations.** The aging details have no transaction-date
  window, so the six-month limit is applied to their due-date range, a one-sided range is
  refused, and no range at all is allowed. AccountListDetail carries no six-month limit: it
  lists accounts, not transactions. VendorBalanceDetail's documented `date_macro` isn't
  exposed: what a range macro means on an as-of-plus-due-date report hasn't been shown.
- **Some behaviour is unobserved.** What "Next 4 Weeks" does when today is a Sunday is a
  labelled assumption. Class, department and currency report columns are documented but
  unseen in any live response. A payables report that answers
  is read as a sign the company has accounts payable, which is an assumption.
- **`QBO_TOKEN_KEY_SOURCE=keychain` is macOS-only.** Use `env` elsewhere.
- **Cloud-sync detection is best-effort** (see above).

## Secret scanning

[`.gitleaks.toml`](../.gitleaks.toml) adds keyword-anchored rules for Intuit client ids and
secrets, refresh and access tokens, the token-store key, and realm ids, on top of the
upstream gitleaks ruleset. Run both scans:

```bash
gitleaks git --no-banner --redact     # commit history
gitleaks dir --no-banner --redact     # working tree
```

`--redact` matters: without it gitleaks prints the secret it found into the scan log.

This config avoids two usual shortcuts:

- **`tests/` is not allowlisted by path.** The synthetic fixture credentials are allowlisted
  *by value*, listed individually. Allowlisting the directory would have silenced the fixtures
  and also switched off every rule in the one place most likely to collect a real token during
  a debugging session. Adding a new fixture credential therefore means editing the config,
  which puts a human in front of the decision.
- **The rules are tested.** A scanner that matches nothing reports zero findings.
  `tests/test_secret_scanning.py` builds
  credential-shaped canaries at run time and requires every rule declared in the config to
  fire on its own canary, and requires a non-placeholder realm id under `tests/` to still be
  caught.

## The mutation gate

A passing test suite says nothing about whether a safety behaviour is *protected*: a
behaviour can be deleted with every test still green. That matters most for the send-time
read-only check, the central claim, and the cross-process file lock, which stops two
processes destroying a refresh chain.

[`scripts/mutation_gate.py`](../scripts/mutation_gate.py) is the mechanical answer. Each entry
names a behaviour, the exact edit that removes it, and the test family that claims to protect
it. The gate copies the tree, applies the edit to the copy, runs that family, and **fails if the
family still passes**. The copy holds the files git lists, tracked or new and not ignored, in a
fresh repository with one commit of the tracked ones, which is what the hygiene tests read. A local
`.env`, `entities.yaml` or token file never goes in, even one tracked by mistake, and nor does the
original's git directory, which keeps whatever was ever staged. So it runs only in a git checkout,
with git 2.32 or later. A variable that names a repository, such as a `GIT_DIR` from a hook, never
reaches its listing, the copy or the tests it runs. The gate's own git commands on the copy also see
no other `GIT_*` variable and none of your own or the system's configuration; the tests it runs
there use yours. Each tracked file is added by its literal name and forced, and the copy's index
must then match the list, so an ignore rule or a name like `[ab].txt` changes nothing.

```bash
uv run python scripts/mutation_gate.py
uv run python scripts/mutation_gate.py --list
uv run python scripts/mutation_gate.py --only store-flock
```

It carries controls, so that the gate itself can fail:

- **The import control.** A subprocess is asked where it imports the package from, and the
  answer must be inside the copy. A gate that quietly imported the unmutated source would report
  every behaviour protected, forever.
- **The baseline.** The selected families must pass unmutated, or a failure under mutation proves
  nothing.
- **The mutation control.** One entry deletes a behaviour whose detectors are known to be alive.
  It runs first, whatever was selected. If *it* isn't caught, the harness is broken rather than
  the tree being clean, and the gate exits 2 before any other entry runs.
- **The collection control.** "Caught" means a test failed. A collection error, an internal error,
  or nothing collected all exit nonzero having tested nothing, so the verdict is read from pytest's
  own summary, and a planted broken test file must be classified as infrastructure, never as caught.

The gate's own failure mode is going stale: rename the line an entry matches on and it stops
describing anything, mutates nothing, and prints a pass. So every edit must match exactly once and
a miss is an error, never a skip, and `tests/test_mutation_gate.py` runs that check in the ordinary
suite, so a refactor that moves one of these lines fails in milliseconds rather than at release. A
mutated Python file must also still compile: an edit that breaks the module fails its family on
import, which would read as "caught" having tested nothing.

Tests that start their own server subprocess pass it the source tree the test process imported.
Without that, a subprocess falls back to the installed package, which under the gate is the
*unmutated* tree, and passes whatever the gate deleted.

Exit codes: `0` every behaviour protected, `1` at least one can be deleted unnoticed, `2` the gate
itself could not be trusted.

## Tests

Test families are named after what they prove: registry and startup, token store (lock, write-ahead,
generation adoption, same-string refresh, stale journal replay, durability, races, response loss,
disk full), OAuth CLI, network policy, projections, serialization, the error boundary, the guard,
keepalive, and repo hygiene.

The token store has both a *threads* test and a *processes* test for one realm, and they prove
different things: `canonical_lock` holds an in-process mutex as well as a file lock, so two threads
serialise even with the `flock` deleted. Only `tests/refresh_child.py`, which is a second interpreter,
contends for the lock itself.

Fixture realms match `^0{12,15}[0-9]$` and company names are obviously fake, enforced by a test: a
realistic realm id in a public repository would help someone guess a real one.
