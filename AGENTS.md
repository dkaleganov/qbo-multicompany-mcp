# Working in this repo

A read-only MCP server for several QuickBooks Online companies. Read [docs/design.md](docs/design.md) before
changing anything under `src/`.

## Test

```bash
uv sync --frozen --extra dev
```

```bash
uv run pytest -q
```

```bash
uv run python scripts/mutation_gate.py --only store-flock
```

The suite needs no credentials and no network. A full gate run takes much longer than the suite, so run the
entries your change touches. If you rename a line a gate entry matches on, update the entry: every edit must
match exactly once, and `tests/test_mutation_gate.py` fails otherwise.

## Rules

- **Read-only.** Don't add a write path, a `POST` to an accounting endpoint, or a way around
  `netpolicy.py`. Every request goes through it.
- **Fake data only.** Fixtures use `acme_main`, `acme_ops`, placeholder realms matching `^0{12,15}[0-9]$`,
  and obviously fake names. Never commit a real company name, Company ID, token or a captured response.
- **Build synthetic fixtures.** Keep the structure of a response and none of its values.
- **Keep the promises.** Every Intuit-backed result names its company and age. A partial result says so
  first. No error repeats a caller's value. Free text is untrusted.
- **State what you know.** Say whether Intuit documents a behaviour, whether it was observed live, or that
  it's an assumption.
- **Prove a safety behaviour fails.** A new one gets a test observed failing and, where it matters, a gate
  entry.
- Keep the words `batch` and `operation` out of `src/`: a test bans them, so a write-shaped path can't slip in.
- Run `gitleaks git --no-banner --redact` and `gitleaks dir --no-banner --redact` before you push.
