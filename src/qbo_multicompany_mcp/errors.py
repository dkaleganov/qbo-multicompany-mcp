"""The package's error taxonomy and its fixed error texts.

Two rules govern everything here.

*Fixed text only.* A model-visible error carries a status, a masked endpoint
label and a hint -- never an upstream response body, never a caller's argument
echoed back, never a full realm id. Upstream bodies are attacker-influenced and
frequently contain the very identifiers this package works to keep out of a
transcript; a caller's argument echoed into an error is how a prompt-injected
string gets a second chance at being read as instructions.

*Configuration is not consent.* A store row whose environment or client-id
fingerprint disagrees with the running process is a configuration error. It
must never set ``needs_consent``, because that would send the operator through
an Intuit consent screen to fix a typo in an environment variable -- and would
burn the working refresh token on the way.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final


class QboMcpError(Exception):
    """Base class for everything this package raises deliberately."""


class StartupError(QboMcpError):
    """A failure that must end the process with exit status 2.

    Reported as a single redacted line on stderr with no traceback. Raised for
    a malformed registry, a duplicate or non-numeric realm, a ``writes`` key
    that is not ``false``, a disallowed API base, and a loopback bind failure.
    A store or row from the wrong environment is a :class:`ConfigurationError`.
    """


class ConfigurationError(QboMcpError):
    """A mismatch between the store and the running configuration.

    Distinct from :class:`NeedsConsentError` on purpose. See the module
    docstring.
    """


class AppCredentialsRejected(ConfigurationError):
    """Intuit refused the app's own client id or secret (``invalid_client``).

    Configuration for every company at once, not one: a run that met it for
    one company would get the same answer for the next.
    """


class StoreError(QboMcpError):
    """The token store could not be read or written."""


class StoreLockTimeout(StoreError):
    """The canonical store lock could not be taken in time.

    Transient by definition: another process is mid-refresh and holds the lock,
    normally for one token POST and its writes, though nothing bounds a slow
    token response. Callers retry or exit 1. This is never reported as a
    credential failure and never sets ``needs_consent``.
    """


class PlaintextStoreRefused(StoreError):
    """``QBO_TOKEN_KEY_SOURCE=none`` without ``--allow-plaintext-store``."""

    def __init__(self) -> None:
        super().__init__(PLAINTEXT_REFUSED_TEXT)


class MalformedRowError(StoreError):
    """One realm's stored row could not be decoded by this build.

    Raised by every read of *that* realm; the other realms are unaffected.
    Its own class so a caller that must act differently on a row it cannot
    read -- ``auth revoke`` removes it locally without a request, since it
    can't be bound, and exits 1, since a token in it may still be live -- can
    tell it from a store that could not be read at all.
    """


class StoreLocationRefused(StartupError):
    """The store path is inside a git working copy or a known sync root."""


class NeedsConsentError(QboMcpError):
    """The realm has no usable credential; a human must re-consent."""


class RefreshUncertainError(QboMcpError):
    """A refresh may have been accepted but its response was lost.

    A refresh may have replaced the refresh token (Intuit's Authorization FAQ),
    so a lost response leaves the local copy possibly one generation behind the
    server's. Retrying automatically would resubmit a token that may already be
    spent, which Intuit answers with ``invalid_grant`` -- and *that* would look
    like a dead credential when the credential is in fact fine. So this stops
    and asks.
    """


class StaleConsentError(QboMcpError):
    """A consent ceremony began from a store state that no longer holds.

    The realm was revoked, or a newer consent landed, while the human was at
    the consent screen. Committing would overwrite the newer state with an
    older intent, so the ceremony stops without writing and the CLI revokes
    the pair it received. Exit 1: the operator re-runs ``connect`` if the
    newer state is not what they wanted.
    """


class NetworkPolicyViolation(QboMcpError):
    """A request was refused by the runtime network policy before it was sent.

    Raised by :mod:`qbo_multicompany_mcp.netpolicy`. Reaching this from normal
    use means a bug in this package, not bad input: the policy is the read-only
    guarantee, so it fails closed and loudly.
    """


class UpstreamError(QboMcpError):
    """QuickBooks answered, and the answer was a failure.

    Carries the HTTP status and, when the body named one this package
    recognises, the accounting fault code -- so a caller can ask "was that
    5030?" without parsing a sentence. The text is fixed; nothing from the
    response body is ever in it except fault codes: the one the fixed table
    has a hint for or, when it has none for any, the fault's codes as digits.
    ``fault_code`` stays a table code only.
    """

    def __init__(self, text: str, *, status: int, fault_code: str | None = None) -> None:
        super().__init__(text)
        self.status = status
        self.fault_code = fault_code


class DeadlineExceeded(QboMcpError):
    """The call's whole budget ran out before QuickBooks finished answering."""


class CallCancelled(QboMcpError):
    """The caller cancelled; no further business read was made."""


class ResponseTooLarge(QboMcpError):
    """A response body decoded to more than its cap. Nothing from it is used."""


class CapabilityUnavailable(QboMcpError):
    """A required QuickBooks feature is known to be off for this company.

    Raised before any request. Companies on lower QuickBooks plans genuinely
    lack class tracking, departments, inventory or multi-currency; answering
    with an empty result would read as "no data" when the truth is "not
    available on this plan".
    """


class GuardMismatch(QboMcpError):
    """The company name at Intuit disagrees with the registry.

    Per-entity: other entities keep working. Never echoes either name -- the
    operator reads both, with ``repr()``, from the CLI.
    """


class EntityUnknown(QboMcpError):
    """The ``entity`` argument names no entry in the registry."""


# --------------------------------------------------------------------------
# Fixed texts
# --------------------------------------------------------------------------

PLAINTEXT_REFUSED_TEXT: Final = (
    "Token store is unprotected (QBO_TOKEN_KEY_SOURCE=none). "
    "Re-start the server with --allow-plaintext-store to permit it, or set "
    "QBO_TOKEN_KEY_SOURCE=keychain."
)

LOCK_TIMEOUT_TEXT: Final = (
    "Token store is busy: another process holds the store lock. "
    "This is transient; retry. No credential was changed."
)

NO_APP_CREDENTIALS_TEXT: Final = (
    "No Intuit app credentials are configured, so no stored QuickBooks "
    "credential will be sent: without them this server cannot check which app "
    "a stored token was issued to, and cannot refresh one. Set QBO_CLIENT_ID "
    "and QBO_CLIENT_SECRET in the server's environment and restart it. "
    "list_entities and server_info work without them."
)

NO_APP_CREDENTIALS_STARTUP_TEXT: Final = (
    "warning: QBO_CLIENT_ID and QBO_CLIENT_SECRET are not set, so credential "
    "use is disabled. Listing tools and the request-free diagnostics work; "
    "every call that would send a stored QuickBooks credential will fail with "
    "a configuration error until both are set."
)

def deadline_text(budget: float) -> str:
    return (
        f"This call ran out of its {budget:.0f}-second budget before QuickBooks "
        f"finished answering, so no result is returned. Narrow the request -- a "
        f"shorter date window, a smaller limit -- and try again."
    )


def download_deadline_text(budget: float) -> str:
    return (
        f"This call ran out of its {budget:.0f}-second budget before the download "
        f"finished, so nothing is returned. The storage host may be slow; try again."
    )


CANCELLED_TEXT: Final = "This call was cancelled by the client; nothing further was read."


# --------------------------------------------------------------------------
# Argument refusals the SDK raises before any tool runs
# --------------------------------------------------------------------------

UNKNOWN_TOOL_TEXT: Final = (
    "Unknown tool: no tool of that name is registered on this server; tools/list "
    "names every one. The name given is not repeated."
)

#: pydantic error types whose message is built from the schema alone -- the
#: expected type, a bound, a length, a pattern, the enum -- and never carries
#: the value given (each checked against a planted value). A type outside this
#: set is described by :data:`_UNVETTED_REASON` instead, because its message
#: may quote the value (a validator's own ``value_error`` can).
_VETTED_MESSAGE_TYPES: Final = frozenset(
    {
        "missing", "string_type", "string_unicode", "string_too_short", "string_too_long",
        "string_pattern_mismatch", "int_type", "int_parsing", "int_parsing_size",
        "int_from_float", "float_type", "float_parsing", "finite_number", "bool_type",
        "bool_parsing", "list_type", "tuple_type", "set_type", "dict_type", "too_short",
        "too_long", "greater_than", "greater_than_equal", "less_than", "less_than_equal",
        "multiple_of", "literal_error", "enum", "none_required",
    }
)
_UNVETTED_REASON: Final = "is not a valid value"
#: A list of a thousand bad items is a thousand errors; the first few say it.
ARGUMENT_ERRORS_SHOWN: Final = 5


def argument_error_text(
    tool: str, errors: Sequence[Mapping[str, Any]], declared: Collection[str]
) -> str:
    """The SDK's argument refusal, rebuilt so it names the parameter and the
    reason and never the value.

    The SDK reports a failed argument validation with pydantic's text, which
    quotes ``input_value`` (mcp/server/mcpserver/tools/base.py): over real
    stdio a configured client secret sent as an argument would come back in
    the error. ``errors`` is ``ValidationError.errors(
    include_input=False)``. A location is named only through a parameter
    ``tool`` declares, and list positions as ``[n]``: a location can carry a
    dict key, which is the caller's own text. The reason is pydantic's message
    only for a type in :data:`_VETTED_MESSAGE_TYPES`.
    """
    parts: list[str] = []
    for error in list(errors)[:ARGUMENT_ERRORS_SHOWN]:
        location = tuple(error.get("loc") or ())
        head = location[0] if location else None
        if isinstance(head, str) and head in declared:
            where = head + "".join(
                f"[{part}]" for part in location[1:]
                if isinstance(part, int) and not isinstance(part, bool)
            )
        elif head is None:
            where = "the arguments"
        else:
            where = "an argument this tool does not take"
        reason = str(error.get("msg", "")) if error.get("type") in _VETTED_MESSAGE_TYPES else _UNVETTED_REASON
        parts.append(f"{where}: {reason}")
    more = len(errors) - ARGUMENT_ERRORS_SHOWN
    return (
        f"Error executing tool {tool}: the arguments did not validate -- "
        f"{'; '.join(parts)}{f'; and {more} more' if more > 0 else ''}. "
        f"The values given are not repeated."
    )


def too_large_text(cap_bytes: int, what: str) -> str:
    return (
        f"QuickBooks returned more than {cap_bytes // (1024 * 1024)} MiB for this "
        f"{what}, which is this server's limit, so nothing from it is returned. "
        f"Narrow the request -- a shorter date window, a smaller limit -- and try "
        f"again."
    )


REFRESH_UNCERTAIN_TEXT: Final = (
    "Token refresh outcome is unknown: the request may have been accepted but "
    "the response was lost or couldn't be used. Automatic retry is disabled for this realm. "
    "Run 'qbo-multicompany-mcp-auth status', then either "
    "'qbo-multicompany-mcp-auth verify <entity>' to submit the stored token "
    "once, or 'qbo-multicompany-mcp-auth connect <entity>' to re-consent."
)


def stale_consent_text(entity: str) -> str:
    return (
        f"The consent for entity '{entity}' began from a state the token store "
        f"no longer holds: the realm was revoked or a newer consent landed while "
        f"the consent screen was open. Nothing was stored. If the newer state is "
        f"not what you want, run 'qbo-multicompany-mcp-auth connect {entity}' again."
    )


def malformed_row_text(realm_id: str, reason: str) -> str:
    """``reason`` is one of this package's own fixed store-error texts."""
    return (
        f"The stored credential for realm ...{realm_id[-4:]} is malformed "
        f"({reason}). That entity is blocked until it is reconnected with "
        f"'qbo-multicompany-mcp-auth connect <entity>'; other realms are "
        f"unaffected."
    )


def bound_identity(entity: str, environment: str, *, intuit_tid: str | None = None) -> str:
    """The entity and environment a call was bound to, as an error names them,
    and Intuit's support id for the response when it carried one.

    Both names are the operator's own, from the registry, validated before any
    request, so naming them is safe; the realm id is never among them.
    ``intuit_tid`` is a correlation id Intuit asks integrators to log for its
    support team, passed here only in its documented shape
    (:func:`qbo_multicompany_mcp.client.intuit_tid_of`).
    """
    tid_part = f" intuit_tid={intuit_tid}" if intuit_tid else ""
    return f"entity={entity} environment={environment}{tid_part}"


def upstream_error_text(
    status: int,
    endpoint_label: str,
    hint: str,
    *,
    entity: str,
    environment: str,
    fault: str | None = None,
    intuit_tid: str | None = None,
) -> str:
    """Compose the only shape of upstream error a model ever sees.

    ``entity`` and ``environment`` say which company the failed call was bound
    to, so a model holding several companies in one session can tell which
    one failed. Keyword-only and required, so no caller can compose an
    upstream error that names none. ``endpoint_label`` is a masked label such
    as ``accounting/companyinfo`` -- a class of endpoint, never a URL and
    never a realm. ``fault`` is the code the fixed table in
    :mod:`qbo_multicompany_mcp.client` has a hint for or, for a fault it has
    no hint for, that fault's codes as digits
    (:func:`qbo_multicompany_mcp.client.named_fault_codes`): never a message,
    a detail or an element, which echo request content.
    """
    fault_part = f" fault={fault}" if fault else ""
    tid_part = f" intuit_tid={intuit_tid}" if intuit_tid else ""
    return (
        f"QuickBooks request failed: {bound_identity(entity, environment)} "
        f"status={status} endpoint={endpoint_label}{fault_part}{tid_part}. {hint}"
    )


def needs_consent_text(entity: str) -> str:
    """``entity`` is a registry key the operator chose, not caller-supplied data.

    It is validated against the registry before it reaches here, so naming it
    is safe and is the only way the operator knows which company to reconnect.
    """
    return (
        f"No usable QuickBooks credential for entity '{entity}'. "
        f"Run: qbo-multicompany-mcp-auth connect {entity}"
    )


def config_mismatch_text(entity: str, what: str) -> str:
    return (
        f"Stored credential for entity '{entity}' does not match this configuration "
        f"({what}). This is a configuration problem, not an expired credential: "
        f"no re-consent is needed. Check QBO_ENV and the client id in the environment."
    )


def guard_mismatch_text(entity: str) -> str:
    return (
        f"Company name at Intuit does not match the registry for entity '{entity}'. "
        f"Refusing to serve it. Run 'qbo-multicompany-mcp-auth status' to see both "
        f"values, then correct entities.yaml and restart this server, or reconnect the entity."
    )


def capability_missing_text(entity: str, feature: str) -> str:
    return (
        f"QuickBooks feature '{feature}' is not enabled for entity '{entity}', so this "
        f"tool cannot answer. Enable it in QuickBooks (Account and Settings), or use a "
        f"tool that does not require it. No request was made."
    )
