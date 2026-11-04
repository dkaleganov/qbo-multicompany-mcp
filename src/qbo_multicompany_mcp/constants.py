"""Pinned Intuit endpoints and package-wide constants.

Every value here was confirmed against Intuit's live OpenID discovery documents
(``https://developer.api.intuit.com/.well-known/openid_configuration`` and the
``openid_sandbox_configuration`` variant; recorded under "Verified without
credentials" in docs/api-findings.md). The authorization, token and
revocation endpoints are identical for sandbox and production; only the
Accounting API origin and the userinfo endpoint differ by environment.

Discovery may *confirm* these values. It may never rewrite them: a destination
that carries a secret is pinned in source, so a compromised or spoofed
discovery document cannot redirect a refresh token somewhere else.
"""

from __future__ import annotations

from typing import Final

ENV_SANDBOX: Final = "sandbox"
ENV_PRODUCTION: Final = "production"
ENVIRONMENTS: Final = (ENV_SANDBOX, ENV_PRODUCTION)

#: Accounting API origins, keyed by environment.
API_BASES: Final[dict[str, str]] = {
    ENV_SANDBOX: "https://sandbox-quickbooks.api.intuit.com",
    ENV_PRODUCTION: "https://quickbooks.api.intuit.com",
}

AUTHORIZE_ENDPOINT: Final = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_ENDPOINT: Final = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_ENDPOINT: Final = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

DISCOVERY_URLS: Final[dict[str, str]] = {
    ENV_SANDBOX: "https://developer.api.intuit.com/.well-known/openid_sandbox_configuration",
    ENV_PRODUCTION: "https://developer.api.intuit.com/.well-known/openid_configuration",
}

#: The only scope Intuit offers for the Accounting API. It grants writes too.
#: That is precisely why read-only has to be enforced here.
SCOPE_ACCOUNTING: Final = "com.intuit.quickbooks.accounting"

#: The addresses the local listener BINDS. Both loopback literals, because a
#: browser resolving ``localhost`` may pick either family and a listener on
#: only one of them fails intermittently and machine-dependently.
CALLBACK_HOST_V4: Final = "127.0.0.1"
CALLBACK_HOST_V6: Final = "::1"
CALLBACK_PORT: Final = 8765
CALLBACK_PATH: Final = "/callback"

#: The redirect URI SENT TO INTUIT, which is a different thing from what the
#: listener binds, and the distinction is the whole point of this block.
#:
#: The developer portal's Redirect URIs screen states (read 2026-09-20;
#: Intuit's page on redirect URIs carries the IP-address rule too): "The URI
#: itself cannot be an IP address or a URI
#: fragment or relative path." So ``http://localhost:8765/callback`` is
#: registrable and ``http://127.0.0.1:8765/callback`` is not -- a live consent
#: attempt with the latter failed with "The redirect_uri query parameter value
#: is invalid".
#:
#: The listener still binds both loopback literals above. Only the *name* sent
#: to Intuit has to be ``localhost``.
#:
#: ``docs/callback.html`` deliberately keeps targeting the ``127.0.0.1``
#: literal. That hop is browser-to-loopback, local only; Intuit never sees it,
#: so the IP literal is correct there and must not be "fixed" to match this.
DEFAULT_REDIRECT_URI: Final = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

#: Intuit Accounting API minor version. Pinned so a server-side default bump
#: cannot silently change response shapes under the projections.
MINOR_VERSION: Final = "75"

#: Network budgets. The store's lock wait is derived from the first of these so
#: a lock wait outlasts an ordinary in-flight token POST and the write that
#: follows it; nothing bounds a slow token response (see
#: :mod:`qbo_multicompany_mcp.store`).
OAUTH_POST_TIMEOUT_SECONDS: Final = 30.0
#: Upstream times a request out at 120 seconds (Intuit's page on API call
#: limits and throttles). Ours sits at that mark so a hung request is released
#: by us at about the moment upstream gives up rather than hanging on past it.
ACCOUNTING_TIMEOUT_SECONDS: Final = 120.0

#: The storage host an Attachable's ``TempDownloadUri`` named, OBSERVED
#: 2026-09-25 in sandbox (the sample on Intuit's page elides it). SANDBOX ONLY:
#: production has not been observed and may name another host. This is NOT a
#: default pin -- no arbitrary storage host is admitted, and nothing is
#: contacted until the operator pins a host in ``QBO_ATTACHMENT_HOSTS`` -- it
#: is what the refusals name so the operator knows what to pin.
OBSERVED_ATTACHMENT_HOSTS_SANDBOX: Final[tuple[str, ...]] = ("financialdocument.platform.intuit.com",)

#: The sentence every no-host refusal carries (attachments, the tool, the
#: network policy, the CLI help). Fixed text; names no caller value.
ATTACHMENT_HOST_HINT: Final = (
    "the storage host observed in sandbox on 2026-09-25 was "
    f"{OBSERVED_ATTACHMENT_HOSTS_SANDBOX[0]}; production has not been observed "
    "and may differ, so confirm the host one attachment's link names before "
    "pinning it"
)

#: The whole-call budget: one monotonic deadline per tool call, covering lock
#: waits, rate-limit waits, guard reads, every page, every retry and its sleep.
#: Each network request gets what is left of it, less the reserve below.
#: Enforced by :mod:`qbo_multicompany_mcp.deadline`.
PER_CALL_BUDGET_SECONDS: Final = 150.0
#: Held back from the budget for building the result after the last request.
#: A read that finished at the deadline would leave no time to answer with it.
SERIALIZATION_RESERVE_SECONDS: Final = 5.0

#: Decoded-byte caps on a response body. Counted after any content decoding,
#: because a compressed body is small on the wire and large in memory.
MAX_JSON_RESPONSE_BYTES: Final = 32 * 1024 * 1024
MAX_REPORT_RESPONSE_BYTES: Final = 16 * 1024 * 1024

#: Retries for idempotent reads: 5xx only, three retries, base one second,
#: factor two, full jitter. Never a 504 or a read timeout on a report, and
#: never a 429 (see limits.py).
RETRY_ATTEMPTS: Final = 3
RETRY_BASE_SECONDS: Final = 1.0
RETRY_FACTOR: Final = 2.0
DISCOVERY_TIMEOUT_SECONDS: Final = 15.0
STORE_WRITE_BUDGET_SECONDS: Final = 5.0

#: The refresh token has TWO clocks (Intuit's Authorization FAQ).
#:
#: The INACTIVITY clock: 100 days, restarted when Intuit issues a new
#: refresh-token value, at most once per 24 hours (observed 2026-09-26). It is what the
#: token response's ``x_refresh_token_expires_in`` reports. This nominal value
#: is used only when a response omits that field.
REFRESH_TOKEN_NOMINAL_DAYS: Final = 100
#: The HARD clock: a five-year maximum that only re-authorization resets. The
#: value is the one Intuit's own example responses show for the opt-in field
#: ``x_refresh_token_hard_expires_in`` (its Set up OAuth 2.0 page): 157,680,000
#: seconds, five years of 365 days. Every token response has carried that field
#: since the opt-in was sent (observed 2026-09-24, both sandbox realms); the
#: hard clock is read when present and otherwise reported as unknown, never as
#: an invented date; this constant gives an upper bound from a connect's
#: consent date, and nothing more.
REFRESH_TOKEN_HARD_LIMIT_SECONDS: Final = 157_680_000
#: Access tokens live one hour (Intuit's Authorization FAQ).
ACCESS_TOKEN_NOMINAL_SECONDS: Final = 3600

#: The hard-expiry opt-in. The response field ``x_refresh_token_hard_expires_in``
#: is returned only when the request opts in, and Intuit's Set up OAuth 2.0 page
#: shows the opt-in two ways: a body parameter of the same name in its
#: parameter tables and this header in its request examples. The HEADER form is
#: what is sent, on exchange and on refresh: the refresh body policy in
#: :mod:`netpolicy` admits exactly two form keys, so the body form would be
#: refused by this package's own policy before it left. The header form was
#: observed working on 2026-09-24: every sandbox token response carried the
#: field.
HARD_EXPIRY_OPT_IN_HEADER: Final = "x-include-refresh-token-hard-expires-in"
HARD_EXPIRY_OPT_IN_VALUE: Final = "true"

#: The User-Agent every request from this package carries. Here rather than in
#: :mod:`oauth` so the network policy can pin it on token requests without an
#: import cycle; ``oauth.USER_AGENT`` still names it.
USER_AGENT: Final = "qbo-multicompany-mcp"

#: ``last_refresh_at`` older than this makes a realm "stale" for keepalive and
#: for the ``any_stale`` field of ``server_info``.
STALE_REFRESH_SECONDS: Final = 3600.0

#: Warn this far ahead of the INACTIVITY expiry. A refresh restores it, so
#: two weeks is ample notice that a scheduled keepalive has stopped.
REFRESH_INACTIVITY_WARN_DAYS: Final = 14
#: Warn this far ahead of the HARD expiry, observed or its upper bound. Only
#: a human re-consent restores it, so the notice is longer.
REFRESH_HARD_EXPIRY_WARN_DAYS: Final = 30

STORE_SCHEMA_VERSION: Final = 1
