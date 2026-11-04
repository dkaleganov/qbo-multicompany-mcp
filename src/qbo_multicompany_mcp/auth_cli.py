"""``qbo-multicompany-mcp-auth``: consent, import, status, revoke, key handling.

This tool never asks anyone to paste a secret, apart from the callback URL
``connect --manual`` takes. Client credentials come from ``QBO_CLIENT_ID`` and
``QBO_CLIENT_SECRET``; an imported refresh token comes from
``QBO_IMPORT_REFRESH_TOKEN``. When one is missing, the tool names the variable
and stops. A tool that prompts for secrets teaches people to type secrets into
whatever prompts them.

The consent flow's order is deliberate, and each step relies on the last:

1. Bind the loopback listener on both literals, on the fixed port. A failure
   here exits 2 -- never a different port, because the redirect URI registered
   with Intuit is fixed.
2. Only then open the browser.
3. Validate ``state``: 32 random bytes, new for each attempt, single use, 15
   minutes, constant-time. An invalid local request is answered 400 and does
   **not** consume the attempt.
4. **Compare the callback's realm with the registry realm, before any token
   exchange.** A mismatch is fatal and cannot be overridden by a matching
   company name -- identical company names are entirely possible, and the
   realm is the identity.
5. Exchange the code at most once, including after a timeout.
6. Commit under the canonical lock, which is taken only now -- after the human
   part is over.

The authorization code and the callback query string are never logged.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import webbrowser
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from urllib.parse import urlencode  # parsing only, no network capability

from . import oauth
from .callback_server import (
    Attempt,
    AttemptAlreadyUsed,
    CallbackParams,
    CallbackRejected,
    LoopbackReceiver,
    parse_pasted_url,
)
from .config import (
    ENV_IMPORT_REFRESH_TOKEN,
    Settings,
    add_common_arguments,
    register_environment_secrets,
    settings_from_args,
)
from .constants import (
    AUTHORIZE_ENDPOINT,
    CALLBACK_HOST_V4,
    CALLBACK_PATH,
    SCOPE_ACCOUNTING,
)
from .crypto import (
    ENV_KEY_VAR,
    KeySource,
    keychain_create_key,
    keychain_export_key,
    keychain_has_key,
)
from .errors import (
    ConfigurationError,
    MalformedRowError,
    NeedsConsentError,
    QboMcpError,
    RefreshUncertainError,
    StaleConsentError,
    StartupError,
)
from .guard import describe_mismatch, names_match
from .redaction import ValueFreeParser, install_redacting_hooks, redact
from .registry import Entity, Registry
from .session import company_info_body
from .store import AppCredentials, ConsentBaseline, RefreshActor, TokenRow, TokenStore

if TYPE_CHECKING:
    import httpx

#: How long to wait for a callback. Matches the state's own lifetime; waiting
#: longer would only ever accept a state that has already expired.
CONSENT_WAIT_SECONDS: Final = 15 * 60

ROLE: Final = "auth-cli"


def out(message: str = "") -> None:
    print(message)


def _say(*lines: str) -> None:
    """Print the lines a step prints on its way to a revoke.

    A terminal that has closed can fail them, and neither the revoke after
    them nor the error that ended the step may be lost to that.
    """
    with contextlib.suppress(OSError, ValueError):
        for line in lines:
            out(line)


# --------------------------------------------------------------------------
# connect
# --------------------------------------------------------------------------


def authorize_url(*, client_id: str, state: str, redirect_uri: str) -> str:
    """Build the consent URL.

    ``redirect_uri`` must be byte-identical to the one registered with Intuit
    and to the one sent in the later token exchange; a mismatch is a documented
    cause of ``invalid_grant`` at the exchange (Intuit's Authorization FAQ).

    ``code_challenge`` is deliberately absent. Whether Intuit accepts it on
    this flow hasn't been verified, and sending an unverified parameter risks a
    consent that fails for reasons that are hard to diagnose from the outside.
    The client secret authenticates the exchange; it does not bind the code to
    this attempt the way a PKCE verifier would, so this package claims no PKCE
    protection. Add it only once a sandbox consent has been *observed* to
    succeed with it present.
    """
    return f"{AUTHORIZE_ENDPOINT}?" + urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE_ACCOUNTING,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )


def cmd_connect(settings: Settings, registry: Registry, args: argparse.Namespace) -> int:
    entity = _resolve(registry, args.entity)
    credentials = settings.require_credentials()
    store = settings.open_store(credentials=credentials)
    _refuse_plaintext_write(settings)
    # What this ceremony begins from, captured before the browser opens. If the
    # realm is revoked or another consent lands while the human is at the
    # consent screen, the commit below stops.
    baseline = store.consent_baseline(entity.realm_id)

    attempt = Attempt(
        entity_key=entity.key,
        environment=settings.environment,
        client_fingerprint=credentials.fingerprint,
        redirect_uri=settings.redirect_uri,
    )

    receiver = LoopbackReceiver(attempt, port=settings.callback_port)
    # Bind first. A StartupError here propagates to main() and exits 2.
    receiver.bind()
    receiver.start()

    try:
        url = authorize_url(
            client_id=credentials.client_id,
            state=attempt.state,
            redirect_uri=settings.redirect_uri,
        )
        out(f"Connecting entity '{entity.key}' ({settings.environment}).")
        # These are two different addresses whenever the registered redirect is
        # an HTTPS forwarder, and an operator debugging a redirect needs both.
        out(
            f"Listening on http://{CALLBACK_HOST_V4}:{receiver.bound_port}{CALLBACK_PATH} "
            f"(and the IPv6 loopback)."
        )
        out(f"Redirect URI sent to Intuit: {settings.redirect_uri}")

        if args.manual:
            out("")
            out("Open this URL in a browser, approve access, then paste the address")
            out("the browser ends up on:")
            out("")
            out(f"  {url}")
            out("")
            params = _await_manual(attempt, receiver)
        else:
            out("Opening your browser to approve access...")
            if not webbrowser.open(url):
                out("Could not open a browser. Open this URL yourself:")
                out(f"  {url}")
            params = receiver.wait(CONSENT_WAIT_SECONDS)

        if params is None:
            out("")
            out("Timed out waiting for the consent callback. Nothing was changed.")
            return 1

        return _finish_connect(
            settings, store, entity, credentials, attempt, params, baseline=baseline
        )
    finally:
        receiver.close()


def _await_manual(attempt: Attempt, receiver: LoopbackReceiver) -> CallbackParams | None:
    """Read a pasted callback URL, competing with the live listener.

    The listener stays up during the manual flow on purpose. If the user's
    browser does reach loopback, whichever path arrives first claims the single
    attempt and the other is told so plainly -- rather than both proceeding and
    one of them exchanging an already-spent code.
    """
    while True:
        if attempt.completed.is_set():
            out("The browser delivered the callback; ignoring the prompt.")
            return attempt.params
        try:
            pasted = input("Paste the callback URL (or press Enter to cancel): ").strip()
        except EOFError:
            return None
        if not pasted:
            return None
        try:
            params = parse_pasted_url(pasted)
            attempt.claim(params)
        except AttemptAlreadyUsed as exc:
            out(f"  {exc}")
            return attempt.params
        except CallbackRejected as exc:
            # The attempt is untouched; the user can paste the right URL.
            out(f"  Not accepted: {exc}")
            continue
        return params


def _finish_connect(
    settings: Settings,
    store: TokenStore,
    entity: Entity,
    credentials: AppCredentials,
    attempt: Attempt,
    params: CallbackParams,
    baseline: ConsentBaseline | None = None,
) -> int:
    # Before any token exchange: the realm decides. A company name that happens
    # to match cannot override this, because two QuickBooks companies may carry
    # byte-identical names.
    if params.realm_id != entity.realm_id:
        out("")
        out("REFUSING: the company you approved is not the one this entity names.")
        out(f"  entity:            {entity.key}")
        # Last four of both, like every other realm this tool prints: this output
        # is exactly what ends up in a terminal log.
        out(f"  registry realm:    ...{entity.realm_id_last4}")
        out(f"  approved realm:    ...{params.realm_id[-4:]}")
        out("")
        out("No token was requested and nothing was stored. Either approve the other")
        out("company, or correct realm_id in entities.yaml, taking the full Company ID")
        out("from QuickBooks. This tool never prints one.")
        return 2

    attempt.begin_exchange()
    response = oauth.exchange_code(
        code=params.code,
        # Byte-identical to the one sent on the authorize request; Intuit
        # rejects the exchange otherwise.
        redirect_uri=settings.redirect_uri,
        client_id=credentials.client_id,
        client_secret=credentials.client_secret,
        environment=settings.environment,
    )

    with _UnstoredPair(settings, credentials, response) as pair:
        if response.realm_id is not None and response.realm_id != entity.realm_id:
            out("")
            out("REFUSING: the token Intuit issued is for a different realm.")
            out("Nothing was stored.")
            pair.discard()
            return 2

        # The guard runs HERE: after the exchange, before anything is stored.
        # Otherwise a consent prints only a realm id and expiries, and a
        # wrong-company approval is written to disk and discovered only at
        # first use.
        intuit_name = _probe_company_name(settings, store, entity, response.access_token)

        if intuit_name is None:
            out("")
            out("Could not read the company name to verify this connection.")
            out("Refusing to store an unverified credential; nothing was written.")
            pair.discard()
            return 2

        matches = names_match(entity.display_name, intuit_name)

        if not matches:
            out("")
            out("COMPANY NAME MISMATCH")
            out(describe_mismatch(entity.display_name, intuit_name))
            out("")
            out("The realm is correct, so this is the right company file -- but the")
            out("registry's display_name does not match what QuickBooks reports.")
            if not _confirm("Store this credential anyway?"):
                out("Nothing was stored.")
                pair.discard()
                return 2

    try:
        row = store.commit_consent(
            entity=entity.key,
            realm_id=entity.realm_id,
            response=response,
            actor=RefreshActor.here(ROLE),
            baseline=baseline,
            origin="connect",
        )
    except StaleConsentError as exc:
        _say("", "STALE CEREMONY", f"  {exc}")
        _discard_unstored_tokens(settings, credentials, response)
        return 1
    except QboMcpError as exc:
        # The exchange spent the grant, and a pair nobody holds shouldn't stay live.
        _say(f"Could not store the credential: {redact(exc)}")
        _discard_unstored_tokens(settings, credentials, response)
        return 1

    out("")
    out(f"Connected '{entity.key}'.")
    out(f"  display name (registry): {entity.display_name!r}")
    out(f"  company name (QuickBooks): {intuit_name!r}")
    out(f"  names match:             {matches}")
    out(f"  realm id (last 4):       ...{entity.realm_id_last4}")
    out(f"  access token expires in: {row.days_to_access_expiry():.2f} days")
    out(f"  inactivity expiry in:    {row.days_to_inactivity_expiry():.1f} days (restarted when a refresh is issued a new value, at most daily)")
    out(f"  hard expiry:             {row.hard_expiry().describe()}")
    if not matches:
        out("")
        out("Put this exact value in entities.yaml as display_name for "
            f"'{entity.key}':")
        out("")
        out(f"    display_name: {_yaml_scalar(intuit_name)}")
        out("")
        out("Until it matches, every tool except get_company_info refuses this")
        out("entity.")
    out("")
    out("Refresh tokens rotate, and expire for good if unused.")
    out("Run qbo-multicompany-mcp-keepalive on a schedule to keep the chain alive.")
    return 0


def _yaml_scalar(text: str) -> str:
    """A double-quoted scalar that parses back to exactly ``text``: a name can
    hold ': ' or ' #', or end in a space. JSON's escapes are valid YAML, and the
    characters JSON leaves raw that a YAML loader refuses or folds are escaped too."""
    quoted = json.dumps(text, ensure_ascii=False)
    unsafe = "[\x7f-\x9f\u2028\u2029\ufeff\ud800-\udfff\ufffe\uffff]"
    return re.sub(unsafe, lambda match: f"\\u{ord(match.group()):04x}", quoted)


def _probe_company_name(
    settings: Settings, store: TokenStore, entity: Entity, access_token: str
) -> str | None:
    """Read CompanyName with the freshly issued token, storing nothing."""
    from .client import AccountingClient

    with AccountingClient(settings, store, actor_role=ROLE) as client:
        try:
            info = client.company_info_with_token(entity, access_token)
        except QboMcpError as exc:
            out(f"  ({redact(exc)})")
            return None
    name = company_info_body(info.payload).get("CompanyName")
    return name if isinstance(name, str) else None


def _confirm(question: str) -> bool:
    """Explicit confirmation, and only from a real terminal.

    Without a terminal there is nobody to confirm, so the answer is no. A
    non-interactive run that silently stored a mismatched connection would
    defeat the point of asking.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        out("  (not an interactive terminal, so this cannot be confirmed)")
        return False
    try:
        answer = input(f"{question} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _discard_unstored_tokens(
    settings: Settings, credentials: AppCredentials, response: oauth.TokenResponse
) -> None:
    """Revoke a pair we decided not to keep.

    Best effort. Leaving a live refresh token issued and unrevoked is untidy
    rather than dangerous, and a failure to revoke must not read as a failure
    to protect anything -- nothing was stored either way.
    """
    try:
        oauth.revoke_token(
            token=response.refresh_token,
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            environment=settings.environment,
        )
    except QboMcpError:
        _say("  (could not revoke the unstored credential; it will expire unused)")
    else:
        _say("  (the unstored credential was revoked at Intuit)")


class _UnstoredPair:
    """A pair Intuit has issued that no store holds yet.

    A refusal revokes it through :meth:`discard`. Leaving the ``with`` block on
    an error or an interrupt revokes it too, and the error still propagates:
    left to the refusals alone, a failure none of them names, such as an
    interrupt at the prompt or a rate-limit lock file that can't be opened,
    would leave the pair live with no local copy.
    The block ends before the commit. An unexpected error from inside a commit
    leaves it unknown whether the pair was stored, and revoking a stored pair
    would break the connection, so past the block the commit's own handling
    decides.
    """

    def __init__(
        self, settings: Settings, credentials: AppCredentials, response: oauth.TokenResponse
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._response = response
        self._open = True

    def discard(self) -> None:
        """Ask Intuit to revoke the pair, once."""
        if self._open:
            self._open = False
            _discard_unstored_tokens(self._settings, self._credentials, self._response)

    def __enter__(self) -> _UnstoredPair:
        return self

    def __exit__(self, kind: type[BaseException] | None, *rest: object) -> None:
        if kind is not None and self._open:
            try:
                _say("", "Stopped before the credential was stored; nothing was written.")
            finally:
                # An interrupt while the lines print still gets the revoke.
                self.discard()


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------


def cmd_import(settings: Settings, registry: Registry, args: argparse.Namespace) -> int:
    """Adopt an existing refresh token, supplied through the environment.

    The token is immediately refreshed, which both proves it is live and
    replaces it with one this store owns. It is then verified against the
    registry realm with a real read: a refresh response does not name a realm,
    so the only honest way to learn which company a token belongs to is to use
    it.

    Verified **before** it is stored, with the fresh access token and storing
    nothing, exactly as ``connect`` does. Committing first and verifying
    second would overwrite a healthy pair with an unverified one and then, on
    a failed read, remove the row: a failed replacement would destroy the
    connection that worked. A failed verification stores nothing, revokes the
    unstored pair, and leaves whatever consent the realm had exactly as it
    was; the commit that follows a successful verification is checked against
    the baseline captured before the token endpoint was called, so a consent
    that landed meanwhile stops it.
    """
    import os

    entity = _resolve(registry, args.entity)
    credentials = settings.require_credentials()
    _refuse_plaintext_write(settings)

    supplied = os.environ.get(ENV_IMPORT_REFRESH_TOKEN, "").strip()
    if not supplied:
        raise StartupError(
            f"set {ENV_IMPORT_REFRESH_TOKEN} in the environment to the refresh token "
            f"to import. This tool never asks you to paste a secret."
        )

    store = settings.open_store(credentials=credentials)
    baseline = store.consent_baseline(entity.realm_id)

    out(f"Importing a refresh token for '{entity.key}' ({settings.environment}).")
    response = oauth.refresh_tokens(
        refresh_token=supplied,
        client_id=credentials.client_id,
        client_secret=credentials.client_secret,
        environment=settings.environment,
        from_cli=True,
    )

    # Verify the token really belongs to the registry realm, storing nothing.
    with _UnstoredPair(settings, credentials, response) as pair:
        intuit_name = _probe_company_name(settings, store, entity, response.access_token)
        if intuit_name is None:
            out("")
            out("REFUSING: that token could not read the realm this entity names.")
            out("Nothing was stored; any consent this entity already had is untouched.")
            pair.discard()
            return 2

    try:
        row = store.commit_consent(
            entity=entity.key,
            realm_id=entity.realm_id,
            response=response,
            actor=RefreshActor.here(ROLE),
            baseline=baseline,
            origin="import",
        )
    except StaleConsentError as exc:
        _say("", "STALE CEREMONY", f"  {exc}")
        _discard_unstored_tokens(settings, credentials, response)
        return 1
    except QboMcpError as exc:
        # The refresh spent the supplied token, and a pair nobody holds shouldn't stay live.
        _say(f"Could not store the credential: {redact(exc)}")
        _discard_unstored_tokens(settings, credentials, response)
        return 1

    out("")
    out(f"Imported '{entity.key}'.")
    out(f"  realm id (last 4):       ...{entity.realm_id_last4}")
    out(f"  inactivity expiry in:    {row.days_to_inactivity_expiry():.1f} days (restarted when a refresh is issued a new value, at most daily)")
    # An imported grant is older than this record of it, so no bound is derived.
    out(f"  hard expiry:             {row.hard_expiry().describe()}")
    if not names_match(entity.display_name, intuit_name):
        out("")
        out("  WARNING: the company name does not match the registry.")
        out(describe_mismatch(entity.display_name, intuit_name))
    return 0


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def cmd_status(settings: Settings, registry: Registry, args: argparse.Namespace) -> int:
    """Report every entity from the store. Makes no request unless a mismatch
    is already recorded, in which case it fetches the two names to show."""
    store = settings.open_store()
    malformed: dict[str, object] = {}
    try:
        document = store.read()
        rows = document.rows
        malformed = document.malformed
    except (ConfigurationError, QboMcpError) as exc:
        # Not "never authorized": that would invite a connect over a store that
        # may only need its key back.
        out(f"Token store unavailable: {redact(exc)}")
        out("No company's state can be read until the store opens.")
        return 2 if isinstance(exc, ConfigurationError) else 1

    now = datetime.now(UTC)
    mismatched: list[Entity] = []

    out(f"environment: {settings.environment}")
    out(f"token store: {store.document_path}")
    out("")
    for entity in registry:
        row = rows.get(entity.realm_id)
        out(f"{entity.key}")
        out(f"  display name (registry): {entity.display_name!r}")
        # Last four only, as everywhere a realm id is printed. The rest of this
        # output is still private: the store path, the companies' names and
        # the host that last refreshed.
        out(f"  realm id (last 4):       ...{entity.realm_id_last4}")
        if entity.realm_id in malformed:
            out("  state:                   malformed store row (this entity only)")
            out(f"  reason:                  {malformed[entity.realm_id].reason}")
            out(f"  replace with:            qbo-multicompany-mcp-auth connect {entity.key}")
            out("")
            continue
        if row is None:
            out("  state:                   never authorized")
            out(f"  connect with:            qbo-multicompany-mcp-auth connect {entity.key}")
            out("")
            continue

        out(f"  state:                   {row.auth_state(now=now, registry_display_name=entity.display_name)}")
        out(f"  consented at:            {row.consented_at}")
        out(f"  auth generation:         {row.auth_generation}")
        out(f"  last refresh:            {row.last_refresh_at or 'never'}")
        age = row.refresh_age_seconds(now=now)
        out(f"  refresh age:             {'unknown' if age is None else f'{age / 60:.1f} min'}")
        out(f"  stale (this realm):      {row.is_stale(now=now)}")
        out(f"  access token expires in: {row.days_to_access_expiry(now=now):.2f} days")
        out(f"  inactivity expiry in:    {row.days_to_inactivity_expiry(now=now):.1f} days (restarted when a refresh is issued a new value, at most daily)")
        out(f"  hard expiry:             {row.hard_expiry().describe(now=now)}")
        if row.last_refresh_by:
            actor = row.last_refresh_by
            out(f"  last refreshed by:       {actor.role} pid {actor.pid} on {actor.hostname}")
        if row.refresh_uncertain and row.recovery_hint:
            out(f"  ATTENTION:               {row.recovery_hint}")
        verdict = row.guard_verdict_for(entity.display_name)
        if verdict is not None and verdict.mismatch:
            out("  name mismatch:           yes (fetching both values below)")
            mismatched.append(entity)
        elif row.name_mismatch is not None and verdict is None:
            out("  guard verdict:           stale (display_name changed since it was checked;")
            out("                           re-verified on the entity's next use)")
        out("")

    if mismatched:
        _report_mismatches(settings, store, mismatched)
    return 0


def _report_mismatches(settings: Settings, store: TokenStore, entities: list[Entity]) -> None:
    from .client import AccountingClient

    out("name mismatches")
    out("---------------")
    with AccountingClient(settings, store, actor_role=ROLE) as client:
        for entity in entities:
            out(f"{entity.key}:")
            try:
                info = client.company_info(entity)
            except QboMcpError as exc:
                out(f"  could not read the company name: {redact(exc)}")
                continue
            intuit_name = company_info_body(info.payload).get("CompanyName")
            if not isinstance(intuit_name, str):
                out("  QuickBooks returned no company name")
                continue
            # repr() on both, so an invisible difference is visible on the page.
            out(describe_mismatch(entity.display_name, intuit_name))
    out("")


# --------------------------------------------------------------------------
# revoke
# --------------------------------------------------------------------------


def cmd_revoke(settings: Settings, registry: Registry, args: argparse.Namespace) -> int:
    """Revoke at Intuit, then remove locally.

    The stored token is read through ``usable_row``, the same binding check
    every other send makes: environment, app and entity. Read from the
    document directly, it would be submitted under whatever app the CLI was
    configured with, then removed whether Intuit accepted or not -- so a wrong
    ``QBO_ENV`` or a stale ``QBO_CLIENT_ID`` would destroy a valid connection.
    A mismatch is a configuration error: nothing is submitted, the row stays,
    exit 2.

    A malformed row can't be bound, so nothing in it is submitted: it is
    removed locally, and the run exits 1, because a token this build can't
    read may still be live at Intuit until the app is disconnected there. A
    realm with nothing stored submits nothing either, and still goes through
    the store so its consent epoch advances: every revoke cancels a ceremony
    in flight, present row or not.
    """
    credentials = settings.require_credentials()
    store = settings.open_store(credentials=credentials)

    if args.all:
        targets = list(registry)
    elif args.entity:
        targets = [_resolve(registry, args.entity)]
    else:
        raise StartupError("revoke needs an entity, or --all")

    # One client, built before the first company. A revoke that can't be sent
    # must not remove what it was to revoke, so this failure leaves every row
    # for a rerun.
    try:
        http = oauth.token_client()
    except oauth.OAuthError as exc:
        raise StartupError(
            f"{exc}, so nothing was revoked or removed. Check SSLKEYLOGFILE and the CA "
            f"bundle, then run revoke again"
        ) from exc
    try:
        return _revoke_each(settings, store, credentials, targets, http)
    finally:
        http.close()


def _revoke_each(
    settings: Settings,
    store: TokenStore,
    credentials: AppCredentials,
    targets: list[Entity],
    http: httpx.Client,
) -> int:
    """Revoke each company in turn; one company's failure never ends the run."""
    exit_code = 0
    for entity in targets:
        try:
            row = store.usable_row(entity.realm_id, entity_key=entity.key)
        except MalformedRowError:
            out(
                f"{entity.key}: this build can't read the stored row, so any token in it "
                f"was not revoked at Intuit; disconnect the app in QuickBooks to be sure"
            )
            exit_code = max(exit_code, 1)
            try:
                arrived = store.revoke_row(entity.realm_id)
            except QboMcpError as exc:
                # One company's store failure doesn't end a revoke --all.
                out(f"{entity.key}: the store could not be updated ({redact(exc)}); run revoke again")
                continue
            out(f"{entity.key}: removed from the local store")
            exit_code = max(exit_code, _revoke_arrived(settings, store, credentials, entity, arrived, http))
            continue
        except ConfigurationError as exc:
            out(f"{entity.key}: {redact(exc)}")
            out(f"{entity.key}: nothing was submitted and the stored row is unchanged")
            exit_code = 2
            continue
        except QboMcpError as exc:
            out(f"{entity.key}: {redact(exc)}")
            out(f"{entity.key}: nothing was submitted and the stored row is unchanged")
            exit_code = max(exit_code, 1)
            continue
        if row is None:
            # No token to submit, but a revoke is still a revoke: the realm's
            # consent epoch advances so a ceremony that began before this
            # command cannot commit after it.
            out(f"{entity.key}: nothing stored")
            try:
                arrived = store.revoke_row(entity.realm_id)
            except QboMcpError as exc:
                out(f"{entity.key}: the store could not be updated ({redact(exc)}); run revoke again")
                exit_code = max(exit_code, 1)
                continue
            out(f"{entity.key}: any consent ceremony begun before this revoke is cancelled")
            exit_code = max(exit_code, _revoke_arrived(settings, store, credentials, entity, arrived, http))
            continue
        try:
            oauth.revoke_token(
                token=row.refresh_token,
                client_id=credentials.client_id,
                client_secret=credentials.client_secret,
                environment=settings.environment,
                client=http,
            )
            out(f"{entity.key}: revoked at Intuit")
        except QboMcpError as exc:
            # Still drop the local row: a token we cannot revoke is a token we
            # should certainly not keep using.
            out(f"{entity.key}: revocation call failed ({redact(exc)}); removing locally anyway")
            exit_code = max(exit_code, 1)
        try:
            removed = store.revoke_row(entity.realm_id)
        except QboMcpError as exc:
            # One company's store failure doesn't end a revoke --all.
            out(f"{entity.key}: the local row could not be removed ({redact(exc)}); run revoke again")
            exit_code = max(exit_code, 1)
            continue
        out(f"{entity.key}: removed from the local store")
        if removed is not None and removed.refresh_token != row.refresh_token:
            # The revocation request above ran unlocked, and another process
            # stored a pair for this realm meanwhile, by a refresh or a new
            # consent: revoke_row handed it back, so it goes the way of any
            # pair that arrives mid-revoke.
            exit_code = max(exit_code, _revoke_arrived(settings, store, credentials, entity, removed, http))
    return exit_code


def _revoke_arrived(
    settings: Settings,
    store: TokenStore,
    credentials: AppCredentials,
    entity: Entity,
    arrived: TokenRow | None,
    http: httpx.Client,
) -> int:
    """Revoke a pair stored between this company's read and its removal.

    A consent can commit in that gap, and the removal then hands its row back.
    Left alone, it would be live at Intuit with no local copy. Best effort, as
    the other revocations here are.
    """
    if arrived is None:
        return 0
    try:
        # The binding check every other send makes: a consent from a process
        # configured with another app isn't sent under this one.
        store.check_binding(arrived, entity_key=entity.key)
    except ConfigurationError:
        out(f"{entity.key}: a pair stored during the revoke belongs to another configuration, so it "
            f"was not sent; disconnect the app in QuickBooks")
        return 1
    try:
        oauth.revoke_token(
            token=arrived.refresh_token,
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            environment=settings.environment,
            client=http,
        )
    except QboMcpError as exc:
        out(f"{entity.key}: a pair stored during the revoke could not be revoked "
            f"({redact(exc)}); disconnect the app in QuickBooks")
        return 1
    out(f"{entity.key}: a pair stored during the revoke was revoked as well")
    return 0


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def cmd_verify(settings: Settings, registry: Registry, args: argparse.Namespace) -> int:
    """The one deliberate exit from ``refresh_uncertain``.

    Awkward on purpose: one entity at a time, an explanation of what is about
    to happen, and an interactive confirmation. It submits the stored refresh
    token exactly once, under the lock, and clears the quarantine only on a
    validated success; ``invalid_grant`` marks the realm ``needs_consent``.
    There is no switch that clears the flag without submitting anything,
    because a flag cleared by hand says the credential is live when nobody
    has checked.
    """
    entity = _resolve(registry, args.entity)
    credentials = settings.require_credentials()
    store = settings.open_store(credentials=credentials)

    row = store.get_row(entity.realm_id)
    if row is None:
        out(f"{entity.key}: never authorized; nothing to verify")
        return 2
    state = row.auth_state(now=datetime.now(UTC), registry_display_name=entity.display_name)
    if not row.refresh_uncertain:
        out(f"{entity.key}: state is {state}; nothing to verify, nothing was submitted")
        return 0

    out(f"{entity.key}: the last refresh outcome for this realm is unknown.")
    if row.recovery_hint:
        out(f"  {row.recovery_hint}")
    out("")
    out("verify submits the STORED refresh token to Intuit exactly once. If Intuit")
    out("accepts it, the realm is authorized again with the new pair. If Intuit")
    out("answers invalid_grant, the token was already spent and the realm is")
    out("marked needs_consent -- the same place a re-consent would start from.")
    if not _confirm("Submit the stored refresh token once?"):
        out("Nothing was submitted and nothing was changed.")
        return 1

    try:
        outcome = store.verify_uncertain(
            entity.realm_id, actor=RefreshActor.here(ROLE), entity_key=entity.key
        )
    except NeedsConsentError as exc:
        out(f"{entity.key}: {redact(exc)}")
        return 1
    except RefreshUncertainError as exc:
        out(f"{entity.key}: still uncertain: {redact(exc)}")
        return 1

    verified = outcome.row
    out(
        f"{entity.key}: {verified.auth_state(registry_display_name=entity.display_name)}; "
        f"generation {verified.auth_generation}"
    )
    return 0


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------


def cmd_init_key(settings: Settings, registry: Registry, args: argparse.Namespace) -> int:
    """Create the store key if it does not exist. Idempotent; never overwrites.

    Overwriting would leave every stored refresh token permanently
    undecryptable while the file sat there looking fine -- and under this
    rotation model there is no backup that could hold a live replacement.
    """
    if settings.key_source is KeySource.ENV:
        out(f"QBO_TOKEN_KEY_SOURCE=env: provide the key in {ENV_KEY_VAR}. Nothing to do.")
        return 0
    if settings.key_source is KeySource.NONE:
        out("QBO_TOKEN_KEY_SOURCE=none: the store is plaintext. No key is used.")
        return 0

    if keychain_has_key(settings.environment):
        out(f"A key already exists for environment '{settings.environment}'. Unchanged.")
        return 0
    keychain_create_key(settings.environment)
    out(f"Created a token store key for environment '{settings.environment}'.")
    return 0


def cmd_export_key(settings: Settings, registry: Registry, args: argparse.Namespace) -> int:
    """Print the store key, only to an interactive terminal.

    Into a pipe or a redirect, key material lands in a file, a CI log or a
    scrollback buffer that outlives the session that asked for it.
    """
    if settings.key_source is not KeySource.KEYCHAIN:
        raise StartupError("export-key applies only to QBO_TOKEN_KEY_SOURCE=keychain")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise StartupError(
            "export-key requires an interactive terminal; it refuses to write key "
            "material into a pipe, a file or a log."
        )
    out(keychain_export_key(settings.environment))
    return 0


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


def _resolve(registry: Registry, key: str) -> Entity:
    entity = registry.get(key)
    if entity is None:
        known = ", ".join(sorted(registry.keys())) or "(none configured)"
        raise StartupError(f"unknown entity. Configured entities: {known}")
    return entity


def _refuse_plaintext_write(settings: Settings) -> None:
    if settings.key_source is KeySource.NONE and not settings.allow_plaintext_store:
        raise StartupError(
            "QBO_TOKEN_KEY_SOURCE=none would store refresh tokens in clear. "
            "Pass --allow-plaintext-store to permit it, or use "
            "QBO_TOKEN_KEY_SOURCE=keychain."
        )


def build_parser() -> argparse.ArgumentParser:
    # ValueFreeParser turns abbreviations off. argparse's default reads any
    # unambiguous prefix as the option: --enti would be --entities.
    parser = ValueFreeParser(
        prog="qbo-multicompany-mcp-auth",
        description="Consent, import, inspect and revoke QuickBooks credentials.",
    )
    add_common_arguments(parser)
    # Each command is a parser of its own. add_parser doesn't pass allow_abbrev
    # down, but it builds each one from ValueFreeParser, so revoke --al is
    # refused rather than read as revoke --all.
    sub = parser.add_subparsers(dest="command", required=True)

    connect = sub.add_parser("connect", help="run the consent flow for one entity")
    connect.add_argument("entity")
    connect.add_argument(
        "--manual",
        action="store_true",
        help="paste the callback URL instead of relying on the browser redirect",
    )
    connect.set_defaults(handler=cmd_connect)

    imported = sub.add_parser(
        "import", help=f"adopt a refresh token supplied in {ENV_IMPORT_REFRESH_TOKEN}"
    )
    imported.add_argument("entity")
    imported.set_defaults(handler=cmd_import)

    status = sub.add_parser("status", help="report every entity's credential state")
    status.set_defaults(handler=cmd_status)

    revoke = sub.add_parser("revoke", help="revoke at Intuit and remove locally")
    revoke.add_argument("entity", nargs="?")
    revoke.add_argument("--all", action="store_true", help="revoke every entity")
    revoke.set_defaults(handler=cmd_revoke)

    verify = sub.add_parser(
        "verify",
        help=(
            "submit one quarantined entity's stored refresh token exactly once; "
            "the only exit from refresh_uncertain other than connect"
        ),
    )
    verify.add_argument("entity")
    verify.set_defaults(handler=cmd_verify)

    init_key = sub.add_parser("init-key", help="create the token store key if absent")
    init_key.set_defaults(handler=cmd_init_key)

    export_key = sub.add_parser("export-key", help="print the store key (interactive only)")
    export_key.set_defaults(handler=cmd_export_key)

    return parser


def main(argv: list[str] | None = None) -> int:
    install_redacting_hooks()
    register_environment_secrets()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = settings_from_args(args)
        registry = settings.load_registry()
        return int(args.handler(settings, registry, args))
    except (StartupError, ConfigurationError) as exc:
        print(f"qbo-multicompany-mcp-auth: {redact(exc)}", file=sys.stderr)
        return 2
    except QboMcpError as exc:
        print(f"qbo-multicompany-mcp-auth: {redact(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # revoke --all can finish one company before the interrupt, and a
        # consent can commit before its last line prints: the message can't
        # say nothing changed.
        print(
            "\ninterrupted. What was reported above as done is done, and the step "
            "under way may have finished; run 'status' before trying again",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
