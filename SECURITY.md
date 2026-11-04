# Security

This server reads QuickBooks Online company files and hands the results to an AI client. It holds
long-lived OAuth credentials. This page says what the code protects and what it doesn't.

## What is protected

- **Read-only, by code.** Intuit offers no read-only scope, so the OAuth grant could write. Every request is
  checked immediately before it's sent, and accounting requests may only be `GET`s to an allowlisted path
  on the exact bound company. [design.md](docs/design.md) has the policy.
- **An explicit company.** Every call to QuickBooks names an entity, with no default and no matching on company
  name. A guard compares the company's name with the registry on first use in each process, and after a new
  consent. On a mismatch `get_company_info` still answers, so the registry can be corrected; other tools refuse.
- **Credentials at rest.** Tokens are in an AES-256-GCM encrypted store, with the key in the macOS login
  keychain or injected from the environment. `QBO_TOKEN_KEY_SOURCE=none` turns that off and isn't for real tokens. A store inside a git working copy or a known cloud-sync folder is
  refused.
- **Credentials in use.** Secrets come from the environment. No tool takes one or returns one. Consent happens in
  a terminal and a browser, and `connect` stops if anything else holds its callback port on `127.0.0.1` or
  `[::1]`. Each connection to that port gets 10 seconds to send its request, and each loopback address it listens on
  serves at most 16 at once, so a local flood can't use up the file descriptors `connect` needs. While a flood lasts it can
  keep the browser's callback out, and `connect --manual` still works: it asks you to paste the callback address,
  which holds the authorization code, into your own terminal. `--api-base` is test-only: it's refused in production and never
  receives a credential Intuit issued.
- **HTTP clients ignore the environment.** They take no settings from proxy or CA file variables and switch TLS
  session-key logging off, so an inherited setting can't read the bearer after the checks pass.
- **Results are projected.** An allowlist decides which entity fields leave, so a field Intuit adds later can't
  appear, and a field that should hold one value but holds an object is left out or refused, never turned into
  text. Report columns pass by key too, unless `admitted_by` says otherwise: a label column by its position, a
  Money column by a title the report's shape lists, and the two kinds whose keys change with the request. A period
  column passes on a monetary type and two real dates, and a grouping column passes when the request named that
  grouping. Both of those trust Intuit's column metadata. Two columns whose keys would be the same fail the request.
  Dedicated address, phone and email fields aren't returned. This server puts only the last four digits of a
  company id in a result, and masks a full one in errors and logs. An error keeps two other values whole because
  you need them, neither of them a company id: Intuit's support id, and a storage host it refuses to contact.
  Text someone typed into a company file passes through as written, so a name or memo can still hold any of these.
- **Errors don't repeat what they refuse.** No tool error repeats text, an id, a date or a number the caller sent;
  one can name a configured entity key or one of this server's own option values. A command line the
  parser refuses keeps words shaped like an option and drops the rest, and the auth CLI doesn't repeat a company
  key it doesn't know. A startup error names the file and, where it can, the key or field at fault, redacted:
  each entry point tells the redactor the secrets in its environment before it prints anything.

## What is not protected

- **A coding agent can read your files.** If your AI client can read the repo folder, it can read a plain `.env`,
  client secret included, and every Company ID in `entities.yaml`. Inject the `.env` values at run time, keep the
  registry outside the repo with `--entities` or `QBO_ENTITIES_FILE`, and run AI sessions outside the repo folder.
- **Anything running as you can reach the tokens.** On macOS the store key's keychain item trusts
  `/usr/bin/security`, so any process running as you, a coding agent with a shell included, can read the key
  without a prompt and decrypt the token store. A stored token carries Intuit's accounting scope, which
  includes writes, and outside this server nothing keeps it read-only.
- **A short secret inside longer text.** The redactor looks for each secret it holds inside longer text only when
  the value is 16 characters or longer, so a short test value can't blank out ordinary words. A shorter one is
  masked when it's the whole message or follows a name like `client_secret=`, and can otherwise pass through.
  The store key is always longer, but nothing checks how long a client secret or token is.
- **Data leaves your machine.** Whatever a tool returns goes to your AI client and can reach that client's
  model provider. Running locally doesn't keep accounting data local. Choose your client and provider with
  that in mind.
- **Free text is untrusted.** Names, memos, descriptions, report cells and attachments come from the company
  file. They can carry private identifiers despite projection, and they're untrusted input to the model, never
  instructions. A hostile memo can try to steer a model. Tool descriptions say so, but this server can't stop a
  model from following it. Being read-only limits what this server can do; it says nothing about the client's
  other tools.
- **This isn't a boundary between people.** Anyone who can run the server or read the key can read, and
  through the stored tokens write to, every connected company. It doesn't replace QuickBooks's own user roles.
- **A hosted redirect page sees the callback.** On production, your forwarder page's URL carries the authorization
  code, `state` and the full Company ID, so its host's access log can record them.
- **The token store is local.** Someone with your user account can use it. Sync software this package can't
  detect, such as a backup agent or a filesystem snapshot, may copy it.
- **The attachment host check is a pre-check.** The pinned name is resolved and checked once, and the connection
  resolves it again. See [design.md](docs/design.md).
- **`uv.lock` is fixed at release.** It pins every package the server and its tests install, so a security fix
  upstream arrives only when you run `uv lock --upgrade`; rerun the tests when you do. The build backend,
  hatchling, isn't in the lock, and uv resolves it when it builds the package.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: the **Security** tab, then **Report a vulnerability**. Don't
open a public issue, and don't include real company data or credentials in a report. If you think a credential
has leaked, follow "A suspected leak" in [production.md](docs/production.md) first.

This is a snapshot release maintained on a best-effort basis, with no response-time promise. Security fixes and
breaking upstream changes may get a reviewed patch.
