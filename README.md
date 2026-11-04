# qbo-multicompany-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that serves **several QuickBooks Online
company files** to one AI session over stdio. Every call to QuickBooks names its company through an
explicit `entity` key. There's no default company and no matching on company name.

> Unofficial. Not affiliated with or endorsed by Intuit. Each user registers their own Intuit app and
> answers Intuit's questions for themselves.

## Who it's for

- **You run or keep books for more than one QuickBooks company** and want an assistant to read across
  them without mixing them up: close the month, compare companies, trace a transaction, cross-check
  vendor payments.
- **You build tools for AI agents** and want a worked example of an MCP server designed around how a
  model actually behaves.

Other QuickBooks MCP servers exist, some from Intuit, and some serve several companies or write to the
books. Compare them against what you need. This one never writes.

```text
AI client  -- stdio -->  qbo-multicompany-mcp  -- GET, checked before send -->  QuickBooks Online
                              |
                              +-- entities.yaml        your companies, by key
                              +-- encrypted store      one token chain per company
```

## Quickstart on sandbox

You need Python 3.11+, [`uv`](https://github.com/astral-sh/uv), and an
[Intuit developer app](https://developer.intuit.com/app/developer/qbo/docs/get-started/start-developing-your-app)
with `http://localhost:8765/callback` registered as a development redirect URI.

```bash
git clone https://github.com/dkaleganov/qbo-multicompany-mcp.git
```

```bash
cd qbo-multicompany-mcp && uv sync --frozen && cp entities.example.yaml entities.yaml && cp .env.example .env && chmod 600 .env entities.yaml
```

Put your app's Client ID and Client Secret in `.env`, and your sandbox companies in `entities.yaml`. Then create
the store key and connect each company in a browser:

```bash
uv run qbo-multicompany-mcp-auth --env-file .env --entities entities.yaml init-key
```

```bash
uv run qbo-multicompany-mcp-auth --env-file .env --entities entities.yaml connect acme_main
```

A first connect usually reports a name mismatch: answer `y` and copy the printed `display_name` into
`entities.yaml`. Then add the server to your client with absolute paths. `--scope user` makes it available to
every Claude Code session on your account, so you can run those sessions outside the repo folder:

```bash
claude mcp add --scope user qbo -- "<repo>/.venv/bin/qbo-multicompany-mcp" --env-file "<repo>/.env" --entities "<repo>/entities.yaml"
```

[docs/getting-started.md](docs/getting-started.md) walks through every step, including Claude Desktop and
Codex. Production needs more: [docs/production.md](docs/production.md).

## Tools

Eighteen by default, and one opt-in. [docs/tools.md](docs/tools.md) has every parameter and a "Choosing a
tool" table.

| Tool | Returns |
|---|---|
| `list_entities` | Configured companies, auth state and token freshness. Makes no request |
| `server_info` | Build, environment, read-only status, store protection. Makes no request |
| `get_company_info` | Company profile, settings and derived capabilities |
| `list_accounts` | Chart of accounts with balances |
| `list_vendors` | Vendors, the 1099 flag, and whether a tax id is on file, never the id |
| `list_customers` | Customers with their balances, and no tax ids, notes or contact fields |
| `list_reference_data` | Classes, departments, items, terms, payment methods, tax codes, tax rates |
| `list_transactions` | Transactions in a window of up to six months |
| `get_transaction` | One transaction with its lines |
| `run_query` | One parsed `SELECT`, rebuilt from its parse tree and sent by `GET` |
| `list_changes` | Changes and deletions within 30 days, on list objects such as vendors and accounts, with no transactions |
| `summarize_vendor_payments` | Per-vendor **candidate** totals for a calendar year |
| `report_profit_and_loss` | Income and expense by section |
| `report_balance_sheet` | Balances as of a date |
| `report_general_ledger` | Postings by account |
| `report_aging` | Open payables or receivables by bucket |
| `run_report` | The other 21 documented reports |
| `list_attachments` | Attachment metadata, never a link |
| `get_attachment` (opt-in) | The file, only with `--allow-documents` and a pinned storage host |

## What makes it work well with AI agents

A model will call the wrong company, trust an empty result, and retry a bad value. The server is built
around that.

- **An explicit company on every call.** With no default, a model has to name the company each time, so a
  wrong choice shows in the transcript. `list_entities` gives the keys, and a guard checks the company's name
  on first use in each process.
- **Results name their company and age.** Every answer from QuickBooks carries the entity, environment, the
  last four digits of the realm id (Intuit's Company ID), and when it was fetched, so a model can say which file a number came from.
- **One list vocabulary.** Every list that walks QuickBooks's pages returns `count`, `rows`, `truncated`,
  `duplicates_dropped` and `pages_walked`, so a model learns the shape once.
- **Partial reports say so first.** A problem in a cell or a row makes the result partial, and the text a
  model reads opens with `PARTIAL RESULT`. An empty report never establishes that there's no data, and
  `list_transactions` re-checks a filtered request with zero rows, unfiltered, before it reports "none".
- **Refusals name the fix and never echo a value.** A refusal names the parameter and the reason, usually
  before any request, and no text, id, date or number you sent comes back; it can name a configured entity
  key or one of this server's own option values. A test sends a marked value through every parameter of
  every tool and finds it repeated in no refusal.
- **Company text is flagged as untrusted.** Names, memos and report cells can carry anything. Every tool that
  returns them says in its description that they're untrusted input to the model and that the data can reach
  its provider.
- **Sign-in stays with a person.** Consent happens in a terminal and a browser, secrets come from the
  environment, and no tool takes a secret or an authorization code or returns one. Anything running as
  you can read a plain `.env` and the token store's key, from any folder. Keeping AI sessions out of the repo
  folder keeps `.env` and `entities.yaml` out of an agent's working folder; an agent with a shell can still
  reach both, and the key, as [SECURITY.md](SECURITY.md) explains.
- **Descriptions route.** Several tools say in their descriptions when another fits better, and the "Choosing a
  tool" table in [docs/tools.md](docs/tools.md) covers the whole surface for people.

## Read-only, and how that's enforced

Intuit offers no read-only accounting scope: the one scope covers reads and writes alike. So read-only lives
in this code. Every request is inspected as a fully built `httpx.Request` immediately before it's sent, and
accounting requests may only be `GET`s to an allowlisted path on the exact bound company. OAuth calls go only to
Intuit's fixed endpoints, the opt-in attachment download goes only to a host you pin, and everything else is
denied. [docs/design.md](docs/design.md) has the policy, and how walks, reports and the token store behave.

## How it's verified

- **Tests.** `uv sync --frozen --extra dev`, then `uv run pytest -q`: 2,805 pass and 4 skip, with no credentials
  and no network. That count needs git 2.32 or later and `gitleaks` on your PATH; without gitleaks 25 more
  skip.
- **Mutation gate.** A passing suite doesn't prove a safety behaviour is protected, so
  [`scripts/mutation_gate.py`](scripts/mutation_gate.py) has 480 entries that each delete one and require
  its tests to fail. It has controls of its own, so it can't quietly pass by testing nothing.
- **Leak checks.** Custom [gitleaks](.gitleaks.toml) rules for Intuit credentials, each proven to fire on a
  canary, plus [`scripts/scrub_check.py`](scripts/scrub_check.py) for identifier-shaped values.
- **Live runs.** Every tool has run over stdio in sandbox: the default tools on this release, and
  `get_attachment` on a pre-release build. Every default tool ran on production, read-only, on 2026-09-28 with a
  pre-release build, apart from `TaxSummary` under `run_report`, which has never returned rows.
  What the live runs found is in [docs/api-findings.md](docs/api-findings.md).

[CHANGELOG.md](CHANGELOG.md) separates what was verified live from what has only run on fixtures.

## Example prompts

> Which companies can you see, and is each one connected?

> Compare profit and loss for acme_main and acme_ops, this fiscal year-to-date against last. Say where each
> number came from.

> Show the balance sheet for acme_ops as of the end of last quarter, then the journal entries behind any
> account that moved by more than 10%.

> Which vendors in acme_ops carry the 1099 flag?

> Give me candidate vendor payments for acme_main for 2025, and what needs review.

## Known limits

- **Read only, and QuickBooks Online only.** There are no writes, and no QuickBooks Desktop.
- **Transactions have their own tools.** On transaction types `run_query` answers `COUNT(*)` and refuses
  `SELECT *`, and `list_changes` refuses them too. Use `list_transactions` and `get_transaction`.
- **Filters validate differently.** An unknown parameter name is discarded upstream, a bad enum value is a 400,
  and an id or text that matches nothing is an empty 200. This server handles each, and
  [docs/design.md](docs/design.md#limits-worth-knowing) explains how.
- **Vendor payment totals are candidates.** They're a cross-check against a bank ledger, not a filing
  classifier, and they haven't been reconciled by hand against one.
- **Sandbox isn't production.** A value confirmed in sandbox may not hold on a production company.
- **Some columns are unobserved.** Class, department and currency report columns are documented, but no live
  response has shown them.
- **`get_attachment` is only proven in sandbox.** Production's storage host hasn't been observed, and the host
  check is a pre-check, not a binding to the address connected to.
- **Walks aren't snapshots.** A complete walk can still straddle an edit, and results say `non_snapshot`.
- **Rate limits and quotas are Intuit's.** The server enforces both documented windows, doesn't send a call
  it can't count, and reports a 429 rather than sleeping through it.
- **`uv.lock` is fixed at release.** It pins every package the server and its tests install, so run
  `uv lock --upgrade` on your own schedule. The build backend, hatchling, isn't in it: uv resolves it when it
  builds the package.

## Support

macOS is tested. Windows is unsupported: the store relies on POSIX file locks. Linux should work with
`QBO_TOKEN_KEY_SOURCE=env` but hasn't been tested. This is an alpha, frozen at this release, with best-effort
maintenance: bug
reports are welcome, and security reports go through [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
