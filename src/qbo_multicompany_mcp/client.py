"""The Accounting API client. Every request here is a ``GET``.

There is no method parameter anywhere in this module and no way for a caller to
supply one. Each call site writes the literal ``"GET"``, and every request is
then handed to :func:`qbo_multicompany_mcp.netpolicy.send`, which re-checks the
method, the origin, the path, the realm and the query immediately before the
bytes leave.

The other thing this module owns is the 401. A 401 from Intuit is not proof
that the access token expired: Intuit's Authorization FAQ lists expiry, a user
revoking the connection and the two refresh-token limits as reasons a token
stops working, and the 401 itself does not say which. So the response is
handled by generation rather than by timestamp, exactly once, in
:meth:`qbo_multicompany_mcp.store.TokenStore.on_unauthorized`.

And the limits. Every request asks for ``Accept-Encoding: identity`` and has
its body capped after decoding -- 32 MiB, 16 MiB for a report. Inside a tool
call each request's timeout is what is left of the call's budget
(:mod:`qbo_multicompany_mcp.deadline`). A 5xx on a read is retried three times
with full-jitter backoff; a 504 or a read timeout on a report is not, and
neither is a 429.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from . import deadline as call_deadline
from .config import Settings
from .constants import (
    ACCOUNTING_TIMEOUT_SECONDS,
    MAX_JSON_RESPONSE_BYTES,
    MAX_REPORT_RESPONSE_BYTES,
    MINOR_VERSION,
    RETRY_ATTEMPTS,
    RETRY_BASE_SECONDS,
    RETRY_FACTOR,
)
from .errors import (
    DeadlineExceeded,
    NeedsConsentError,
    RefreshUncertainError,
    QboMcpError,
    UpstreamError,
    bound_identity,
    deadline_text,
    needs_consent_text,
    upstream_error_text,
)
from .limits import DEFAULT_ACQUIRE_TIMEOUT_SECONDS, THROTTLED_TEXT, RateLimiter, UpstreamThrottled
from .netpolicy import PolicyContext, RequestClass, build_accounting_url, isolated_client, send
from .oauth import USER_AGENT
from .registry import Entity
from .store import RefreshActor, TokenRow, TokenStore

#: Masked endpoint labels. A model-visible error names the class of endpoint,
#: never a URL and never a realm.
ENDPOINT_LABELS: Final[dict[str, str]] = {
    "companyinfo/{realm}": "accounting/companyinfo",
    "preferences": "accounting/preferences",
    "query": "accounting/query",
    "cdc": "accounting/changes",
}


def endpoint_label(tail: str) -> str:
    """The masked label for ``tail``. Every report, whatever its name, is
    ``accounting/reports``: the class of endpoint, never the report or a URL."""
    if tail.startswith(REPORT_PREFIX):
        return "accounting/reports"
    return ENDPOINT_LABELS.get(tail, "accounting")

#: Every report path. A report can be expensive upstream; re-issuing one that
#: already timed out, or that the gateway gave up on with a 504, adds load to a
#: company that is by definition struggling to answer, and the second attempt
#: is no more likely to finish inside the budget than the first.
REPORT_PREFIX: Final = "reports/"

#: Indirection for the tests, which record backoff instead of sleeping it.
_sleep: Callable[[float], None] = time.sleep
_jitter: Callable[[float, float], float] = random.uniform


def _is_report(tail: str) -> bool:
    return tail.startswith(REPORT_PREFIX)


def _retryable(status: int, tail: str) -> bool:
    """5xx on a read, except a 504 on a report."""
    if not 500 <= status <= 599:
        return False
    return not (status == 504 and _is_report(tail))


class _ReadTimedOut(Exception):
    """Internal: a read timeout the retry loop decides about."""

#: Accounting fault code -> a fixed hint.
#:
#: Eight codes with meanings from Intuit's own error-code page ("Review
#: QuickBooks Online Accounting API error codes"), plus 2170 and 2500, which
#: this package's own report filters were observed to produce against live
#: sandbox (docs/api-findings.md), and 5020 and 10000, each observed live where
#: its comment says. A fault with no code in this table is named by its codes
#: instead (:func:`named_fault_codes`).
#: The body is read for ``Fault.Error[].code`` and nothing else. ``Message``,
#: ``Detail`` and ``element`` are never quoted: they echo request content.
FAULT_HINTS: Final[dict[str, str]] = {
    "120": (
        "QuickBooks refused authorization for this company. Intuit documents "
        "this for an accountant user removed from the company while it was "
        "connected; reconnect the entity."
    ),
    "140": "QuickBooks reports this company is locked out.",
    "150": "QuickBooks reports this company is under maintenance. Retry later.",
    "3100": (
        "QuickBooks says the connection lacks the scope this request needs. "
        "Reconnect the entity."
    ),
    "3200": (
        "QuickBooks rejected the access token as invalid or expired. If it "
        "persists, run 'qbo-multicompany-mcp-auth status'."
    ),
    # OBSERVED in sandbox 2026-09-24: TaxSummary answered HTTP 400 with fault
    # 5020, "Permission Denied Error: To access this, sign in again or contact
    # an administrator." A report the company or connection cannot run is a
    # fixed, actionable error, never an empty report. Distinct from 5030 (the
    # company's QuickBooks plan lacks the feature) and 6190 (the subscription).
    "5020": (
        "QuickBooks refused permission for this request: the connected user or "
        "this connection cannot run it for this company; sign in to QuickBooks "
        "again or ask an administrator for access. This is a refusal, not an "
        "empty report."
    ),
    "5030": (
        "QuickBooks reports that the requested feature is not supported for "
        "this company, which can mean its plan does not include it."
    ),
    "6190": (
        "QuickBooks reports that this company's subscription has ended, has a "
        "billing problem, or was cancelled."
    ),
    # OBSERVED in production 2026-09-28: TransactionListByCustomer with any
    # `columns` answered HTTP 200 with SystemFault 10000, "An application error
    # has occurred" (a NullPointerException on Intuit's side), and the same
    # request failed again each time it was repeated. Intuit's error-code page
    # lists 10000 under "Important, non-recoverable error codes" and calls a
    # SystemFault a problem with its servers that the caller cannot fix.
    "10000": (
        "QuickBooks reported an internal error on its own side (fault 10000). "
        "Intuit lists it as non-recoverable, so retrying the same request "
        "unchanged is unlikely to help."
    ),
    "10100": "QuickBooks reports the result set is too large. Narrow the request.",
    "2170": "QuickBooks refused a filter value that is not in its enumeration.",
    "2500": (
        "QuickBooks refused a reference id as malformed: an id filter such as "
        "vendor or customer takes a QuickBooks id."
    ),
}


#: Intuit's support correlation id, a documented REST response header that
#: Intuit asks integrators to log (its SDK examples read ``e.intuit_tid``).
#: OBSERVED 2026-09-26 on a live error (fault 5020, sandbox) as ``1-``, 8 hex,
#: 24 hex -- not the documented ``gw-`` plus a UUID; the shape below admits
#: both.
INTUIT_TID_HEADER: Final = "intuit_tid"

#: Wide enough for the documented shape, ``gw-`` and a UUID, and the one
#: observed: up to 80 letters, digits and hyphens. The header is upstream text
#: and an error here is fixed text, so a value outside this shape is dropped,
#: never quoted.
_INTUIT_TID_SHAPE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,79}")


def intuit_tid_of(response: httpx.Response) -> str | None:
    """The response's ``intuit_tid``, or ``None`` if absent or not in shape."""
    value = (response.headers.get(INTUIT_TID_HEADER) or "").strip()
    return value if _INTUIT_TID_SHAPE.fullmatch(value) else None


def fault_codes(body: Any) -> list[str]:
    """Every fault code in ``body``, in order, normalised to plain digits.

    Two spellings exist: accounting faults are ``Fault.Error[].code`` and
    authentication faults arrive lower-case, ``fault.error[].code``. Nothing
    else is read from the body.
    """
    if not isinstance(body, Mapping):
        return []
    fault = body.get("Fault", body.get("fault"))
    if not isinstance(fault, Mapping):
        return []
    errors = fault.get("Error", fault.get("error"))
    if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes)):
        return []
    codes: list[str] = []
    for entry in errors:
        if not isinstance(entry, Mapping):
            continue
        code = entry.get("code")
        if isinstance(code, int) and not isinstance(code, bool):
            code = str(code)
        if isinstance(code, str) and code.strip():
            text = code.strip()
            # ASCII and short: int() refuses a superscript digit and a run of
            # thousands of digits, and this is an upstream value.
            numeric = text.isascii() and text.isdigit() and len(text) < 20
            codes.append(str(int(text)) if numeric else text)
    return codes


#: The codes an error may name for a fault the table has no hint for: a run of
#: one to seven ASCII digits, as :func:`fault_codes` yields it. Intuit's
#: documented codes run three to five digits (its error-code page), and a run
#: of eight or more is what :mod:`qbo_multicompany_mcp.redaction` treats as a
#: possible identifier, so it is never named. A code that is not digits is
#: upstream text and is never named either.
_NAMEABLE_FAULT_CODE: Final = re.compile(r"[0-9]{1,7}")

#: A body is capped in megabytes, not in codes: an error names at most this
#: many of a fault's codes and counts the rest.
FAULT_CODES_NAMED: Final = 5


def named_fault_codes(codes: Sequence[str]) -> str | None:
    """How an error names a fault the table has no hint for: its nameable
    codes, each once, in order, comma-separated, at most
    :data:`FAULT_CODES_NAMED` of them with the rest counted; ``None`` when it
    has none. Only a code is ever named: ``Message``, ``Detail`` and
    ``element`` are never read (:func:`fault_codes`), because they echo
    request content."""
    nameable = list(dict.fromkeys(code for code in codes if _NAMEABLE_FAULT_CODE.fullmatch(code)))
    if not nameable:
        return None
    shown = ",".join(nameable[:FAULT_CODES_NAMED])
    more = len(nameable) - FAULT_CODES_NAMED
    return f"{shown} and {more} more" if more > 0 else shown


@dataclass(frozen=True, slots=True)
class ApiResult:
    payload: dict[str, Any]
    request_started_at: datetime
    fetched_at: datetime


class AccountingClient:
    def __init__(
        self,
        settings: Settings,
        store: TokenStore,
        *,
        http: httpx.Client | None = None,
        actor_role: str = "server",
        limiter: RateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.actor_role = actor_role
        self._http = http
        self._owns_http = http is None
        self.limiter = limiter or RateLimiter(
            settings.token_store_dir, environment=settings.environment
        )

    def _client(self) -> httpx.Client:
        # Built by the network policy, which reads no proxy, CA file or key
        # log from the environment.
        if self._http is None:
            self._http = isolated_client(timeout=ACCOUNTING_TIMEOUT_SECONDS)
        return self._http

    def close(self) -> None:
        if self._http is not None and self._owns_http:
            self._http.close()
            self._http = None

    def __enter__(self) -> AccountingClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- credentials -------------------------------------------------------

    def _usable_row(self, entity: Entity) -> TokenRow:
        # `usable_row`, not `get_row`: the configuration binding is checked
        # before the token is sent, not only when a refresh happens to be due.
        row = self.store.usable_row(entity.realm_id, entity_key=entity.key)
        if row is None:
            raise NeedsConsentError(needs_consent_text(entity.key))
        if row.refresh_uncertain:
            raise RefreshUncertainError(
                row.recovery_hint or "the last refresh outcome for this realm is unknown"
            )
        if row.needs_consent:
            raise NeedsConsentError(needs_consent_text(entity.key))
        if not row.access_token_valid():
            # The store checks the call's budget under its lock, after the
            # wait for it, before anything is submitted.
            row = self.store.refresh(
                entity.realm_id,
                actor=RefreshActor.here(self.actor_role),
                force=True,
                entity_key=entity.key,
            )
        return row

    # -- requests ----------------------------------------------------------

    def get(
        self,
        entity: Entity,
        tail: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> ApiResult:
        """One allowlisted read: 5xx retried, and at most one 401-driven retry."""
        row = self._usable_row(entity)
        started = datetime.now(UTC)

        response = self._send_retrying(entity, tail, params, row.access_token)
        if response.status_code == 401:
            # Identify which generation was rejected and let the store decide:
            # use a newer one if it exists, otherwise force exactly one refresh.
            refreshed = self.store.on_unauthorized(
                entity.realm_id,
                rejected_generation=row.auth_generation,
                actor=RefreshActor.here(self.actor_role),
                entity_key=entity.key,
            )
            response = self._send_retrying(entity, tail, params, refreshed.access_token)

        return ApiResult(
            payload=self._payload(response, tail, entity),
            request_started_at=started,
            fetched_at=datetime.now(UTC),
        )

    def _send_retrying(
        self,
        entity: Entity,
        tail: str,
        params: Mapping[str, str] | None,
        access_token: str,
    ) -> httpx.Response:
        """Send, retrying a transient failure of an idempotent read.

        A 5xx is retried on its own backoff: three retries, base one second,
        factor two, full jitter. A read timeout is retried the same way off a
        report and never on one; a 504 on a report is not retried either. A
        retry the call's budget cannot fit is not attempted -- the last answer
        stands.
        """
        attempt = 0
        while True:
            try:
                response = self._send(entity, tail, params, access_token)
            except _ReadTimedOut:
                if _is_report(tail) or not self._may_retry(attempt):
                    raise QboMcpError(self._timeout_text(entity, tail)) from None
            else:
                if not _retryable(response.status_code, tail) or not self._may_retry(attempt):
                    return response
            self._back_off(attempt)
            attempt += 1

    @staticmethod
    def _backoff_ceiling(attempt: int) -> float:
        return RETRY_BASE_SECONDS * RETRY_FACTOR**attempt

    def _may_retry(self, attempt: int) -> bool:
        if attempt >= RETRY_ATTEMPTS:
            return False
        deadline = call_deadline.current()
        # The worst-case sleep plus a second for the attempt itself.
        return deadline is None or deadline.usable() > self._backoff_ceiling(attempt) + 1.0

    def _back_off(self, attempt: int) -> None:
        deadline = call_deadline.current()
        if deadline is not None:
            deadline.checkpoint()
        _sleep(_jitter(0.0, self._backoff_ceiling(attempt)))

    @staticmethod
    def _timeout_text(entity: Entity, tail: str) -> str:
        hint = "QuickBooks did not answer in time."
        if _is_report(tail):
            hint += (
                " This is a report path, which is not retried automatically: "
                "re-issuing a report that already timed out adds load without "
                "improving the odds. Narrow the date window and try again."
            )
        return upstream_error_text(
            0, endpoint_label(tail), hint, entity=entity.key, environment=entity.environment
        )

    def _send(
        self,
        entity: Entity,
        tail: str,
        params: Mapping[str, str] | None,
        access_token: str,
    ) -> httpx.Response:
        try:
            http = self._client()
        except OSError as exc:
            # Building the TLS context opens files, the CA bundle and any key
            # log the environment names among them. Nothing was sent.
            raise QboMcpError(
                upstream_error_text(
                    0,
                    endpoint_label(tail),
                    "The HTTP client for QuickBooks could not be built, so nothing was "
                    "sent. Check SSLKEYLOGFILE and the CA bundle.",
                    entity=entity.key,
                    environment=entity.environment,
                )
            ) from exc
        url = build_accounting_url(self.settings.api_base, entity.realm_id, tail)
        query = {"minorversion": MINOR_VERSION}
        if params:
            query.update(params)

        deadline = call_deadline.current()
        # Both documented windows, shared across processes, before the send --
        # and never waited for past the call's budget. Filed under the entity
        # key, never the realm id: the shared file is plaintext.
        try:
            self.limiter.acquire(
                entity.key, timeout=call_deadline.bounded(DEFAULT_ACQUIRE_TIMEOUT_SECONDS)
            )
        except UpstreamThrottled:
            # A 429 recorded by any process refuses here, before a request.
            # The same throttle, so it names its company the same way.
            raise UpstreamThrottled(
                f"{THROTTLED_TEXT} ({bound_identity(entity.key, entity.environment)})"
            ) from None
        timeout = call_deadline.bounded(ACCOUNTING_TIMEOUT_SECONDS)

        request = http.build_request(
            "GET",
            url,
            params=query,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                # So the decoded-size cap measures what was sent. Whether
                # Intuit honours it is unknown; the cap counts decoded bytes
                # either way.
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
        )
        ctx = PolicyContext(
            request_class=RequestClass.ACCOUNTING,
            environment=self.settings.environment,
            api_base=self.settings.api_base,
            realm_id=entity.realm_id,
            allow_documents=self.settings.allow_documents,
            allow_custom_api_base=self.settings.allow_custom_api_base,
        )

        report = _is_report(tail)
        try:
            response = send(
                http, request, ctx,
                max_bytes=MAX_REPORT_RESPONSE_BYTES if report else MAX_JSON_RESPONSE_BYTES,
                what="report" if report else "response",
                deadline=deadline,
            )
        except httpx.TimeoutException as exc:
            if deadline is not None and deadline.usable() <= 0.5:
                # The timeout was the budget's, not the request's.
                raise DeadlineExceeded(deadline_text(deadline.budget)) from exc
            if isinstance(exc, httpx.ReadTimeout):
                raise _ReadTimedOut() from exc
            raise QboMcpError(
                upstream_error_text(
                    0,
                    endpoint_label(tail),
                    "The request did not reach QuickBooks. This is usually transient.",
                    entity=entity.key,
                    environment=entity.environment,
                )
            ) from exc
        except httpx.HTTPError as exc:
            raise QboMcpError(
                upstream_error_text(
                    0,
                    endpoint_label(tail),
                    "The request did not reach QuickBooks. This is usually transient.",
                    entity=entity.key,
                    environment=entity.environment,
                )
            ) from exc

        if response.status_code == 429:
            # Record the block so every process fails fast rather than each
            # spending a request to rediscover it. No invented reset time: a
            # 429 can also mean the monthly quota is gone, and waiting does not
            # replenish that.
            self.limiter.note_throttled(
                entity.key, response.headers.get("Retry-After")
            )
            raise UpstreamThrottled(
                f"{THROTTLED_TEXT} ("
                f"{bound_identity(entity.key, entity.environment, intuit_tid=intuit_tid_of(response))})"
            )

        return response

    def _payload(self, response: httpx.Response, tail: str, entity: Entity) -> dict[str, Any]:
        label = endpoint_label(tail)
        status = response.status_code
        # Every response this reports as a failure names its company and, when
        # Intuit sent one, its support id.
        bound = {
            "entity": entity.key,
            "environment": entity.environment,
            "intuit_tid": intuit_tid_of(response),
        }
        try:
            body: Any = response.json()
        except ValueError:
            body = None

        # A fault fails the call whatever the status. A 200 can carry one
        # (Intuit's error-code page), and parsed as a payload it has no
        # QueryResponse -- which a walk reads as zero rows.
        codes = fault_codes(body)
        if status != 200 or codes:
            known = next((code for code in codes if code in FAULT_HINTS), None)
            if known is not None:
                hint = FAULT_HINTS[known]
            elif status == 200:
                hint = "QuickBooks returned a fault inside a successful response."
            else:
                hint = _hint_for(status)
            # A fault the table has no hint for is named by its codes, so one
            # never seen before is still diagnosable. The caller-facing
            # fault_code stays a table code only.
            named = known if known is not None else named_fault_codes(codes)
            if known is None and named is not None:
                hint += " This server has no hint for this fault."
            raise UpstreamError(
                upstream_error_text(status, label, hint, fault=named, **bound),
                status=status,
                fault_code=known,
            )

        if body is None:
            raise UpstreamError(
                upstream_error_text(200, label, "The response was not valid JSON.", **bound),
                status=200,
            )
        if not isinstance(body, dict):
            raise UpstreamError(
                upstream_error_text(200, label, "The response was not a JSON object.", **bound),
                status=200,
            )
        return body

    # -- resources ---------------------------------------------------------

    def company_info(self, entity: Entity) -> ApiResult:
        return self.get(entity, "companyinfo/{realm}")

    def company_info_with_token(self, entity: Entity, access_token: str) -> ApiResult:
        """One CompanyInfo read using a caller-supplied token.

        Used only by the auth CLI, at consent, and it bypasses the store on
        purpose: the guard has to run **before** the credential is written. If
        it ran afterwards, a wrong-company approval would already be on disk
        and would only surface at first use.

        No 401 handling here, and none wanted: the token is seconds old, and
        there is no stored generation to refresh from yet.
        """
        started = datetime.now(UTC)
        response = self._send_retrying(entity, "companyinfo/{realm}", None, access_token)
        return ApiResult(
            payload=self._payload(response, "companyinfo/{realm}", entity),
            request_started_at=started,
            fetched_at=datetime.now(UTC),
        )

    def preferences(self, entity: Entity) -> ApiResult:
        return self.get(entity, "preferences")

    def query(self, entity: Entity, statement: str) -> ApiResult:
        # The canonical statement goes in as-is; httpx encodes it exactly once.
        return self.get(entity, "query", params={"query": statement})

    def report(
        self, entity: Entity, report_name: str, params: Mapping[str, str]
    ) -> ApiResult:
        return self.get(entity, f"reports/{report_name}", params=params)

    def changes(
        self, entity: Entity, *, entities: Sequence[str], changed_since: str
    ) -> ApiResult:
        return self.get(
            entity,
            "cdc",
            params={"entities": ",".join(entities), "changedSince": changed_since},
        )


def _hint_for(status: int) -> str:
    if status == 403:
        return (
            "QuickBooks refused the request. The connected user may lack access to "
            "this company, or the app's scopes may have changed."
        )
    if status == 429:
        return "QuickBooks is rate-limiting this app. Retry in a minute."
    if status == 400:
        return "QuickBooks rejected the request as malformed."
    if 500 <= status < 600:
        return "QuickBooks had a server-side failure. This is usually transient."
    return "See the QuickBooks status page if this persists."
