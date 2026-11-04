# Running on production companies

Read [getting-started.md](getting-started.md) first and get a sandbox working. Production adds
four things: Intuit's approval of your own app, a hosted redirect page, a store key you keep a copy
of, and a schedule that keeps tokens alive.

This project is unofficial and isn't affiliated with or endorsed by Intuit. It has no Intuit app and
no approval to pass on. You register your own app, you answer Intuit's questions about your own use,
and Intuit decides. What follows describes Intuit's pages as they read on 2026-11-01; they change, so
read them yourself. None of this is legal advice.

## 1. Read Intuit's terms

Every tool result goes to your AI client, and from there can reach that client's model provider. So
read these with your own deployment in mind:

- The [Intuit Developer Terms of Service](https://developer.intuit.com/app/developer/qbo/docs/legal-agreements/intuit-terms-of-service-for-intuit-developer-services),
  dated October 14, 2025. Section 11.7 covers your use of AI and large language models, including use by
  third parties. Section 12.2 asks you to comply with Intuit's
  [Responsible AI Principles](https://www.intuit.com/privacy/responsible-ai/) and
  [data stewardship principles](https://developer.intuit.com/app/developer/qbo/docs/legal-agreements/intuit-terms-of-service-for-intuit-developer-services/intuit-data-stewardship-principles),
  and sets rules for sharing user data with third parties.
- The [QuickBooks terms of service](https://www.intuit.com/legal/terms/en-us/quickbooks/online/). Their
  prohibited uses include training or developing AI or machine learning models with information derived
  from the platform without authorization. Check what your model provider does with your data.
- Intuit's [security requirements](https://developer.intuit.com/app/developer/qbo/docs/go-live/publish-app/security-requirements),
  including the section on QuickBooks data usage. Intuit reviews them for apps on its App Store and for apps with over 500 connections.

This page doesn't say what those terms mean for you. That's between you and Intuit.

## 2. Publish your app's pages

Intuit's [publish guide](https://developer.intuit.com/app/developer/qbo/docs/go-live/publish-app) asks,
under **Keys and credentials > Production > App Details**, for:

- URLs for your End User License Agreement and Privacy Policy;
- your host domain, launch URL, disconnect URL and connect/reconnect URL;
- up to four categories, any regulated industries, and hosting regions.

Static pages on a domain you control can provide the URLs. Write them about your own deployment. A
privacy policy that left out where the data goes wouldn't be true, so say that accounting data reaches
your AI provider.

## 3. Host the redirect page

Intuit [allows](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/set-redirect-uri)
plain `localhost` for sandbox only. A production redirect must be https and can't be an IP address, while
`connect` listens on loopback. A small forwarder page bridges the two.

[`callback.html`](callback.html) is that page. It's static, with one hard-coded hop: it sends the browser
to `http://127.0.0.1:8765/callback` with Intuit's query string, and never takes a destination from the URL.

| Hop | Address | Why |
|---|---|---|
| Intuit → browser | `https://oauth.your-domain.example/callback` | Registered with Intuit, so it must name a host |
| browser → this server | `http://127.0.0.1:8765`, `[::1]:8765` | Both loopback literals are bound. The page targets `127.0.0.1`; `[::1]` serves sandbox's `localhost` redirect, which a browser may resolve to either family |

The page targets the `127.0.0.1` literal on purpose. That hop is local and Intuit never sees it, so the
no-IP rule doesn't apply, and the hard-coded target is what keeps the page safe. Don't "fix"
it. A test pins the file's hash, and its Content-Security-Policy carries the script's own hash, so an edit
means updating both.

1. Host `callback.html` over https at a stable URL on a domain you control, with a path ending in `/callback`,
   so `connect --manual` accepts the address the page forwards from.
   Keep it out of your host's access logs, or at least the query string: each request carries the
   authorization code, `state` and the full Company ID.
2. Note that URL for step 5, where it becomes `QBO_REDIRECT_URI`. It must be an `https://` URL with no query
   string and no fragment. An IP literal is rejected at startup.
3. Register that exact value under **Settings > Redirect URIs > Production**. As of September 2026,
   Intuit opened that tab only after production keys were granted, so it may have to wait until step 4
   is done.

If a redirect can't reach loopback, run `connect <entity> --manual` first. It prints the address to open, then
takes the address the browser ended on. Start from that run: each run has its own `state` and refuses any other.

## 4. Get production keys

Intuit requires an **App Assessment Questionnaire** before it shows production credentials, for private
apps too ([Intuit's FAQ](https://help.developer.intuit.com/s/article/New-app-assessment-process-FAQ)):
**Keys and credentials > Production > Compliance > Start Questionnaire**. The **Show Credentials** switch
appears once it's approved.

Your answers are your own attestation about your deployment. [design.md](design.md) and
[SECURITY.md](../SECURITY.md) describe what this code does, so you can answer from the code rather than
from a summary. They can't answer for how you host, who can reach your machine, or which AI client you
connect.

## 5. Configure production

Keep production apart from sandbox, in its own env file and registry, with the registry outside the repo folder:

```bash
cp .env.example .env.production && chmod 600 .env.production
```

```bash
cp entities.example.yaml /absolute/path/to/entities.production.yaml
```

In `.env.production`, set:

```bash
QBO_ENV=production
QBO_CLIENT_ID=...
QBO_CLIENT_SECRET=...
QBO_ENTITIES_FILE=/absolute/path/to/entities.production.yaml
QBO_REDIRECT_URI=https://oauth.your-domain.example/callback
```

Set `environment: production` on each entity. Find a Company ID in QuickBooks with **Control+Option+?**
(Ctrl+Alt+? on Windows), or under **Settings > Subscriptions and billing**. The repo's `.gitignore` covers
`entities.*.yaml` and `.env.*`, apart from the two example files.

Don't paste secrets or Company IDs into a chat. Prefer a secret manager that injects values into the
process environment at run time over a plain `.env`. Whatever you use, the server reads credentials from
the environment and never asks for one. The refresh token rotates, so keep each company's token on exactly
one machine.

## 6. Create and keep the store key

```bash
uv run qbo-multicompany-mcp-auth --env-file .env.production init-key
```

On macOS this creates a login-keychain item, service `qbo-multicompany-mcp`, account
`token-key-production`, if one is absent. It never overwrites one.

Keep a copy somewhere safe and separate, in your own terminal, never a chat:

```bash
uv run qbo-multicompany-mcp-auth --env-file .env.production export-key
```

Clear the terminal's scrollback once you've stored the copy. Without a copy, a lost key means reconnecting every
company. Off macOS, set `QBO_TOKEN_KEY_SOURCE=env` and
inject `QBO_TOKEN_KEY` (44 characters of base64 or 64 of hex). The store refuses to run unencrypted unless
you pass `--allow-plaintext-store` with `QBO_TOKEN_KEY_SOURCE=none`, which isn't for real credentials.

## 7. Connect each company

```bash
uv run qbo-multicompany-mcp-auth --env-file .env.production connect <entity>
```

You must be able to connect apps on that company file. Intuit limits that to the primary admin, a company
admin, or an accounting firm's owner. The outcomes are the ones in
[getting-started.md](getting-started.md#5-connect-each-company). Then:

```bash
uv run qbo-multicompany-mcp-auth --env-file .env.production status
```

Tell whoever else administers the company file that this app exists. An unfamiliar app is easy to disconnect
from the QuickBooks apps page, and that kills the company's token at once.

`status` shows each company's hard expiry, a five-year limit only a new consent restarts. Put a reminder
before it. The keepalive warns ahead of it too.

## 8. Add it to your AI client

One server process serves one environment, so production gets its own entry, with absolute paths. The env file
names the registry, so `--entities` isn't needed. `--scope user` makes the server available to every Claude Code
session on your account, so you can run those sessions outside the repo folder, away from `.env.production`:

```bash
claude mcp add --scope user qbo-prod -- "<repo>/.venv/bin/qbo-multicompany-mcp" --env-file "<repo>/.env.production"
```

Codex and Claude Desktop take the same command and arguments, as in
[getting-started.md](getting-started.md#6-add-it-to-your-ai-client).

## 9. Keep credentials alive

A refresh token unused for 100 days expires for good. Run the keepalive daily, with `--require-all` where
every configured company is meant to be connected.

**launchd (macOS).** Save as `~/Library/LaunchAgents/com.example.qbo-mcp-keepalive.plist`, with your own paths.
launchd doesn't create the log folder, so make it first.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.qbo-mcp-keepalive</string>
  <key>ProgramArguments</key>
  <array>
    <string>/absolute/path/to/repo/.venv/bin/qbo-multicompany-mcp-keepalive</string>
    <string>--env-file</string>
    <string>/absolute/path/to/.env.production</string>
    <string>--require-all</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>9</integer>
    <key>Minute</key>
    <integer>23</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/absolute/path/to/logs/keepalive.log</string>
  <key>StandardErrorPath</key>
  <string>/absolute/path/to/logs/keepalive.err</string>
</dict>
</plist>
```

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.example.qbo-mcp-keepalive.plist
```

```bash
launchctl kickstart -k "gui/$(id -u)/com.example.qbo-mcp-keepalive"
```

After editing the plist, unload it, then bootstrap it again:

```bash
launchctl bootout "gui/$(id -u)/com.example.qbo-mcp-keepalive"
```

After a macOS update, check it's still loaded:

```bash
launchctl print "gui/$(id -u)/com.example.qbo-mcp-keepalive"
```

A keychain key needs the user logged in, or the login keychain stays locked.

**cron.** Cron on Linux hasn't been tested, and cron may not reach a macOS keychain, so set
`QBO_TOKEN_KEY_SOURCE=env` and `QBO_TOKEN_KEY` when you use it. For an existing store the key is your
`export-key` value, since a new key can't read that store. That puts the store key beside the client secret in
one plain file, so prefer launchd on macOS.

```cron
23 9 * * * /absolute/path/to/repo/.venv/bin/qbo-multicompany-mcp-keepalive --env-file /absolute/path/to/.env.production --require-all >> /absolute/path/to/logs/keepalive.log 2>&1
```

| Exit | Meaning |
|---|---|
| 0 | Healthy |
| 1 | Transient, or a company needs attention. The line for that company says which |
| 2 | Configuration |

A job that never runs reports nothing, and this package can't tell it isn't running. **Monitor the schedule
itself**, for example with a dead-man check that pings only on exit 0.

## Token recovery

Start from the company's state. If `status` says the token store is unavailable, read the reason it gives, and
don't run `init-key` or `connect`. A missing or wrong key goes to "The store key is missing"; `could not read
tokens.production.journal.enc` goes to the journal paragraph below.

```bash
uv run qbo-multicompany-mcp-auth --env-file .env.production status
```

| State | What happened | Fix |
|---|---|---|
| `authorized` | Healthy | None |
| `name_mismatch` | `display_name` differs from QuickBooks's company name | Copy QuickBooks's exact name into the registry, then restart your AI client |
| `needs_consent` | Intuit rejected the token: disconnected in QuickBooks, revoked, or a limit passed | Connect again |
| `expired` | The 100-day or five-year limit passed | Connect again |
| `refresh_uncertain` | A refresh's answer was lost or couldn't be used, or the journal's contents made no sense, so nobody knows which token is live. Never retried automatically | `verify`, below |
| `malformed store row` | That company's stored record can't be read | Connect again |
| `never authorized` | Not connected yet | Connect |

**`refresh_uncertain`.** `verify`, below, explains itself, asks for confirmation, and submits the stored token
exactly once. Success clears the quarantine. If Intuit rejects the token, the company becomes `needs_consent`. After an
unreadable journal every connected company needs this or a new `connect`, and tools refuse each one until it's done.
The journal stays beside the store as `<journal>.unreadable-*`, safe to delete once each company is settled.

```bash
uv run qbo-multicompany-mcp-auth --env-file .env.production verify <entity>
```

**`could not read tokens.production.journal.enc`.** The file is there and this user can't open it. Fix its owner or
mode (0600). Don't move or delete it: it may be the only record of a refresh, and with it gone the stored token gets
resubmitted. Nothing has changed, and the next call reads it again.

**The store key is missing.** Don't run `init-key`: it would create a different key, and the existing store could
never be read again. Restore your copy. On macOS, with `-w` last, `security` prompts without echoing or keeping
it in shell history:

```bash
security add-generic-password -U -s qbo-multicompany-mcp -a token-key-production -T /usr/bin/security -w
```

With no copy, the store is unreadable for good. Stop every process using it, move **both**
`tokens.production.enc` and `tokens.production.journal.enc` aside, run `init-key`, and connect every company
again. Nothing moved aside is ever restored: it holds tokens that must be treated as dead.

**`connect` can't run.** `import` adopts a refresh token from Intuit's OAuth 2.0 Playground, supplied in
`QBO_IMPORT_REFRESH_TOKEN`. It refreshes at once to prove the token is live, checks it reads the company the
registry names, and only then stores it. This fallback hasn't been exercised on production. In your own terminal,
read the value without echo, import, and unset it:

```bash
read -rs QBO_IMPORT_REFRESH_TOKEN && export QBO_IMPORT_REFRESH_TOKEN
```

```bash
uv run qbo-multicompany-mcp-auth --env-file .env.production import <entity>
```

```bash
unset QBO_IMPORT_REFRESH_TOKEN
```

**Never restore a copied store.** There are no backups, on purpose: a copy taken before a rotation holds a token
that must be treated as dead.

### A suspected leak

1. Stop every process using the store.
2. Revoke every company in the registry. A company you've since removed from it needs its own disconnect, step 4:

   ```bash
   uv run qbo-multicompany-mcp-auth --env-file .env.production revoke --all
   ```

3. Read each company's line. `revoked at Intuit` is confirmed. A failed revocation means the local copy is gone
   but Intuit didn't confirm the token is dead, and the command exits 1. So does a stored row this build can't
   read: it's removed without a request, and its line says the token in it wasn't revoked. If the HTTP client
   can't be built, a bad `SSLKEYLOGFILE` for one, `revoke` stops before the first company, removes nothing and
   exits 2: fix that and run it again. `revoke` hasn't run against Intuit's live endpoint, so check the result in
   QuickBooks too.
4. For each company without a confirmed revocation, disconnect the app in QuickBooks, under **Integrations >
   Manage integrations**.
5. Check each company's audit log in QuickBooks for changes you didn't make: a leaked token can write, outside
   this server.
6. Rotate the client secret in the Intuit portal, then in your secret manager. That stops new token requests with
   the old secret; it doesn't prove tokens already issued are dead. Steps 3 and 4 do.
7. Connect again, company by company.
