"""Runtime network policy: the read-only guarantee, enforced per request.

Intuit's ``com.intuit.quickbooks.accounting`` scope grants writes. There is no
read-only scope to ask for (Intuit's page on scopes lists the one Accounting
API scope). So "read-only" is a claim about *this code*, and a claim about
code is only worth what enforces it.

What enforces it is this module, and specifically :func:`enforce`, which
inspects a fully built :class:`httpx.Request` immediately before it is handed
to the transport. Not a source scan, not a wrapper API that callers are
expected to prefer, not a code review convention -- the actual bytes, at the
actual moment of sending.

Each class of request gets its own policy:

============  =========================================================
Class         Allowed
============  =========================================================
Accounting    ``GET`` only, to the selected Intuit origin, the exact
              realm the call was bound to, an allowlisted path
Token refresh ``POST`` to the fixed token endpoint
Exchange      ``POST`` to the fixed token endpoint, CLI only
Revoke        ``POST`` to the fixed revocation endpoint, CLI only
Discovery     ``GET`` to its fixed origin; may confirm pinned values,
              never rewrite a secret-bearing destination
Attachment    ``GET`` with no bearer, cookies or proxy auth
============  =========================================================

Anything that matches no class is denied. The default is denial, so a new call
site added later is refused until someone classifies it deliberately.

"CLI only" rests on the call sites. ``oauth`` marks an exchange and a revoke
as the CLI's itself, so the check below can't tell who called; the auth CLI is
the only caller, and a test keeps it the only one.

A note on the AST test that accompanies this module: it is regression
protection, not proof. It can see that a literal ``"GET"`` was written at a
call site; it cannot see what a variable holds at run time, and ``.send()``
takes a request object rather than a method string at all. This module is where
the guarantee actually lives.
"""

from __future__ import annotations

import ipaddress
import json
import queue
import re
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final
from urllib.parse import parse_qsl  # parsing only; carries no network capability

import httpx

from . import live_values
from .constants import (
    ATTACHMENT_HOST_HINT,
    API_BASES,
    DISCOVERY_URLS,
    ENV_PRODUCTION,
    HARD_EXPIRY_OPT_IN_HEADER,
    HARD_EXPIRY_OPT_IN_VALUE,
    MAX_JSON_RESPONSE_BYTES,
    REVOKE_ENDPOINT,
    TOKEN_ENDPOINT,
    USER_AGENT,
)
from .errors import (
    DeadlineExceeded,
    NetworkPolicyViolation,
    ResponseTooLarge,
    deadline_text,
    too_large_text,
)

_REALM_RE: Final = re.compile(r"^[0-9]{1,32}$")

#: Accounting resources this server may read, as path tails below
#: ``/v3/company/{realm}/``. ``{realm}`` is substituted with the bound realm and
#: then compared, so a tail cannot smuggle a different company in.
ACCOUNTING_PATH_TAILS: Final[tuple[str, ...]] = (
    "companyinfo/{realm}",
    "preferences",
    "query",
    # Reports are allowlisted by exact name, not by prefix. `reports/` as a
    # prefix would admit every report Intuit publishes, including ones whose
    # column sets carry addresses and account numbers and which no projection
    # here has reviewed. Names join this tuple one at a time; each is the
    # endpoint path Intuit's page for that report names
    # (`reports.REPORT_ENDPOINTS`).
    "reports/TransactionList",
    "reports/ProfitAndLoss",
    "reports/BalanceSheet",
    "reports/GeneralLedger",
    "reports/AgedPayables",
    "reports/AgedReceivables",
    "reports/AgedPayableDetail",
    "reports/AgedReceivableDetail",
    # run_report: the 21 documented reports without a named tool, each by the
    # path Intuit's page for it names (`report_catalog.CATALOG`).
    "reports/AccountList",
    "reports/CashFlow",
    "reports/CustomerBalance",
    "reports/CustomerBalanceDetail",
    "reports/CustomerIncome",
    "reports/InventoryValuationDetail",
    "reports/InventoryValuationSummary",
    "reports/JournalReport",
    "reports/ProfitAndLossDetail",
    "reports/ClassSales",
    "reports/CustomerSales",
    "reports/DepartmentSales",
    "reports/ItemSales",
    "reports/TaxSummary",
    "reports/TransactionListByCustomer",
    "reports/TransactionListByVendor",
    "reports/TransactionListWithSplits",
    "reports/TrialBalance",
    "reports/VendorBalance",
    "reports/VendorBalanceDetail",
    "reports/VendorExpenses",
    "cdc",
)

#: Query parameters an accounting request may carry. Anything else -- and any
#: duplicate of one of these -- is refused. Duplicates matter because servers
#: and proxies disagree about which copy wins, which is exactly the gap a
#: smuggled parameter needs.
ACCOUNTING_QUERY_KEYS: Final[frozenset[str]] = frozenset({"query", "minorversion"})

#: Query parameters permitted per path tail. Keyed on the tail rather than
#: applied globally, so a report parameter cannot be smuggled onto /query and
#: vice versa.
#:
#: `accounting_method` is deliberately ABSENT from the TransactionList set.
#:
#: Whether TransactionList honours it isn't established: a live sandbox probe
#: saw no effect on the rows in one window, with a bogus-name control matching
#: the baseline. Sending it could add a parameter that does nothing while
#: looking like it does something. Keeping it out of this allowlist means the
#: network policy refuses any request carrying it, so the mistake cannot be
#: made by accident later.
#: See docs/api-findings.md.
ACCOUNTING_QUERY_KEYS_BY_TAIL: Final[dict[str, frozenset[str]]] = {
    "companyinfo/{realm}": frozenset({"minorversion"}),
    "preferences": frozenset({"minorversion"}),
    "query": frozenset({"query", "minorversion"}),
    "reports/TransactionList": frozenset(
        {
            "minorversion",
            "start_date", "end_date", "date_macro",
            "transaction_type", "vendor", "customer",
            "cleared", "source_account_type",
            "docnum", "memo", "columns",
            "sort_by", "sort_order", "group_by",
        }
    ),
    # Each set is the subset of that report's documented parameters that the
    # named tool may send; anything Intuit lists but the tool does not expose
    # (qzurl, item, adjusted_gain_loss, shipvia, term, custom1-3, ...) is
    # refused here even if a future edit tried to send it. The four aging
    # reports differ materially: AP detail takes accounting_method and no
    # aging_method, AR detail the reverse. BalanceSheet takes `date_macro`
    # because report_balance_sheet accepts `as_of` or `date_macro`, one
    # required.
    "reports/ProfitAndLoss": frozenset(
        {
            "minorversion", "start_date", "end_date", "date_macro",
            "accounting_method", "summarize_column_by",
            "customer", "vendor", "class", "department",
        }
    ),
    "reports/BalanceSheet": frozenset(
        {
            "minorversion", "start_date", "end_date", "date_macro",
            "accounting_method", "summarize_column_by", "class", "department",
        }
    ),
    "reports/GeneralLedger": frozenset(
        {
            "minorversion", "start_date", "end_date",
            "account", "account_type", "columns",
            "accounting_method", "class", "department",
        }
    ),
    "reports/AgedPayables": frozenset(
        {"minorversion", "aging_method", "report_date", "date_macro", "sort_order", "department", "vendor"}
    ),
    "reports/AgedReceivables": frozenset(
        {"minorversion", "aging_method", "report_date", "date_macro", "sort_order", "department", "customer"}
    ),
    "reports/AgedPayableDetail": frozenset(
        {
            "minorversion", "accounting_method", "report_date",
            "start_duedate", "end_duedate", "past_due", "aging_period", "num_periods",
            "columns", "vendor",
        }
    ),
    "reports/AgedReceivableDetail": frozenset(
        {
            "minorversion", "aging_method", "report_date",
            "start_duedate", "end_duedate", "past_due", "aging_period", "num_periods",
            "columns", "customer",
        }
    ),
    # run_report. Each set is exactly what `report_catalog.CATALOG` lets the
    # tool send for that path, plus minorversion; `tests/test_run_report.py`
    # holds the two in step, and the other documented parameters (qzurl,
    # custom1-3, shipvia, term, arpaid, appaid, the moddate/createdate
    # windows, group_by, sort_by, name, ...) are refused here.
    "reports/AccountList": frozenset(
        {"minorversion", "start_date", "end_date", "account_type", "sort_order", "columns"}
    ),
    "reports/CashFlow": frozenset(
        {"minorversion", "start_date", "end_date", "date_macro", "summarize_column_by",
         "customer", "vendor", "class", "item", "department", "sort_order"}
    ),
    "reports/CustomerBalance": frozenset(
        {"minorversion", "customer", "accounting_method", "date_macro", "report_date", "sort_order",
         "summarize_column_by", "department"}
    ),
    "reports/CustomerBalanceDetail": frozenset(
        {"minorversion", "customer", "start_duedate", "end_duedate", "report_date", "sort_order",
         "aging_method", "department", "columns"}
    ),
    "reports/CustomerIncome": frozenset(
        {"minorversion", "customer", "accounting_method", "start_date", "end_date", "date_macro", "class",
         "sort_order", "summarize_column_by", "department", "vendor"}
    ),
    "reports/InventoryValuationDetail": frozenset(
        {"minorversion", "start_date", "end_date", "date_macro", "columns"}
    ),
    "reports/InventoryValuationSummary": frozenset(
        {"minorversion", "date_macro", "item", "report_date", "sort_order", "summarize_column_by"}
    ),
    "reports/JournalReport": frozenset(
        {"minorversion", "start_date", "end_date", "date_macro", "sort_order", "columns"}
    ),
    "reports/ProfitAndLossDetail": frozenset(
        {"minorversion", "customer", "account", "accounting_method", "start_date", "end_date", "date_macro",
         "class", "payment_method", "sort_order", "employee", "department", "vendor", "account_type", "columns"}
    ),
    "reports/ClassSales": frozenset(
        {"minorversion", "customer", "accounting_method", "start_date", "end_date", "date_macro", "class",
         "item", "summarize_column_by", "department"}
    ),
    "reports/CustomerSales": frozenset(
        {"minorversion", "customer", "accounting_method", "start_date", "end_date", "date_macro", "class",
         "item", "sort_order", "summarize_column_by", "department"}
    ),
    "reports/DepartmentSales": frozenset(
        {"minorversion", "customer", "accounting_method", "start_date", "end_date", "date_macro", "class",
         "item", "sort_order", "summarize_column_by", "department"}
    ),
    "reports/ItemSales": frozenset(
        {"minorversion", "customer", "accounting_method", "start_date", "end_date", "date_macro", "class",
         "item", "sort_order", "summarize_column_by", "department"}
    ),
    "reports/TaxSummary": frozenset(
        {"minorversion", "agency_id", "accounting_method", "start_date", "end_date", "date_macro", "sort_order"}
    ),
    # No `columns`, though Intuit's page lists them: Intuit faults on any value
    # (SystemFault 10000, observed in production 2026-09-28;
    # report_catalog.BY_CUSTOMER_COLUMNS_REFUSAL).
    "reports/TransactionListByCustomer": frozenset(
        {"minorversion", "start_date", "end_date", "date_macro", "customer", "transaction_type",
         "source_account_type", "cleared", "docnum", "memo", "payment_method", "department", "sort_order"}
    ),
    "reports/TransactionListByVendor": frozenset(
        {"minorversion", "start_date", "end_date", "date_macro", "vendor", "transaction_type",
         "source_account_type", "cleared", "docnum", "memo", "payment_method", "department", "sort_order",
         "columns"}
    ),
    "reports/TransactionListWithSplits": frozenset(
        {"minorversion", "start_date", "end_date", "date_macro", "docnum", "payment_method",
         "source_account_type", "transaction_type", "sort_order", "columns"}
    ),
    "reports/TrialBalance": frozenset(
        {"minorversion", "accounting_method", "start_date", "end_date", "date_macro", "sort_order",
         "summarize_column_by"}
    ),
    "reports/VendorBalance": frozenset(
        {"minorversion", "accounting_method", "date_macro", "report_date", "sort_order", "summarize_column_by",
         "department", "vendor"}
    ),
    "reports/VendorBalanceDetail": frozenset(
        {"minorversion", "start_duedate", "end_duedate", "accounting_method", "report_date", "sort_order",
         "department", "vendor", "columns"}
    ),
    "reports/VendorExpenses": frozenset(
        {"minorversion", "customer", "vendor", "start_date", "end_date", "date_macro", "class", "sort_order",
         "summarize_column_by", "department", "accounting_method"}
    ),
    "cdc": frozenset({"minorversion", "entities", "changedSince"}),
}

DISCOVERY_QUERY_KEYS: Final[frozenset[str]] = frozenset()
TOKEN_QUERY_KEYS: Final[frozenset[str]] = frozenset()

_CREDENTIAL_HEADERS: Final = ("authorization", "cookie", "proxy-authorization")

_ENCODED_PATH_MARKERS: Final = ("%2f", "%2F", "%5c", "%5C", "%00", "%2e%2e", "%2E%2E")

#: See :func:`_looks_synthetic`.
_SYNTHETIC_CHARS: Final = re.compile(r"[a-z0-9-]+")
_LONG_RUN: Final = re.compile(r"[a-z0-9]{13,}")


class RequestClass(StrEnum):
    ACCOUNTING = "accounting"
    TOKEN_REFRESH = "token_refresh"
    TOKEN_EXCHANGE = "token_exchange"
    TOKEN_REVOKE = "token_revoke"
    DISCOVERY = "discovery"
    ATTACHMENT = "attachment"


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Everything the policy needs in order to decide, supplied by the caller.

    Deliberately explicit. ``realm_id`` in particular is the realm the *call*
    was bound to, resolved from the registry before any URL was built; the
    policy compares it with the realm actually present in the URL. If those
    ever disagree, a request for one company file was about to be sent with
    another company's identity in the path.
    """

    request_class: RequestClass
    environment: str
    api_base: str | None = None
    realm_id: str | None = None
    from_cli: bool = False
    allow_documents: bool = False
    allow_custom_api_base: bool = False
    extra_allowed_query_keys: frozenset[str] = field(default_factory=frozenset)
    #: The download hosts the operator pinned; the attachment class contacts
    #: no other.
    attachment_hosts: tuple[str, ...] = ()


def enforce(request: httpx.Request, ctx: PolicyContext) -> None:
    """Refuse ``request`` unless its class permits it exactly.

    Raises :class:`NetworkPolicyViolation`. Reaching this from ordinary use
    means a bug in this package, so it fails closed and says so plainly.
    """
    handler = _HANDLERS.get(ctx.request_class)
    if handler is None:
        raise NetworkPolicyViolation(f"no policy for request class {ctx.request_class!r}")
    _check_url_shape(request)
    handler(request, ctx)


#: Headers that describe the body as it crossed the wire. The body returned
#: from :func:`send` is already decoded, so they no longer describe it.
_WIRE_ONLY_HEADERS: Final = frozenset({"content-encoding", "content-length", "transfer-encoding"})


# --------------------------------------------------------------------------
# The clients: built here, and reading nothing from the environment
# --------------------------------------------------------------------------


def isolated_tls() -> ssl.SSLContext:
    """TLS for every client this package builds: certifi's roots, no key log.

    The roots are httpx's own default with ``trust_env`` off -- certifi's
    bundle, never a file or directory ``SSL_CERT_FILE`` or ``SSL_CERT_DIR``
    names (httpx 0.28.1, ``_config.py:34-40``). The standard library's
    ``ssl.create_default_context`` also reads ``SSLKEYLOGFILE``, whatever
    httpx is told, unless the interpreter ignores the environment; a session
    whose secrets are in that file is readable, bearer and client secret
    included, by anyone holding the file and a capture of the traffic. So the
    key log is switched off here. The standard library has already opened the
    file by then, writing its comment header; no secret reaches it.
    """
    context = httpx.create_ssl_context(trust_env=False)
    context.keylog_filename = None
    return context


def isolated_client(*, timeout: float, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """An HTTP client that reads nothing from the process environment.

    httpx's default, ``trust_env=True``, takes a proxy from the environment
    -- or, on macOS, from the system settings, which
    ``urllib.request.getproxies`` falls back to (httpx 0.28.1,
    ``_utils.py:37``) -- and a CA file from ``SSL_CERT_FILE``. With both set,
    a local intercepting proxy can read the bearer after every host check has
    passed. Refusing both here is the package's own guarantee, however it is
    started. Every client it uses is built here
    (``tests/test_ast_regression.py`` holds the other modules to that), with
    redirects off as :func:`send` requires.

    ``transport`` is the attachment client's: it passes
    :func:`isolated_transport` through an indirection its tests replace.
    """
    return httpx.Client(
        transport=transport,
        verify=isolated_tls(),
        trust_env=False,
        follow_redirects=False,
        timeout=timeout,
    )


def isolated_transport() -> httpx.HTTPTransport:
    """The transport :func:`isolated_client` builds for itself, for a caller
    that supplies its own.

    A transport built explicitly does not inherit its client's ``trust_env``:
    its own defaults to on, so it reads ``SSL_CERT_FILE`` even under a client
    built with ``trust_env=False``. Given a ready context, it builds none from
    the environment.
    """
    return httpx.HTTPTransport(verify=isolated_tls(), retries=0)


def send(
    client: httpx.Client,
    request: httpx.Request,
    ctx: PolicyContext,
    *,
    max_bytes: int = MAX_JSON_RESPONSE_BYTES,
    what: str = "response",
    deadline: Any = None,
) -> httpx.Response:
    """Enforce, then send with redirects disabled, reading at most ``max_bytes``.

    Redirects are off for every class. A redirect on a credential-bearing
    request is an invitation to forward an ``Authorization`` header to a host
    the policy never approved, and httpx would re-send the body too. With
    ``follow_redirects=False`` a 3xx comes back as itself and nothing is sent
    again; httpx transports never follow one on their own. Every policy check
    here runs before the request is sent, and none after.

    The body is streamed and counted *after* content decoding, and reading
    stops the moment it passes ``max_bytes``. ``deadline``, when given, is
    checked between chunks, so a slow body cannot outlive its call and a
    cancelled call stops reading. What comes back is an ordinary, fully read
    response.
    """
    enforce(request, ctx)
    response = client.send(request, follow_redirects=False, stream=True)
    try:
        body = _read_capped(response, max_bytes=max_bytes, what=what, deadline=deadline)
    finally:
        response.close()
    return httpx.Response(
        response.status_code,
        headers=[
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in _WIRE_ONLY_HEADERS
        ],
        content=body,
        request=request,
    )


def _read_capped(
    response: httpx.Response, *, max_bytes: int, what: str, deadline: Any
) -> bytes:
    """Read the body in chunks, counted against the cap and bounded by the
    deadline -- each blocking read, not only the gaps between them.

    httpx fixes a request's read timeout when the request is built and
    applies it to every socket read. A body that arrives in pieces can take
    that long per piece, so a deadline consulted only after each piece returns
    lets a split response overrun its budget. With a deadline installed, every
    wait for the next chunk is bounded by what is left of the budget; when it
    runs out the response is closed and the call ends at the deadline.
    """
    chunks: list[bytes] = []
    total = 0
    iterator = response.iter_bytes()
    reader = _BoundedReader(iterator, response) if deadline is not None else None
    try:
        while True:
            if reader is None:
                chunk = next(iterator, None)
            else:
                chunk = reader.next_within(deadline)
            if chunk is None:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge(too_large_text(max_bytes, what))
            chunks.append(chunk)
            if deadline is not None:
                deadline.checkpoint()
    finally:
        # Every exit: a worker waiting to hand over a chunk would otherwise wait
        # for as long as the process lives.
        if reader is not None:
            reader._abandon()
    return b"".join(chunks)


class _BoundedReader:
    """Pull chunks on a worker thread so each blocking read can be waited for
    only as long as the call's budget allows.

    The worker reads at most one chunk ahead (the queue holds one), so the
    memory profile is the caller's cap plus one chunk. On a wait
    that outlives the budget the response is closed, which drops the
    connection; the worker's read then returns or fails, it sees the reader
    was abandoned, and it ends. The caller gets ``DeadlineExceeded``.
    """

    _END: Final = object()

    def __init__(self, iterator: Iterator[bytes], response: httpx.Response) -> None:
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._response = response
        self._abandoned = False
        self._thread = threading.Thread(
            target=self._pump, args=(iterator,), name="qbo-bounded-read", daemon=True
        )
        self._thread.start()

    def _pump(self, iterator: Iterator[bytes]) -> None:
        try:
            for chunk in iterator:
                if not self._offer(chunk):
                    return
            self._offer(self._END)
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            self._offer(exc)

    def _offer(self, item: Any) -> bool:
        while not self._abandoned:
            try:
                self._queue.put(item, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def next_within(self, deadline: Any) -> bytes | None:
        """The next chunk, ``None`` at the end, or ``DeadlineExceeded``."""
        wait = deadline.usable()
        if wait > 0:
            try:
                item = self._queue.get(timeout=wait)
            except queue.Empty:
                item = None
        else:
            item = None
        if item is None:
            self._abandon()
            raise DeadlineExceeded(deadline_text(deadline.budget))
        if item is self._END:
            return None
        if isinstance(item, BaseException):
            raise item
        return item

    def _abandon(self) -> None:
        self._abandoned = True
        try:
            self._response.close()
        except Exception:  # noqa: BLE001 - closing is best effort here
            pass


# --------------------------------------------------------------------------
# Shape checks common to every class
# --------------------------------------------------------------------------


def _check_url_shape(request: httpx.Request) -> None:
    url = request.url

    if url.userinfo:
        raise NetworkPolicyViolation("URL carries userinfo")
    if url.fragment:
        raise NetworkPolicyViolation("URL carries a fragment")
    if url.scheme not in ("https", "http"):
        raise NetworkPolicyViolation(f"scheme {url.scheme!r} is not permitted")

    raw_path = request.url.raw_path.decode("ascii", "replace")
    path_only = raw_path.split("?", 1)[0]

    for marker in _ENCODED_PATH_MARKERS:
        if marker in path_only:
            raise NetworkPolicyViolation("URL path carries an encoded separator or null")
    if "//" in path_only:
        raise NetworkPolicyViolation("URL path carries an empty segment")
    segments = path_only.split("/")
    if any(segment in (".", "..") for segment in segments):
        raise NetworkPolicyViolation("URL path carries a traversal segment")


def _check_query(request: httpx.Request, allowed: frozenset[str]) -> None:
    raw_query = request.url.query.decode("ascii", "replace")
    if not raw_query:
        return
    pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True)
    seen: set[str] = set()
    for key, _value in pairs:
        if key not in allowed:
            # The key is never echoed: an unknown key came from somewhere this
            # package does not control.
            raise NetworkPolicyViolation("URL carries an unknown query parameter")
        if key in seen:
            raise NetworkPolicyViolation("URL carries a duplicate query parameter")
        seen.add(key)


def _check_no_credentials(request: httpx.Request) -> None:
    for header in _CREDENTIAL_HEADERS:
        if header in request.headers:
            raise NetworkPolicyViolation(
                f"request carries a {header} header where none is permitted"
            )


def _check_method(request: httpx.Request, allowed: str) -> None:
    if request.method != allowed:
        raise NetworkPolicyViolation(
            f"method {request.method!r} is not permitted for this request class"
        )


def _origin(url: httpx.URL) -> str:
    port = url.port
    default = 443 if url.scheme == "https" else 80
    host = url.host
    if port is None or port == default:
        return f"{url.scheme}://{host}"
    return f"{url.scheme}://{host}:{port}"


def _require_exact_url(request: httpx.Request, expected: str) -> None:
    if str(request.url) != expected:
        raise NetworkPolicyViolation("request URL is not the pinned endpoint for its class")


# --------------------------------------------------------------------------
# Per-class policies
# --------------------------------------------------------------------------


def _check_accounting_shape(request: httpx.Request) -> None:
    """What a GET to an allowlisted path could still carry. No builder sends a
    body, a method-override header or a Host that isn't the URL's, so none goes
    out. A plain list of allowed headers would also refuse a cookie Intuit set."""
    if request.content:
        raise NetworkPolicyViolation("accounting request carries a body")
    names = {name.decode("latin-1").lower() for name, _value in request.headers.raw}
    if any("method" in name for name in names):
        raise NetworkPolicyViolation("accounting request carries a method-override header")
    netloc = request.url.netloc.decode("ascii")
    if request.headers.get("host", netloc) != netloc:
        raise NetworkPolicyViolation("accounting request Host header does not match its URL")


def _accounting(request: httpx.Request, ctx: PolicyContext) -> None:
    _check_method(request, "GET")
    _check_accounting_shape(request)

    if ctx.realm_id is None or not _REALM_RE.match(ctx.realm_id):
        raise NetworkPolicyViolation("accounting request is not bound to a realm")
    if ctx.api_base is None:
        raise NetworkPolicyViolation("accounting request has no selected API base")

    base = ctx.api_base.rstrip("/")
    _refuse_other_environments_host(base, ctx.environment)
    if not ctx.allow_custom_api_base and not _base_is_permitted(base):
        raise NetworkPolicyViolation("accounting request targets a disallowed API base")
    if _origin(request.url) != base:
        raise NetworkPolicyViolation("accounting request does not target the selected origin")
    if not _base_is_intuit(base):
        # A custom or loopback base is test-only and takes synthetic
        # credentials; a real credential never reaches one. The flag alone is
        # not that condition -- an operator sets it once, and it says nothing
        # about which token the next request carries.
        #
        # Three legs, in order of strength. None of the first two looks at
        # what the credential is shaped like.
        _refuse_in_production(ctx)
        _refuse_held_credentials(request)
        _require_synthetic_credential(request)

    expected_paths = {
        f"/v3/company/{ctx.realm_id}/{tail.format(realm=ctx.realm_id)}": tail
        for tail in ACCOUNTING_PATH_TAILS
    }
    matched_tail = expected_paths.get(request.url.path)
    if matched_tail is None:
        raise NetworkPolicyViolation("accounting path is not on the read allowlist")

    # Belt and braces: the realm in the path is compared with the bound realm
    # directly, so a future allowlist entry that forgot to interpolate the
    # realm still cannot address another company.
    parts = request.url.path.split("/")
    if len(parts) < 4 or parts[3] != ctx.realm_id:
        raise NetworkPolicyViolation("accounting path realm differs from the bound realm")

    allowed = ACCOUNTING_QUERY_KEYS_BY_TAIL.get(matched_tail, ACCOUNTING_QUERY_KEYS)
    _check_query(request, allowed | ctx.extra_allowed_query_keys)


def _base_is_intuit(base: str) -> bool:
    """The two real Intuit origins. Everything else is a test-only origin."""
    return base in set(API_BASES.values())


def _refuse_other_environments_host(base: str, environment: str) -> None:
    """An Intuit origin only for its own environment.

    Intuit documents one base per environment, and the bearer token a request
    carries is that environment's. If either origin counted as "real" here, a
    production token could be sent to the sandbox host. The rule is about the
    host, not the credential: no token shape and no
    ``--allow-custom-api-base`` buys a crossing, in either direction, and an
    environment with no base of its own reaches neither.
    """
    if _base_is_intuit(base) and base != API_BASES.get(environment):
        raise NetworkPolicyViolation(
            "accounting request targets the Intuit host of the other environment; "
            "a credential goes only to its own environment's host"
        )


def _looks_synthetic(credential: str) -> bool:
    """Whether ``credential`` is plainly a made-up value rather than a real one.

    Three legs, each of which the token shapes ``.gitleaks.toml`` records -- a
    JWT-shaped access token, an ``AB``-prefixed refresh token, as observed on
    sandbox credentials (Intuit documents only maximum lengths) -- fails on its
    own:

    *Character set.* Lowercase letters, digits and hyphens. Both recorded
    shapes carry uppercase; the access token carries dots and underscores too.

    *Length.* Forty characters, which is already generous for a value like
    ``access-1``.

    *Run length.* No unbroken alphanumeric run past twelve characters, so a
    long opaque string cannot pass by being lowercased. This leg is here
    because a character-set rule with no length bound exempts a whole
    credential shape.

    A shape rule cannot prove a value is fake -- it admits a UUID, for one. It
    is therefore the *fallback*: :func:`_refuse_in_production` and
    :func:`_refuse_held_credentials` run first, and neither looks at shape.
    """
    if not credential or len(credential) > 40:
        return False
    if not _SYNTHETIC_CHARS.fullmatch(credential):
        return False
    return not _LONG_RUN.search(credential)


def _refuse_in_production(ctx: PolicyContext) -> None:
    """Leg 1: production never talks to a test-only base.

    Whatever the request carries. These bases are test-only, so there is no
    production use to preserve, and this leg needs no judgement about any
    credential at all.
    """
    if ctx.environment == ENV_PRODUCTION:
        raise NetworkPolicyViolation(
            "a custom or loopback API base is test-only and is refused in "
            "production, whatever credential the request carries"
        )


def _bearer_credential(request: httpx.Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header:
        return None
    _scheme, _, value = header.partition(" ")
    return value.strip() or None


def _refuse_held_credentials(request: httpx.Request) -> None:
    """Leg 2: nothing this process holds reaches a test-only base.

    Matched by identity against the live-value registry: the client secret and
    every token received from Intuit or read from the store. Two matches, so
    that neither the shape nor the length of a value decides anything:

    *Exact*, on every value that arrives as a value -- the bearer credential,
    each query parameter, each header. This is the identity leg proper, and it
    answers for a value of any length: a text search, with its length floor,
    would let a short held token reach a loopback mock.

    *As a substring*, over the URL, the headers and the body, so a held value
    embedded in something larger is refused too. Substring scanning keeps the
    registry's text-scan floor, a false-positive policy for scanning and not a
    property of credentials.
    """
    exact = [
        *(value for _key, value in request.url.params.multi_items()),
        *request.headers.values(),
    ]
    bearer = _bearer_credential(request)
    if bearer is not None:
        exact.append(bearer)
    parts = [str(request.url), *exact]
    try:
        parts.append(request.content.decode("utf-8", "replace"))
    except httpx.RequestNotRead:  # pragma: no cover - a streaming body; GETs have none
        pass
    held_exactly = any(live_values.holds(value) for value in exact)
    if held_exactly or any(live_values.found_in(part) for part in parts):
        # The value is never echoed, and neither is which part carried it.
        raise NetworkPolicyViolation(
            "a test-only API base may never receive a credential this process "
            "holds; this request carries one"
        )


def _require_synthetic_credential(request: httpx.Request) -> None:
    value = _bearer_credential(request)
    if value is None:
        return
    if not _looks_synthetic(value):
        # The value is never echoed.
        raise NetworkPolicyViolation(
            "a test-only API base may carry only a synthetic credential; this "
            "request carries one that does not have that shape"
        )


def _base_is_permitted(base: str) -> bool:
    """The two Intuit hosts, or a loopback mock.

    A loopback origin can only reach the machine the server already runs on, so
    allowing it does not widen the blast radius, and it is how the test suite
    exercises the real client against a real socket. Any other origin needs
    ``--allow-custom-api-base``, which is checked by the caller.
    """
    return base in set(API_BASES.values()) or is_loopback_base(base)


def is_loopback_base(base: str) -> bool:
    try:
        url = httpx.URL(base)
    except Exception:
        return False
    return url.host in ("127.0.0.1", "::1", "localhost")


def _check_form_body(
    request: httpx.Request,
    *,
    grant_type: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    """The body half of the OAuth policy.

    OAuth is an enumerated method, path **and body** policy, not a file-wide
    exemption. With only method, endpoint and CLI context checked, a
    refresh-class POST to the pinned endpoint carrying ``grant_type=password``
    and a username would pass. The endpoint being Intuit's does not make an
    arbitrary grant acceptable to submit under this app's Basic credentials.

    Duplicates are refused for the same reason they are in the query string:
    servers and proxies disagree about which copy wins.
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise NetworkPolicyViolation("token request body is not a form")

    raw = request.content.decode("ascii", "replace")
    if not raw:
        raise NetworkPolicyViolation("token request body is empty")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise NetworkPolicyViolation("token request body is not a valid form") from None

    seen: dict[str, str] = {}
    for key, value in pairs:
        if key in seen:
            raise NetworkPolicyViolation("token request body carries a duplicate field")
        if key not in required and key not in optional:
            # Never echoed: an unknown field came from somewhere this package
            # does not control.
            raise NetworkPolicyViolation("token request body carries an unknown field")
        seen[key] = value

    missing = required - seen.keys()
    if missing:
        raise NetworkPolicyViolation("token request body is missing a required field")
    if seen.get("grant_type") != grant_type:
        raise NetworkPolicyViolation(
            "token request body does not carry the grant this request class permits"
        )


#: The headers a token request may carry, by lower-cased name: what
#: ``oauth._post`` sets (Accept, User-Agent, Content-Type and the Basic
#: Authorization) and what httpx adds itself (Host, Accept-Encoding,
#: Connection, Content-Length). Headers follow the same principle as method,
#: path and body, so nothing else is sent: no cookie, no method override, no
#: proxy credential.
TOKEN_REQUEST_HEADERS: Final[frozenset[str]] = frozenset(
    {"host", "accept", "accept-encoding", "connection", "user-agent",
     "content-type", "content-length", "authorization"}
)

#: The values the builders set, where they set a fixed one.
_TOKEN_HEADER_VALUES: Final[dict[str, str]] = {
    "accept": "application/json",
    "user-agent": USER_AGENT,
    HARD_EXPIRY_OPT_IN_HEADER: HARD_EXPIRY_OPT_IN_VALUE,
}


def _check_token_headers(request: httpx.Request, *, opt_in: bool) -> None:
    """The header half of the OAuth policy.

    Every header must be one the builders send; the hard-expiry opt-in only
    on the two calls that issue a token (``opt_in``); each fixed value must be
    the builders' own; Host must be the URL's; the credential must be the
    app's Basic one, never a bearer; and Content-Length must be the body's.
    A duplicated header is refused, as a duplicated body field is. No header
    name or value is echoed: an unexpected one came from somewhere this
    package does not control.
    """
    names = [name.decode("latin-1").lower() for name, _value in request.headers.raw]
    if len(names) != len(set(names)):
        raise NetworkPolicyViolation("token request carries a duplicated header")
    allowed = TOKEN_REQUEST_HEADERS | ({HARD_EXPIRY_OPT_IN_HEADER} if opt_in else frozenset())
    if set(names) - allowed:
        raise NetworkPolicyViolation("token request carries a header outside the enumerated set")

    headers = request.headers
    for name, value in _TOKEN_HEADER_VALUES.items():
        if name in headers and headers[name] != value:
            raise NetworkPolicyViolation(
                "token request header carries a value the OAuth builders never send"
            )
    if "host" in headers and headers["host"] != request.url.netloc.decode("ascii"):
        raise NetworkPolicyViolation("token request Host header does not match its URL")
    if "authorization" in headers and not headers["authorization"].lower().startswith("basic "):
        raise NetworkPolicyViolation(
            "token request Authorization header is not the app's Basic credential"
        )
    if "content-length" in headers and headers["content-length"] != str(len(request.content)):
        raise NetworkPolicyViolation("token request Content-Length header disagrees with its body")


def _token_post(request: httpx.Request, ctx: PolicyContext, *, cli_only: bool) -> None:
    _check_method(request, "POST")
    _require_exact_url(request, TOKEN_ENDPOINT)
    _check_query(request, TOKEN_QUERY_KEYS)
    if cli_only and not ctx.from_cli:
        raise NetworkPolicyViolation(
            "this request class is permitted only from the auth CLI"
        )
    _check_token_headers(request, opt_in=True)


def _refresh(request: httpx.Request, ctx: PolicyContext) -> None:
    _token_post(request, ctx, cli_only=False)
    _check_form_body(
        request,
        grant_type="refresh_token",
        required=frozenset({"grant_type", "refresh_token"}),
    )


def _exchange(request: httpx.Request, ctx: PolicyContext) -> None:
    _token_post(request, ctx, cli_only=True)
    _check_form_body(
        request,
        grant_type="authorization_code",
        required=frozenset({"grant_type", "code", "redirect_uri"}),
        optional=frozenset({"code_verifier"}),
    )


def _revoke(request: httpx.Request, ctx: PolicyContext) -> None:
    _check_method(request, "POST")
    _require_exact_url(request, REVOKE_ENDPOINT)
    _check_query(request, TOKEN_QUERY_KEYS)
    if not ctx.from_cli:
        raise NetworkPolicyViolation(
            "this request class is permitted only from the auth CLI"
        )
    # A revocation issues no token, so it carries no hard-expiry opt-in.
    _check_token_headers(request, opt_in=False)
    _check_revoke_body(request)


def _check_revoke_body(request: httpx.Request) -> None:
    """Revocation takes a JSON object carrying exactly one key.

    ``oauth.revoke_token`` sends a JSON object rather than a form, so this
    gets its own check instead of sharing the form one. The client id and
    secret travel in the Basic header; a body field naming either would be
    putting a credential somewhere it does not belong.
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise NetworkPolicyViolation("revocation request body is not JSON")
    try:
        body = json.loads(request.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise NetworkPolicyViolation("revocation request body is not valid JSON") from None
    if not isinstance(body, dict) or set(body) != {"token"}:
        raise NetworkPolicyViolation(
            "revocation request body carries something other than the token"
        )
    if not isinstance(body["token"], str) or not body["token"]:
        raise NetworkPolicyViolation("revocation request body carries no token")


def _discovery(request: httpx.Request, ctx: PolicyContext) -> None:
    _check_method(request, "GET")
    expected = DISCOVERY_URLS.get(ctx.environment)
    if expected is None:
        raise NetworkPolicyViolation("discovery has no pinned URL for this environment")
    _require_exact_url(request, expected)
    _check_query(request, DISCOVERY_QUERY_KEYS)
    # Discovery never carries a credential, and it never decides where one is
    # sent. It exists to confirm the pinned endpoints, not to supply them.
    _check_no_credentials(request)


def _attachment(request: httpx.Request, ctx: PolicyContext) -> None:
    """The download class.

    The URL is upstream data -- an Attachable read's ``TempDownloadUri``, a
    signed link on a storage host -- so the checks are about WHERE it goes and
    WHAT it carries, not about a query this package built: https on the
    default port; no credential header of any kind; a host the operator
    pinned, exact or ``*.suffix``, never an IP literal (except a loopback one,
    over http on any port, when the API base is itself a loopback mock outside
    production, the accounting side's own test-only admission); and the query left opaque,
    parsed for shape only, because the signature in it is the storage host's
    business. Every redirect destination comes back through here before it is
    contacted.
    """
    _check_method(request, "GET")
    if not ctx.allow_documents:
        raise NetworkPolicyViolation(
            "document fetching is disabled; start with --allow-documents to permit it"
        )
    # No bearer, no cookies, no proxy auth: attaching this app's bearer token
    # to a storage link would hand the credential to a third party.
    _check_no_credentials(request)
    if not ctx.attachment_hosts:
        raise NetworkPolicyViolation(
            "no download host is pinned: set QBO_ATTACHMENT_HOSTS or pass "
            f"--attachment-host, and every download stays refused until then; "
            f"{ATTACHMENT_HOST_HINT}"
        )
    host = (request.url.host or "").strip().lower().rstrip(".")
    if not host:
        raise NetworkPolicyViolation("attachment URL carries no host")
    test_mode = ctx.environment != ENV_PRODUCTION and bool(ctx.api_base) and is_loopback_base(ctx.api_base or "")
    literal = _ip_literal(host)
    loopback_mock = test_mode and literal is not None and literal.is_loopback
    if request.url.scheme != "https" and not loopback_mock:
        # The loopback mock serves plain http, as the accounting side's
        # loopback base does (validate_api_base); nothing else may.
        raise NetworkPolicyViolation("attachment requests must use https")
    if literal is not None and not loopback_mock:
        raise NetworkPolicyViolation(
            "the download host is an IP literal, which is never a pinned host (a "
            "loopback literal is admitted only against a loopback API base, outside "
            "production)"
        )
    if not _host_pinned(host, ctx.attachment_hosts):
        raise NetworkPolicyViolation(
            f"the download host '{host}' is not pinned in QBO_ATTACHMENT_HOSTS, so it is "
            f"not contacted"
        )
    if request.url.port not in (None, 443) and not loopback_mock:
        # The loopback mock listens on whatever port the test chose; a pinned
        # name gets the default port in every mode.
        raise NetworkPolicyViolation("the download URL names a port other than https's default")
    _check_query_shape(request)


def _ip_literal(host: str) -> Any:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _host_pinned(host: str, pinned: tuple[str, ...]) -> bool:
    for entry in pinned:
        entry = entry.strip().lower().rstrip(".")
        if not entry:
            continue
        if entry.startswith("*."):
            suffix = entry[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == entry:
            return True
    return False


def _check_query_shape(request: httpx.Request) -> None:
    """A signed storage query is opaque; it still has to parse as one."""
    raw_query = request.url.query.decode("ascii", "replace")
    if not raw_query:
        return
    try:
        parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise NetworkPolicyViolation("attachment URL query is malformed") from None


_HANDLERS: Final = {
    RequestClass.ACCOUNTING: _accounting,
    RequestClass.TOKEN_REFRESH: _refresh,
    RequestClass.TOKEN_EXCHANGE: _exchange,
    RequestClass.TOKEN_REVOKE: _revoke,
    RequestClass.DISCOVERY: _discovery,
    RequestClass.ATTACHMENT: _attachment,
}


# --------------------------------------------------------------------------
# URL construction
# --------------------------------------------------------------------------


def build_accounting_url(api_base: str, realm_id: str, tail: str) -> httpx.URL:
    """Build an accounting URL from validated pieces.

    Callers never supply a URL. They supply an allowlisted tail and a realm the
    registry vouched for; anything absolute, anything with a separator, and
    anything off the allowlist is refused here, before :func:`enforce` gets a
    second chance at it.
    """
    if not _REALM_RE.match(realm_id):
        raise NetworkPolicyViolation("realm id is not numeric")
    if tail not in ACCOUNTING_PATH_TAILS:
        raise NetworkPolicyViolation("path tail is not on the read allowlist")
    resolved = tail.format(realm=realm_id)
    if "://" in resolved or resolved.startswith("/"):
        raise NetworkPolicyViolation("path tail must be relative")
    return httpx.URL(f"{api_base.rstrip('/')}/v3/company/{realm_id}/{resolved}")


def validate_api_base(base: str, environment: str, *, allow_custom: bool) -> str:
    """Validate ``--api-base`` at startup.

    Restricted to this environment's Intuit host or a loopback mock.
    ``--allow-custom-api-base`` lifts the restriction for people pointing this
    at a recording proxy; it is a flag rather than a silent default because the
    API base is where every bearer token goes.

    Each environment accepts only its own Intuit host: the other environment's
    is refused in both directions, whatever the flag says. In production a
    custom or loopback base is refused too. The network policy refuses both at
    send time anyway (:func:`_refuse_other_environments_host`,
    :func:`_refuse_in_production`); this says so at startup, as a configuration
    error, instead of letting every call fail with a policy violation that
    reads like a bug in this package.
    """
    from .errors import StartupError

    candidate = base.rstrip("/")
    try:
        url = httpx.URL(candidate)
    except Exception as exc:
        raise StartupError("--api-base is not a valid URL") from exc

    if url.userinfo:
        raise StartupError("--api-base must not carry userinfo")
    if url.query or url.fragment or url.path not in ("", "/"):
        raise StartupError("--api-base must be an origin, with no path, query or fragment")

    if candidate in API_BASES.values():
        if candidate != API_BASES.get(environment):
            raise StartupError(
                "--api-base names the Intuit host of the other environment; each "
                "environment sends its credentials only to its own Intuit host"
            )
        return candidate
    if environment == ENV_PRODUCTION:
        raise StartupError(
            "--api-base must be the Intuit production host in production; a "
            "custom or loopback base is test-only and is refused in production"
        )
    if is_loopback_base(candidate) and url.scheme in ("http", "https"):
        return candidate
    if allow_custom:
        if url.scheme != "https":
            raise StartupError("a custom --api-base must use https")
        return candidate
    raise StartupError(
        "--api-base must be an Intuit host or a loopback mock; "
        "pass --allow-custom-api-base to override"
    )
