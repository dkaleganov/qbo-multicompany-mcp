"""The only module in this package that issues a non-GET request.

Three calls live here -- token exchange, refresh and revocation -- and every
one of them is a ``POST`` to an endpoint pinned in
:mod:`qbo_multicompany_mcp.constants`. Nothing here touches the Accounting API,
and nothing outside here may write a non-``GET`` method literal (an AST test
enforces that as regression protection).

The interesting part of this module is not the happy path. It is the taxonomy
of failures, because a refresh can replace the refresh token and retire the
submitted one (Intuit's Authorization FAQ; the rotation model, and the two
readings of when the old value dies, are written up at the top of
:mod:`qbo_multicompany_mcp.store`):

* A refresh whose response is lost leaves the server unable to tell whether the
  old token is still good or has just been spent. That is
  :class:`TokenResponseLost`, and it is never retried automatically.
* ``invalid_grant`` means the submitted token was rejected. Resubmitting it
  cannot help and may burn a race the caller has not noticed yet.
* ``invalid_client`` means the app credentials are wrong. It is a configuration
  error, and it must never be mistaken for "the user must re-consent" -- that
  would send an operator through a consent screen to fix an environment
  variable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.cookiejar import DefaultCookiePolicy
from typing import Any, Final, TypeGuard

import httpx

from .constants import (
    ACCESS_TOKEN_NOMINAL_SECONDS,
    DISCOVERY_TIMEOUT_SECONDS,
    DISCOVERY_URLS,
    HARD_EXPIRY_OPT_IN_HEADER,
    HARD_EXPIRY_OPT_IN_VALUE,
    OAUTH_POST_TIMEOUT_SECONDS,
    REFRESH_TOKEN_NOMINAL_DAYS,
    REVOKE_ENDPOINT,
    TOKEN_ENDPOINT,
    USER_AGENT,
)
from . import live_values
from .errors import NetworkPolicyViolation, QboMcpError, ResponseTooLarge
from .netpolicy import PolicyContext, RequestClass, enforce, isolated_client, send

_FORM: Final = "application/x-www-form-urlencoded"
_JSON: Final = "application/json"


class OAuthError(QboMcpError):
    """Base for failures of the three token calls.

    Raised as itself only when nothing was sent, which is what lets the store
    clear a refresh's intent on it.
    """


class RefusedBeforeSending(OAuthError, NetworkPolicyViolation):
    """The network policy refused the token request, so nothing was sent."""


class TokenResponseLost(OAuthError):
    """The request may have been accepted, but no response was received.

    The caller must treat the local refresh token as *possibly spent* and stop.
    See :class:`qbo_multicompany_mcp.errors.RefreshUncertainError`.
    """


class InvalidGrant(OAuthError):
    """Intuit rejected the submitted grant."""


class InvalidClient(OAuthError):
    """The app's client id or secret is wrong. Configuration, not consent."""


class OAuthHttpError(OAuthError):
    """A status from a token endpoint that is neither 200 nor a named error."""

    def __init__(self, status: int) -> None:
        self.status = status
        # No body, ever. An ASSUMPTION made in the safe direction: no token
        # endpoint error body has been observed, and the Accounting API's
        # faults do echo request values (``Invalid Enumeration : <value>`` was
        # observed live, see reports.py), so a body here is treated as able to.
        super().__init__(f"token endpoint returned status {status}")

    @property
    def grant_outcome_known(self) -> bool:
        """Whether the submitted grant is known to have survived.

        ASSUMPTION, not an observed Intuit behaviour: a 4xx -- including a 429
        -- is a request that was received and refused without the grant being
        evaluated or consumed, so the stored refresh token is still the live
        one. A 5xx, or any other status, is a response that says nothing about
        what happened to the grant, and is treated as ambiguous. Intuit's
        documentation does not settle this: its Authorization FAQ describes
        only ``invalid_grant`` on a second submission of one token, and its
        API call limits and throttles page gives 429 guidance for the
        Accounting API, not for the token endpoint. Revisit if a live token
        endpoint is ever observed to answer 4xx and reject the same token
        afterwards.
        """
        return 400 <= self.status <= 499


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """A token pair with its expiries resolved to absolute timestamps.

    Intuit returns relative lifetimes (``expires_in``,
    ``x_refresh_token_expires_in``). Storing those as-is would make every
    freshness decision depend on when the row was last read rather than when
    the token was issued, which is how a process that sleeps through an expiry
    convinces itself the credential is fine.

    ``refresh_token_expires_at`` is the refresh token's INACTIVITY clock: 100
    days (Intuit's Authorization FAQ), restarted when Intuit issues a new
    refresh-token value, at most once per 24 hours (observed 2026-09-26). It
    is not the five-year hard limit. That is ``refresh_token_hard_expires_at``,
    filled only when the response carried the opt-in
    ``x_refresh_token_hard_expires_in``, and otherwise ``None``: unknown, never
    invented.
    """

    access_token: str
    refresh_token: str
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime
    realm_id: str | None = None
    refresh_token_hard_expires_at: datetime | None = None
    #: Set only by test fixtures. A synthetic pair is kept out of the
    #: live-value registry, which is what lets the suite's loopback mock
    #: receive it. Nothing in this package sets it: every pair Intuit issues
    #: is live, and a test fails if a source file ever writes ``True`` here.
    synthetic: bool = False

    def __post_init__(self) -> None:
        if not self.synthetic:
            live_values.register(self.access_token, self.refresh_token)

    def access_token_digest(self) -> str:
        return token_digest(self.access_token)

    def refresh_token_digest(self) -> str:
        return token_digest(self.refresh_token)


def token_digest(token: str) -> str:
    """A full SHA-256 of a token, for binding and comparison.

    A digest can be written to a journal and compared later without the journal
    ever holding a usable credential in that field.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def client_id_fingerprint(client_id: str) -> str:
    """The full SHA-256 digest of the client id.

    Full, not truncated: this is the value that decides whether a stored row
    belongs to the app that is running, and a truncated digest invites a
    collision to be read as a match.
    """
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()


def _basic_auth(client_id: str, client_secret: str) -> tuple[str, str]:
    return (client_id, client_secret)


def token_client() -> httpx.Client:
    """A client for token calls: no proxy, CA file or key log from the environment.

    The client secret rides on every one of these, and a grant on two. Building
    the TLS context opens files, the CA bundle and any key log the environment
    names, so a failure here is raised as the base :class:`OAuthError`: nothing
    was sent.

    It keeps no cookie. A token request may carry none, and a client used for
    several calls, as ``revoke --all`` does, would otherwise send back what one
    response set, and the policy would refuse every request after it.
    """
    try:
        client = isolated_client(timeout=OAUTH_POST_TIMEOUT_SECONDS)
    except OSError as exc:
        raise OAuthError("could not build the HTTP client for the Intuit token endpoint") from exc
    client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=()))
    return client


def _post(
    form: dict[str, str],
    *,
    url: str,
    client_id: str,
    client_secret: str,
    request_class: RequestClass,
    environment: str,
    from_cli: bool,
    client: httpx.Client | None = None,
    json_body: dict[str, Any] | None = None,
    opt_in_hard_expiry: bool = False,
) -> httpx.Response:
    owns_client = client is None
    http = client or token_client()
    try:
        auth = httpx.BasicAuth(*_basic_auth(client_id, client_secret))
        headers = {
            "Accept": _JSON,
            "User-Agent": USER_AGENT,
            "Content-Type": _JSON if json_body is not None else _FORM,
        }
        if opt_in_hard_expiry:
            # The two calls that issue a token ask for the five-year clock,
            # in the header form the samples on Intuit's "Set up OAuth 2.0"
            # page show (see constants). A token endpoint that refused the
            # header would answer 4xx, which the store treats as a refusal the
            # grant survived: loud, and nothing rotates.
            headers[HARD_EXPIRY_OPT_IN_HEADER] = HARD_EXPIRY_OPT_IN_VALUE
        if json_body is not None:
            request = http.build_request("POST", url, json=json_body, headers=headers)
        else:
            request = http.build_request("POST", url, data=form, headers=headers)
        request = next(auth.auth_flow(request))
        ctx = PolicyContext(
            request_class=request_class,
            environment=environment,
            from_cli=from_cli,
        )
        # Checked here first, where a refusal is known to mean nothing left this
        # machine; send checks again, and anything it raises later keeps its type.
        try:
            enforce(request, ctx)
        except NetworkPolicyViolation as exc:
            raise RefusedBeforeSending(str(exc)) from exc
        try:
            return send(http, request, ctx, what="token response")
        except ResponseTooLarge as exc:
            # A response arrived, so the request was delivered and may have
            # been accepted; only its body could not be kept. That is the
            # ambiguous outcome, not a clean failure to retry.
            raise TokenResponseLost(
                "the token response was too large to read; the request may have been accepted"
            ) from exc
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The connection was never established, so nothing was submitted
            # and the local token is certainly still whatever it was.
            raise OAuthError("could not reach the Intuit token endpoint") from exc
        except httpx.HTTPError as exc:
            # Everything else -- read timeout, write timeout, reset mid-stream
            # -- leaves the outcome genuinely unknown.
            raise TokenResponseLost(
                "the token request may have been accepted but no response arrived"
            ) from exc
    finally:
        if owns_client:
            http.close()


#: A token this package can store and send: printable ASCII, no space at
#: either end. A blank one, or one with a control character, is no token.
_USABLE_TOKEN: Final = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?")


def _usable(token: object) -> TypeGuard[str]:
    return isinstance(token, str) and _USABLE_TOKEN.fullmatch(token) is not None


def _parse_token_response(response: httpx.Response) -> TokenResponse:
    received_at = datetime.now(UTC)
    try:
        payload = response.json()
    except ValueError as exc:
        # A 200 we cannot parse is indistinguishable from an accepted refresh
        # whose new token we failed to capture. Treat it as lost, not as an
        # error to retry.
        raise TokenResponseLost("token response was not valid JSON") from exc

    if not isinstance(payload, dict):
        raise TokenResponseLost("token response was not a JSON object")

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    # A token that can't be sent can't replace the stored pair, and the request
    # may still have been accepted, which spends the old one: lost, like a body
    # that isn't JSON.
    if not _usable(access_token) or not _usable(refresh_token):
        raise TokenResponseLost("token response did not carry two usable tokens")

    access_ttl = _int_or(payload.get("expires_in"), ACCESS_TOKEN_NOMINAL_SECONDS)
    refresh_ttl = _int_or(
        payload.get("x_refresh_token_expires_in"),
        REFRESH_TOKEN_NOMINAL_DAYS * 86400,
    )
    # Opt-in: the request asks for it, and the field has been OBSERVED live, on
    # every refresh on both sandbox companies on 2026-09-24. Read when present;
    # when absent the hard clock stays unknown, never derived.
    hard_ttl = _int_or(payload.get("x_refresh_token_hard_expires_in"), 0)
    realm_id = payload.get("realmId")

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=received_at + timedelta(seconds=access_ttl),
        refresh_token_expires_at=received_at + timedelta(seconds=refresh_ttl),
        realm_id=str(realm_id) if isinstance(realm_id, (str, int)) else None,
        refresh_token_hard_expires_at=(
            received_at + timedelta(seconds=hard_ttl) if hard_ttl > 0 else None
        ),
    )


def _int_or(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _raise_for_token_status(response: httpx.Response) -> None:
    if response.status_code == 200:
        return
    error_code = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            error_code = str(body.get("error", ""))
    except ValueError:
        error_code = ""

    if error_code == "invalid_client" or response.status_code == 401:
        raise InvalidClient(
            "Intuit rejected the app credentials (invalid_client). "
            "Check the client id and secret in the environment. "
            "No re-consent is required."
        )
    if error_code == "invalid_grant":
        raise InvalidGrant("Intuit rejected the submitted grant (invalid_grant)")
    raise OAuthHttpError(response.status_code)


# --------------------------------------------------------------------------
# The three calls
# --------------------------------------------------------------------------


def exchange_code(
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    environment: str,
    code_verifier: str | None = None,
    client: httpx.Client | None = None,
) -> TokenResponse:
    """Exchange an authorization code. CLI only, and at most once per code."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if code_verifier is not None:
        form["code_verifier"] = code_verifier
    response = _post(
        form,
        url=TOKEN_ENDPOINT,
        client_id=client_id,
        client_secret=client_secret,
        request_class=RequestClass.TOKEN_EXCHANGE,
        environment=environment,
        from_cli=True,
        client=client,
        opt_in_hard_expiry=True,
    )
    _raise_for_token_status(response)
    return _parse_token_response(response)


def refresh_tokens(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    environment: str,
    from_cli: bool = False,
    client: httpx.Client | None = None,
) -> TokenResponse:
    """Exchange a refresh token for a new pair.

    The response may carry a *new* refresh token, and when it does the
    submitted one is to be treated as expired (Intuit's Authorization FAQ; the
    value changes every 24 hours or on the first refresh after; how long the
    old value keeps working has two readings, see
    :mod:`qbo_multicompany_mcp.store`). That is why this package has a
    write-ahead journal, a canonical lock and a refusal to retry an uncertain
    outcome.
    """
    response = _post(
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        url=TOKEN_ENDPOINT,
        client_id=client_id,
        client_secret=client_secret,
        request_class=RequestClass.TOKEN_REFRESH,
        environment=environment,
        from_cli=from_cli,
        client=client,
        opt_in_hard_expiry=True,
    )
    _raise_for_token_status(response)
    return _parse_token_response(response)


def revoke_token(
    *,
    token: str,
    client_id: str,
    client_secret: str,
    environment: str,
    client: httpx.Client | None = None,
) -> None:
    """Revoke a refresh token. CLI only."""
    response = _post(
        {},
        url=REVOKE_ENDPOINT,
        client_id=client_id,
        client_secret=client_secret,
        request_class=RequestClass.TOKEN_REVOKE,
        environment=environment,
        from_cli=True,
        client=client,
        json_body={"token": token},
    )
    if response.status_code not in (200, 204):
        raise OAuthHttpError(response.status_code)


# --------------------------------------------------------------------------
# Discovery (GET)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveryCheck:
    """The result of confirming the pinned endpoints against Intuit."""

    reached: bool
    matches: bool
    differences: tuple[str, ...]


def confirm_pinned_endpoints(
    environment: str,
    *,
    client: httpx.Client | None = None,
) -> DiscoveryCheck:
    """Confirm the pinned endpoints, and only confirm them.

    Discovery may report that a pinned value has drifted. It may never *supply*
    one: a discovery document is fetched over the network, and this package
    sends refresh tokens to the endpoints it names. Reading a destination for
    secret-bearing traffic out of a fetched document would mean a single
    spoofed response could redirect every credential this server holds. So the
    pinned values win, always, and a mismatch is reported for a human to act on.
    """
    url = DISCOVERY_URLS.get(environment)
    if url is None:
        return DiscoveryCheck(reached=False, matches=False, differences=("unknown environment",))

    owns_client = client is None
    http = client or isolated_client(timeout=DISCOVERY_TIMEOUT_SECONDS)
    try:
        request = http.build_request(
            "GET", url, headers={"Accept": _JSON, "User-Agent": USER_AGENT}
        )
        ctx = PolicyContext(request_class=RequestClass.DISCOVERY, environment=environment)
        try:
            response = send(http, request, ctx)
        except httpx.HTTPError:
            return DiscoveryCheck(reached=False, matches=False, differences=())
        if response.status_code != 200:
            return DiscoveryCheck(reached=False, matches=False, differences=())
        try:
            document = response.json()
        except ValueError:
            return DiscoveryCheck(reached=False, matches=False, differences=())
    finally:
        if owns_client:
            http.close()

    if not isinstance(document, dict):
        return DiscoveryCheck(reached=True, matches=False, differences=("malformed document",))

    differences = [
        name
        for name, pinned in (
            ("token_endpoint", TOKEN_ENDPOINT),
            ("revocation_endpoint", REVOKE_ENDPOINT),
        )
        if document.get(name) != pinned
    ]
    return DiscoveryCheck(
        reached=True,
        matches=not differences,
        differences=tuple(differences),
    )
