"""The loopback OAuth callback listener.

This is the only module in the package that opens a listening socket, and it
makes no outbound connection of any kind. It is separate from
:mod:`qbo_multicompany_mcp.auth_cli` precisely so the AST test that forbids
alternative HTTP clients can exempt one small, auditable file rather than the
whole CLI.

The rules it implements, and why each one is here:

*Bind before opening the browser.* If the listener is not already accepting
when the browser is launched, a fast consent leaves the callback hitting a
closed port and the authorization code is gone. Binding first also means a port
conflict is discovered before a human has been sent anywhere.

*Never silently choose another port.* The redirect URI is registered with
Intuit and must match exactly. A listener that quietly moved to port 8766 would
consent successfully and then drop every callback on the floor, which looks
like Intuit being broken. A bind failure exits 2 and says which port.

*An invalid local request must not consume the one valid callback slot.*
Anything on the machine can reach loopback -- another browser tab, a link
preview, a health check, a port scanner. If a request with a wrong or missing
``state`` consumed the attempt, any of those would be able to cancel a consent
in progress, and a malicious one could do it deliberately and repeatedly. So
invalid requests are answered with 400 and the attempt stays open.

*The attempt is claimed exactly once, by whoever gets there first.* The browser
path and the ``--manual`` paste path compete for the same claim, under the same
lock, so a user who does both cannot exchange one code twice.
"""

from __future__ import annotations

import errno
import hmac
import secrets
import socket  # inbound listener only; this module makes no outbound connection
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit  # parsing only, no network capability

from .constants import CALLBACK_HOST_V4, CALLBACK_HOST_V6, CALLBACK_PATH, CALLBACK_PORT
from .errors import StartupError

#: What binding ``::1`` raises on a machine whose loopback has no IPv6: no such
#: address family or protocol, or no such address. Any other failure, above all
#: the port being taken, means something else may answer there.
IPV6_UNAVAILABLE_ERRNOS: Final = frozenset(
    {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL}
)

#: 32 bytes of entropy, URL-safe. Single use, and bound to one attempt.
STATE_BYTES: Final = 32
STATE_TTL: Final = timedelta(minutes=15)

#: How long one connection gets to deliver its request. A browser's callback is
#: one short GET, so a connection that idles or sends a byte at a time is cut.
REQUEST_DEADLINE_SECONDS: Final = 10.0

#: How many connections each address's listener serves at once. A browser
#: needs one or two. Unbounded, a local flood of idle ones would use up the file
#: descriptors the token exchange and the store write need too. Bounded, a flood
#: can still keep the browser's callback out while it lasts.
MAX_CONNECTIONS: Final = 16

_REQUIRED_PARAMS: Final = ("code", "state", "realmId")
_OPTIONAL_PARAMS: Final = frozenset({"error", "error_description"})


class CallbackRejected(Exception):
    """The callback was not acceptable. The attempt remains unclaimed."""


class AttemptAlreadyUsed(Exception):
    """The one-use attempt has already been claimed."""


@dataclass(frozen=True, slots=True)
class CallbackParams:
    code: str
    state: str
    realm_id: str


class Attempt:
    """One consent attempt: one ``state``, one claim, one exchange.

    The ``state`` is random and new for each attempt, so a callback produced by
    a *different* attempt -- a concurrent connect for another entity, say --
    carries a state this one never issued and cannot satisfy it. The entity,
    environment, client identity and redirect URI are recorded here, not
    checked: the realm check and the exchange's settings are in
    ``auth_cli._finish_connect``.
    """

    def __init__(
        self,
        *,
        entity_key: str,
        environment: str,
        client_fingerprint: str,
        redirect_uri: str,
        ttl: timedelta = STATE_TTL,
    ) -> None:
        self.entity_key = entity_key
        self.environment = environment
        self.client_fingerprint = client_fingerprint
        self.redirect_uri = redirect_uri
        self.state = secrets.token_urlsafe(STATE_BYTES)
        self.created_at = datetime.now(UTC)
        self.expires_at = self.created_at + ttl
        self._lock = threading.Lock()
        self._claimed = False
        self._exchanged = False
        self.params: CallbackParams | None = None
        self.completed = threading.Event()

    # -- validation --------------------------------------------------------

    def expired(self, *, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) > self.expires_at

    def check_state(self, candidate: str) -> None:
        """Constant-time comparison against a single-use, time-limited value.

        Constant time because a callback handler that leaks how much of a
        ``state`` matched, via timing, hands an attacker a way to build a valid
        one byte at a time.
        """
        if self.expired():
            raise CallbackRejected("the consent attempt expired; start it again")
        if not hmac.compare_digest(self.state.encode(), candidate.encode("utf-8", "surrogatepass")):
            raise CallbackRejected("state did not match this consent attempt")

    def claim(self, params: CallbackParams) -> None:
        """Claim the attempt. Raises if the state is wrong or it is already used.

        Ordering matters: the state is checked *before* the claim is taken, so
        a wrong-state request leaves the attempt available for the real one.
        """
        self.check_state(params.state)
        with self._lock:
            if self._claimed:
                raise AttemptAlreadyUsed(
                    "this consent attempt has already been handled "
                    "(the browser and the manual paste both delivered a callback)"
                )
            self._claimed = True
            self.params = params
        self.completed.set()

    def begin_exchange(self) -> None:
        """Mark the single permitted token exchange as started.

        Called before the exchange, not after, so a timeout part-way through
        cannot be followed by a second attempt with the same code. An
        authorization code is single use (Intuit's Authorization FAQ): a
        second exchange returns ``invalid_grant``, the same error a wrong
        client secret produces, so the two are indistinguishable.
        """
        with self._lock:
            if self._exchanged:
                raise AttemptAlreadyUsed(
                    "the authorization code for this attempt has already been exchanged"
                )
            self._exchanged = True


def parse_callback_query(raw_query: str, *, redirect_path: str = CALLBACK_PATH) -> CallbackParams:
    """Parse and validate a callback query string.

    Duplicate parameters are refused outright. Servers, proxies and browsers do
    not agree on which copy of a repeated parameter wins, and a consent flow is
    exactly the place where "which ``realmId`` did you mean?" must have one
    answer.
    """
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=bool(raw_query))
    except ValueError:
        # The parser's message quotes the fragment it stopped on.
        raise CallbackRejected("the callback query could not be read") from None
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise CallbackRejected("the callback carried a duplicate query parameter")
        values[key] = value

    if "error" in values:
        raise CallbackRejected("Intuit reported an error on the consent screen")

    unknown = set(values) - set(_REQUIRED_PARAMS) - _OPTIONAL_PARAMS
    if unknown:
        raise CallbackRejected("the callback carried an unexpected query parameter")

    missing = [name for name in _REQUIRED_PARAMS if not values.get(name)]
    if missing:
        raise CallbackRejected("the callback was missing a required parameter")

    return CallbackParams(
        code=values["code"], state=values["state"], realm_id=values["realmId"]
    )


def parse_pasted_url(pasted: str) -> CallbackParams:
    """Parse a callback URL pasted at the ``--manual`` prompt.

    The path is checked as well as the query: a URL pasted from the wrong tab
    should fail loudly rather than be half-understood.
    """
    try:
        parts = urlsplit(pasted.strip())
    except ValueError:
        raise CallbackRejected("that address could not be read; paste the full address "
                               "the browser ended up on") from None
    if parts.path and not parts.path.endswith(CALLBACK_PATH):
        raise CallbackRejected(
            f"that URL's path is not {CALLBACK_PATH}; paste the full address the "
            f"browser ended up on"
        )
    return parse_callback_query(parts.query)


_PAGE_OK = b"""<!doctype html><meta charset="utf-8"><title>Connected</title>
<p>QuickBooks connection handed back to the command line. You can close this tab.</p>
"""
_PAGE_BAD = b"""<!doctype html><meta charset="utf-8"><title>Not accepted</title>
<p>This request was not accepted. Return to the terminal for details.</p>
"""


class _Handler(BaseHTTPRequestHandler):
    attempt: Attempt  # set by the receiver

    server_version = "qbo-multicompany-mcp"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        # One deadline for the whole request. A timeout on each read would let
        # a client that sends a byte at a time keep the connection for ever.
        self._expired = threading.Event()
        self._deadline = threading.Timer(REQUEST_DEADLINE_SECONDS, self._cut)
        self._deadline.daemon = True
        self._deadline.start()

    def _cut(self) -> None:
        # Set first. The reads then end early, and do_GET answers nothing once
        # it's set, not even a whole request that landed as the deadline fell.
        self._expired.set()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # the request finished as the deadline fell

    def finish(self) -> None:
        self._deadline.cancel()
        super().finish()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        if self._expired.is_set():
            return  # cut off by the deadline, so never a callback
        try:
            parts = urlsplit(self.path)
        except ValueError:
            self._respond(400, _PAGE_BAD)
            return
        if parts.path != CALLBACK_PATH:
            self._respond(404, _PAGE_BAD)
            return
        try:
            params = parse_callback_query(parts.query)
            self.attempt.claim(params)
        except AttemptAlreadyUsed:
            self._respond(409, _PAGE_BAD)
            return
        except CallbackRejected:
            # 400, and the attempt is still open for the real callback.
            self._respond(400, _PAGE_BAD)
            return
        self._respond(200, _PAGE_OK)

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        # Both directives: the wording of Intuit's security requirements under
        # App server configuration.
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence the default access log.

        It writes the full request line -- which carries the authorization code
        and the state -- to stderr. Neither is ever logged by this package.
        """
        return


class _Server(ThreadingHTTPServer):
    """Each connection on a thread of its own, so an idle one can't keep the
    callback waiting, and at most :data:`MAX_CONNECTIONS` at once.

    Daemon threads, which closing never waits for: a connection still open
    when the receiver closes ends at its deadline, and can't hold the process.
    """

    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request: Any, client_address: Any) -> None:
        # One over the cap is closed at once: a flood can keep the callback out
        # while it lasts, never starve the rest of the command.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """One fixed line and no traceback: what failed may be a request line,
        and a callback's carries the authorization code and the state."""
        print("qbo-multicompany-mcp: a callback request failed", file=sys.stderr)


class _V6Server(_Server):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        # v6-only, so this bind does not collide with the separate IPv4 bind.
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


class LoopbackReceiver:
    """Binds both loopback literals on the fixed port, then serves.

    Both literals because a browser resolving ``localhost`` may pick either
    family, and a listener on only one of them turns into an intermittent,
    machine-dependent failure that looks like Intuit being flaky.
    """

    def __init__(self, attempt: Attempt, *, port: int = CALLBACK_PORT) -> None:
        """``port=0`` asks the OS for an ephemeral port. **Tests only.**

        The CLI always passes the fixed :data:`CALLBACK_PORT`, and its guarantee
        is unchanged: on a bind failure it exits 2 rather than silently moving,
        because the redirect URI registered with Intuit names one port.

        The test suite needs the opposite. Binding the real port would stop
        the suite running while any consent was in flight on the machine, and
        a developer's live ``connect`` would make unrelated tests fail with
        "Address already in use". An ephemeral port gives the tests a real
        socket with no collision.
        """
        self.attempt = attempt
        self.port = port
        self._servers: list[HTTPServer] = []
        self._threads: list[threading.Thread] = []

    @property
    def bound_port(self) -> int:
        """The port actually bound. Equals ``port`` unless ``port=0``."""
        if not self._servers:
            raise StartupError("the callback listener is not bound yet")
        return int(self._servers[0].server_address[1])

    def bind(self) -> None:
        handler = type("_BoundHandler", (_Handler,), {"attempt": self.attempt})
        bound_any = False
        port = self.port
        for host, factory in (
            (CALLBACK_HOST_V4, _Server),
            (CALLBACK_HOST_V6, _V6Server),
        ):
            try:
                server = factory((host, port), handler)
            except OSError as exc:
                if host == CALLBACK_HOST_V6 and exc.errno in IPV6_UNAVAILABLE_ERRNOS:
                    # A machine with IPv6 disabled is ordinary and still works.
                    continue
                # Anything else is fatal, on either literal. Whatever holds the
                # port gets the callback from a browser that resolves
                # localhost to that family, and the IPv4 literal is also where
                # the hosted production callback forwards. There is no second
                # port to try, because the port is fixed in the redirect URI
                # registered with Intuit.
                for bound in self._servers:
                    # Never started, so closing is enough: shutdown() would
                    # wait for a serve loop that never ran.
                    bound.server_close()
                self._servers.clear()
                address = f"[{host}]" if ":" in host else host
                raise StartupError(
                    f"cannot bind {address}:{port} for the OAuth callback "
                    f"({exc.strerror}). Stop whatever is using that port and "
                    f"retry; this tool will not silently use a different port, "
                    f"because the redirect URI registered with Intuit is fixed."
                ) from exc
            self._servers.append(server)
            bound_any = True
            # With an ephemeral request the OS picks the port for the first
            # bind; the second literal must then be pinned to the same one, or
            # the two families would answer on different ports.
            port = int(server.server_address[1])

        if not bound_any:  # pragma: no cover - the v4 branch raises first
            raise StartupError(f"cannot bind any loopback address on port {self.port}")

    def start(self) -> None:
        for server in self._servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._threads.append(thread)

    def wait(self, timeout: float) -> CallbackParams | None:
        if self.attempt.completed.wait(timeout):
            return self.attempt.params
        return None

    def close(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers.clear()

    def __enter__(self) -> LoopbackReceiver:
        self.bind()
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
