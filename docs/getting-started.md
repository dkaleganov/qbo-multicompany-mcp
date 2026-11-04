# Getting started

How to reach a first tool call on a QuickBooks sandbox company. Sandbox data is
Intuit's sample data, so nothing here touches real books. Production has its own guide:
[production.md](production.md).

You need Python 3.11 or later, [`uv`](https://github.com/astral-sh/uv), and a browser. macOS is
tested. Windows isn't supported. On Linux, see step 4.

## 1. Create an Intuit developer app

You bring your own app. This project has none: every user registers one and answers Intuit's
questions about it.

1. Create an account on the
   [Intuit Developer Portal](https://developer.intuit.com/app/developer/qbo/docs/get-started/start-developing-your-app).
2. Select **My Hub > App dashboard**, then the card with a **+**, and follow the steps. Pick the
   **accounting** scope only.
3. Open the app, then **Settings > Redirect URIs**, and add this under the development list:

   ```text
   http://localhost:8765/callback
   ```

   That exact value is this server's default redirect. Intuit doesn't accept an IP address here, so it says
   `localhost`, not `127.0.0.1`.
4. Open **Keys and credentials > Development**, turn on **Show credentials**, and keep the page open
   for step 4. ([Intuit's page on credentials](https://developer.intuit.com/app/developer/qbo/docs/get-started/get-client-id-and-client-secret).)

## 2. Find your sandbox companies

A sandbox company comes with your developer account. To see this server do what it's for, add a
second one: **My Hub > Sandboxes > Add**. You can have up to ten, and they last two years
([Intuit's sandbox page](https://developer.intuit.com/app/developer/qbo/docs/develop/sandboxes/manage-your-sandboxes)).

Open each company and press **Control+Option+?** (Ctrl+Alt+? on Windows). The window lists the Company
ID, which this project calls the realm id. Intuit's OAuth 2.0 Playground shows it too, as the Realm ID.

## 3. Install

```bash
git clone https://github.com/dkaleganov/qbo-multicompany-mcp.git
```

```bash
cd qbo-multicompany-mcp
```

```bash
uv sync --frozen
```

## 4. Configure

```bash
cp entities.example.yaml entities.yaml
```

```bash
cp .env.example .env && chmod 600 .env entities.yaml
```

Both files are git-ignored and readable only by you: `.env` holds the client secret, and `entities.yaml` each
company's full Company ID. In `.env`, paste your development Client ID and Client Secret.

On Linux, add a store key too, which encrypts your tokens on disk, since `keychain` is macOS-only. Generate one with
`python3 -c "import secrets; print(secrets.token_hex(32))"` and set `QBO_TOKEN_KEY_SOURCE=env` and
`QBO_TOKEN_KEY=<that value>` in `.env`. That's fine for sandbox; on production, keep the key out of that file
([production.md](production.md)).

In `entities.yaml`, give each company a key of your choosing and put its Company ID in `realm_id`. Keep
the quotes. Remove the second block if you have one company.

```yaml
entities:
  acme_main:
    display_name: Acme Main
    realm_id: "0000000000000001"
    environment: sandbox
    writes: false
```

- The **key** is how every tool call names the company: `[a-z0-9_]+`, and it may not contain a realm id
  or eight digits in a row.
- **`realm_id` is the identity.** Each entity must name a different realm.
- **`display_name`** is checked against the name Intuit reports, then shown to people. It's never used
  to route a call. You'll correct it in step 5.
- **`writes`** may only be `false`, or be left out.

## 5. Connect each company

This step needs a person at a browser. No secret or authorization code ever goes through a chat.

On macOS, create the store key once. It's idempotent and never overwrites one:

```bash
uv run qbo-multicompany-mcp-auth --env-file .env --entities entities.yaml init-key
```

Then connect each company, using your entity key (`acme_main` here):

```bash
uv run qbo-multicompany-mcp-auth --env-file .env --entities entities.yaml connect acme_main
```

A browser opens Intuit's consent screen. Choose **that** company and approve. The screen describes read
**and** write access: Intuit offers no read-only accounting permission, and this code is what keeps the
server read-only.

| What you see | What it means |
|---|---|
| `Connected '<entity>'` with both names and the expiry dates | Done |
| The company you approved isn't the one this entity names | Nothing was stored. Choose the right company, or fix `realm_id` |
| A company name mismatch | The company is right and only `display_name` differs. Answer `y`, then copy the printed `display_name:` line into `entities.yaml`. Until it matches, every tool except `get_company_info` refuses that company, and a running server keeps refusing until you restart your AI client |
| The browser never returns to the terminal | Press Ctrl-C, run `connect <entity> --manual`, open the address it prints, and paste the address the browser ends on |

`connect` listens on port 8765 and exits rather than choosing another port, because the redirect you
registered is fixed. If something else holds the port on `127.0.0.1` or `[::1]`, stop it and try again.

Check the result:

```bash
uv run qbo-multicompany-mcp-auth --env-file .env --entities entities.yaml status
```

Every company should show `state: authorized`. This command can't confirm the name: if you answered `y` to a
mismatch, finish the `display_name` edit now, because the first tool call checks it.

## 6. Add it to your AI client

Use absolute paths: a client starts the server from its own folder. Run `pwd` in the repo for
`<repo>`, and replace it below. Run your AI sessions outside the repo folder: an agent working in it can read
`.env` and `entities.yaml`. `--scope user` below makes the server available to every Claude Code session on
your account.

**Claude Code**

```bash
claude mcp add --scope user qbo -- "<repo>/.venv/bin/qbo-multicompany-mcp" --env-file "<repo>/.env" --entities "<repo>/entities.yaml"
```

**Codex**

```bash
codex mcp add qbo -- "<repo>/.venv/bin/qbo-multicompany-mcp" --env-file "<repo>/.env" --entities "<repo>/entities.yaml"
```

**Claude Desktop.** In **Settings > Developer > Edit Config**, add this to `claude_desktop_config.json`,
then restart the app:

```json
{
  "mcpServers": {
    "qbo": {
      "command": "<repo>/.venv/bin/qbo-multicompany-mcp",
      "args": ["--env-file", "<repo>/.env", "--entities", "<repo>/entities.yaml"]
    }
  }
}
```

## 7. First calls

Ask your client:

> Call list_entities, then get_company_info for each company.

`list_entities` makes no request to QuickBooks, so it works even before a company is connected.
`get_company_info` should answer for each company, naming it and its environment. From there:

> Show the profit and loss for acme_main this fiscal year-to-date, then the same for acme_ops.

> Which vendors did acme_main pay last year? Show candidates only, and what needs review.

[tools.md](tools.md) lists every tool. Start at "Choosing a tool".

## Keep it connected

A refresh token unused for 100 days expires for good, so a server you use rarely needs a keepalive.
[production.md](production.md) has launchd and cron snippets. For sandbox, connecting again is fine too.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `no entities file` | Pass `--entities`, or set `QBO_ENTITIES_FILE` |
| `refusing a token store inside ...` | The store won't live in a git working copy or a cloud-sync folder. Leave `QBO_TOKEN_STORE` unset for the default, `~/.local/state/qbo-multicompany-mcp` (under `XDG_STATE_HOME` if that's set), or pick another local folder |
| A configuration error naming `QBO_CLIENT_ID` | The server started without app credentials. It still answers `tools/list`, `list_entities` and `server_info`, but can't use a stored credential. Check `--env-file` |
| `invalid_client` during `connect` | The Client ID and Client Secret are swapped, or they're production keys. Sandbox uses the Development pair |
| `redirect_uri` rejected by Intuit | The registered value must match exactly: `http://localhost:8765/callback` under the development list |
| A tool says the company name doesn't match | Copy the name QuickBooks reports into `display_name`, byte for byte, then restart your AI client: the server reads the registry once, at startup. Sandbox names are generated, and change if a sandbox is reset |
| `needs_consent`, `expired`, `refresh_uncertain` | See "Token recovery" in [production.md](production.md). The same steps apply to sandbox: use `--env-file .env --entities entities.yaml` in place of `--env-file .env.production`, keychain account `token-key-sandbox`, and files `tokens.sandbox.*` |
