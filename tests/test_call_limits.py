"""The declared limits, each proven to bound something.

``PER_CALL_BUDGET_SECONDS`` has to span the whole call: otherwise each request
gets its own 120 seconds and lock waits, rate waits and pages each have budgets
of their own. A response body read eagerly has no cap, so a 33 MiB JSON body
would be accepted. Without ``Accept-Encoding: identity`` a size cap has nothing
solid to count. And a read that retries only a 401 gives up on a transient 5xx.
"""

from __future__ import annotations

import gzip
import time

import anyio
import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from qbo_multicompany_mcp import client as client_mod
from qbo_multicompany_mcp import constants
from qbo_multicompany_mcp.client import AccountingClient
from qbo_multicompany_mcp.errors import QboMcpError
from qbo_multicompany_mcp.tools import register_tools

from .conftest import REALM_MAIN, MockIntuit, make_token_response

MIB = 1024 * 1024


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _mock_client(settings, store, handler) -> AccountingClient:
    return AccountingClient(
        settings, store, http=httpx.Client(transport=httpx.MockTransport(handler))
    )


def _json_of_size(size: int) -> bytes:
    """A valid JSON object of exactly ``size`` bytes."""
    head, tail = b'{"QueryResponse":{},"pad":"', b'"}'
    return head + b"x" * (size - len(head) - len(tail)) + tail


def _answer(status: int = 200, body: bytes | None = None, **headers):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, content=body if body is not None else b'{"QueryResponse":{}}',
            headers=headers, request=request,
        )
    return handler


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff sleeps instead of taking them."""
    slept: list[float] = []
    monkeypatch.setattr(client_mod, "_sleep", slept.append, raising=False)
    monkeypatch.setattr(client_mod, "_jitter", lambda low, high: high, raising=False)
    return slept


def _server(mock_settings, registry, credentials, actor, **token) -> MCPServer:
    mock_settings.open_store(credentials=credentials).commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(**token), actor=actor,
    )
    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    return server


def _vendors(count: int) -> list[dict]:
    return [{"Id": str(i), "DisplayName": f"V{i:02d}", "Active": True} for i in range(count)]


@pytest.fixture
def one_row_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every walk page one row long, so a walk is many requests. The
    mock's own page size is overridden by the MAXRESULTS the walker sends."""
    from qbo_multicompany_mcp import pagination

    monkeypatch.setattr(pagination, "MAX_RESULTS_CEILING", 1)


# --------------------------------------------------------------------------
# Accept-Encoding and the byte caps
# --------------------------------------------------------------------------


def test_accounting_requests_ask_for_identity_encoding(settings, seeded_store, registry) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"Preferences": {}}, request=request)

    with _mock_client(settings, seeded_store, handler) as client:
        client.get(registry["acme_main"], "preferences")
    assert seen[0].headers["Accept-Encoding"] == "identity"


def test_a_json_body_over_32_mib_is_refused(settings, seeded_store, registry) -> None:
    """One MiB over the cap is refused, not parsed."""
    with _mock_client(settings, seeded_store, _answer(body=_json_of_size(33 * MIB))) as client:
        with pytest.raises(QboMcpError, match="32 MiB"):
            client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})


def test_a_json_body_at_the_cap_is_read(settings, seeded_store, registry) -> None:
    """The control: the cap is the cap, not a smaller number."""
    with _mock_client(settings, seeded_store, _answer(body=_json_of_size(32 * MIB))) as client:
        result = client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})
    assert "QueryResponse" in result.payload


def test_a_report_body_over_16_mib_is_refused(settings, seeded_store, registry) -> None:
    """17 MiB is under the JSON cap and over the report cap."""
    body = _json_of_size(17 * MIB)
    with _mock_client(settings, seeded_store, _answer(body=body)) as client:
        client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})
        with pytest.raises(QboMcpError, match="16 MiB"):
            client.report(registry["acme_main"], "TransactionList", {"start_date": "2026-01-01"})


def test_the_cap_counts_decoded_bytes_not_wire_bytes(settings, seeded_store, registry) -> None:
    """If Intuit ignores identity and compresses anyway, 33 MiB of JSON can
    arrive as a few kilobytes. The cap is on what it decodes to."""
    wire = gzip.compress(_json_of_size(33 * MIB))
    assert len(wire) < MIB
    handler = _answer(body=wire, **{"Content-Encoding": "gzip"})
    with _mock_client(settings, seeded_store, handler) as client:
        with pytest.raises(QboMcpError, match="32 MiB"):
            client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})


def test_an_oversized_body_is_abandoned_without_being_read_to_the_end(
    settings, seeded_store, registry
) -> None:
    """Streamed and stopped, not read whole and then measured."""
    produced: list[int] = []

    def chunks():
        yield b'{"QueryResponse":{},"pad":"'
        for _ in range(64):
            produced.append(1)
            yield b"x" * MIB
        yield b'"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks(), request=request)

    with _mock_client(settings, seeded_store, handler) as client:
        with pytest.raises(QboMcpError, match="32 MiB"):
            client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})
    assert len(produced) <= 34


def test_an_oversized_token_response_is_an_ambiguous_refresh(monkeypatch) -> None:
    """A refresh whose response arrived but could not be read may still have
    been accepted: that is TokenResponseLost, which quarantines the realm,
    not a clean failure that invites resubmitting the token."""
    from qbo_multicompany_mcp import oauth

    # A well-formed token response that happens to be huge, so the only thing
    # wrong with it is its size.
    head = b'{"access_token":"zq-a-access-value","refresh_token":"zq-a-refresh-value","pad":"'
    huge = head + b"x" * (33 * MIB) + b'"}'
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=huge, request=r))
    with pytest.raises(oauth.TokenResponseLost):
        oauth.refresh_tokens(
            refresh_token="refresh-1", client_id="test-client-id",
            client_secret="test-client-secret", environment="sandbox",
            client=httpx.Client(transport=transport),
        )


# --------------------------------------------------------------------------
# Retries: idempotent GETs, 5xx on its own backoff, three retries, base 1 s,
# factor 2, full jitter -- and no retry of a 504 or a read timeout on a report.
# --------------------------------------------------------------------------


def _sequence(*outcomes):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={"QueryResponse": {}}, request=request)

    return handler, calls


def test_a_transient_5xx_on_a_read_is_retried(settings, seeded_store, registry, no_sleep) -> None:
    handler, calls = _sequence(503, 200)
    with _mock_client(settings, seeded_store, handler) as client:
        client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})
    assert len(calls) == 2
    assert no_sleep == [1.0]


def test_retries_stop_after_three_with_exponential_backoff(
    settings, seeded_store, registry, no_sleep
) -> None:
    handler, calls = _sequence(500)
    with _mock_client(settings, seeded_store, handler) as client:
        with pytest.raises(QboMcpError, match="status=500"):
            client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})
    assert len(calls) == 4
    assert no_sleep == [1.0, 2.0, 4.0]


def test_the_backoff_is_full_jitter(settings, seeded_store, registry, monkeypatch) -> None:
    """Uniform over [0, cap], not the cap itself."""
    bounds: list[tuple[float, float]] = []
    monkeypatch.setattr(client_mod, "_sleep", lambda s: None, raising=False)
    monkeypatch.setattr(
        client_mod, "_jitter", lambda low, high: bounds.append((low, high)) or 0.0, raising=False
    )
    handler, _calls = _sequence(502, 502, 200)
    with _mock_client(settings, seeded_store, handler) as client:
        client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})
    assert bounds == [(0.0, 1.0), (0.0, 2.0)]


def test_a_504_on_a_non_report_read_is_retried(settings, seeded_store, registry, no_sleep) -> None:
    handler, calls = _sequence(504, 200)
    with _mock_client(settings, seeded_store, handler) as client:
        client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})
    assert len(calls) == 2


def test_a_504_on_a_report_is_not_retried(settings, seeded_store, registry, no_sleep) -> None:
    handler, calls = _sequence(504, 200)
    with _mock_client(settings, seeded_store, handler) as client:
        with pytest.raises(QboMcpError, match="status=504"):
            client.report(registry["acme_main"], "TransactionList", {"start_date": "2026-01-01"})
    assert len(calls) == 1
    assert no_sleep == []


def test_a_5xx_other_than_504_on_a_report_is_retried(
    settings, seeded_store, registry, no_sleep
) -> None:
    handler, calls = _sequence(502, 200)
    with _mock_client(settings, seeded_store, handler) as client:
        client.report(registry["acme_main"], "TransactionList", {"start_date": "2026-01-01"})
    assert len(calls) == 2


def test_a_read_timeout_on_a_report_is_not_retried(
    settings, seeded_store, registry, no_sleep
) -> None:
    handler, calls = _sequence(httpx.ReadTimeout("slow"), 200)
    with _mock_client(settings, seeded_store, handler) as client:
        with pytest.raises(QboMcpError, match="not retried"):
            client.report(registry["acme_main"], "TransactionList", {"start_date": "2026-01-01"})
    assert len(calls) == 1


def test_a_read_timeout_on_a_non_report_read_is_retried(
    settings, seeded_store, registry, no_sleep
) -> None:
    handler, calls = _sequence(httpx.ReadTimeout("slow"), 200)
    with _mock_client(settings, seeded_store, handler) as client:
        client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 403, 404])
def test_a_4xx_is_never_retried(settings, seeded_store, registry, no_sleep, status: int) -> None:
    handler, calls = _sequence(status, 200)
    with _mock_client(settings, seeded_store, handler) as client:
        with pytest.raises(QboMcpError):
            client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})
    assert len(calls) == 1


def test_a_429_still_fails_fast(settings, seeded_store, registry, no_sleep) -> None:
    """A documented choice (limits.py): a 429 can mean the monthly quota is
    gone, which no in-call wait replenishes."""
    handler, calls = _sequence(429, 200)
    with _mock_client(settings, seeded_store, handler) as client:
        with pytest.raises(QboMcpError, match="429"):
            client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})
    assert len(calls) == 1


# --------------------------------------------------------------------------
# The whole-call deadline
# --------------------------------------------------------------------------


@pytest.fixture
def short_budget(monkeypatch: pytest.MonkeyPatch):
    def install(seconds: float, reserve: float = 0.0) -> None:
        monkeypatch.setattr(constants, "PER_CALL_BUDGET_SECONDS", seconds)
        monkeypatch.setattr(constants, "SERIALIZATION_RESERVE_SECONDS", reserve, raising=False)
    return install


def test_the_budget_spans_every_page_of_a_walk(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, short_budget,
    one_row_pages,
) -> None:
    """Each page is quick; together they are not. Per-request timeouts never
    notice that -- only a deadline over the whole call does. Twelve vendors a
    row at a time is fourteen requests: a count, twelve pages, one empty."""
    server = _server(mock_settings, registry, credentials, actor)
    mock_intuit.tables["Vendor"] = _vendors(12)
    anyio.run(lambda: server.call_tool("list_vendors", {"entity": "acme_main"}))  # warm the guard

    short_budget(1.0)
    mock_intuit.delay_seconds = 0.25
    before = len(mock_intuit.requests)
    with pytest.raises(ToolError, match="budget"):
        anyio.run(lambda: server.call_tool("list_vendors", {"entity": "acme_main"}))
    assert len(mock_intuit.requests) - before < 14, "the walk ran to the end past its budget"


def test_one_slow_request_is_cut_off_at_the_budget_not_at_120_seconds(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, short_budget
) -> None:
    server = _server(mock_settings, registry, credentials, actor)
    anyio.run(lambda: server.call_tool("list_vendors", {"entity": "acme_main"}))  # warm the guard

    short_budget(1.0)
    mock_intuit.delay_seconds = 4.0
    started = time.monotonic()
    with pytest.raises(ToolError, match="budget"):
        anyio.run(lambda: server.call_tool("list_vendors", {"entity": "acme_main"}))
    assert time.monotonic() - started < 3.0


def test_the_serialization_reserve_is_held_back(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, short_budget
) -> None:
    """A budget no larger than the reserve leaves nothing for requests: the
    tool refuses before sending anything rather than finishing too late to
    answer."""
    server = _server(mock_settings, registry, credentials, actor)
    short_budget(2.0, reserve=2.0)
    with pytest.raises(ToolError, match="budget"):
        anyio.run(lambda: server.call_tool("list_vendors", {"entity": "acme_main"}))
    assert mock_intuit.requests == []


def test_a_refresh_is_not_started_that_the_budget_cannot_finish(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit,
    short_budget, patch_refresh
) -> None:
    """Credential persistence is the one thing the deadline must never cut
    short: a refresh abandoned after the POST loses the pair Intuit issued. So
    a refresh that could not finish inside the budget is not begun."""
    server = _server(mock_settings, registry, credentials, actor, access_seconds=-60)
    submitted = patch_refresh(lambda n, kw: make_token_response(access_token=f"a{n}"))
    short_budget(10.0)
    with pytest.raises(ToolError, match="budget"):
        anyio.run(lambda: server.call_tool("list_vendors", {"entity": "acme_main"}))
    assert submitted == []


@pytest.mark.parametrize(
    "method", ["refresh", "on_unauthorized"], ids=["an expired access token", "a 401"]
)
def test_a_refresh_is_not_submitted_once_the_lock_wait_has_spent_the_budget(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit,
    short_budget, patch_refresh, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """Enough budget when the call decides to refresh, not once it holds the
    lock: another process took the lock in between and kept it for two and a
    half seconds. The budget is checked again under the lock, as the last step
    before the token is submitted, so the refresh is not begun on either path
    to one."""
    import threading

    from qbo_multicompany_mcp.store import TokenStore

    if method == "refresh":
        server = _server(mock_settings, registry, credentials, actor, access_seconds=-60)
    else:
        server = _server(mock_settings, registry, credentials, actor)
        mock_intuit.unauthorized_tokens.add("access-1")
    submitted = patch_refresh(lambda n, kw: make_token_response(access_token=f"a{n}"))
    real = getattr(TokenStore, method)
    holders: list[tuple[threading.Thread, threading.Event]] = []

    def contended(self: TokenStore, *args, **kwargs):  # noqa: ANN002, ANN003
        held, release = threading.Event(), threading.Event()

        def holder() -> None:
            with self.lock(timeout=5.0):
                held.set()
                release.wait(2.5)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5.0)
        holders.append((thread, release))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(TokenStore, method, contended)
    try:
        # A refresh needs 35 seconds: 36.5 is enough when the call decides to
        # refresh, and about 34 once it holds the lock.
        short_budget(36.5)
        with pytest.raises(ToolError, match="budget"):
            anyio.run(lambda: server.call_tool("list_vendors", {"entity": "acme_main"}))
    finally:
        for thread, release in holders:
            release.set()
            thread.join()
    assert len(holders) == 1, "the lock was not contended where the race is"
    assert submitted == []


def test_a_rate_wait_longer_than_the_budget_is_not_waited_out(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, short_budget
) -> None:
    """The short window full: the next request must wait about a second. With
    half a second left, the call refuses instead of sleeping through it."""
    from qbo_multicompany_mcp.limits import SHORT_WINDOW_MAX, RateLimiter

    server = _server(mock_settings, registry, credentials, actor)
    anyio.run(lambda: server.call_tool("list_vendors", {"entity": "acme_main"}))  # warm the guard
    # Fill the short window in one stroke, so every stamp is "now" and the next
    # request's wait is close to the whole second.
    limiter = RateLimiter(mock_settings.token_store_dir, environment="sandbox")
    with limiter._locked(timeout=5.0):
        limiter._write({"acme_main": {"calls": [time.time()] * SHORT_WINDOW_MAX}})

    short_budget(0.5)
    before = len(mock_intuit.requests)
    started = time.monotonic()
    with pytest.raises(ToolError):
        anyio.run(lambda: server.call_tool("list_vendors", {"entity": "acme_main"}))
    assert time.monotonic() - started < 0.9
    assert len(mock_intuit.requests) == before


def test_caller_cancellation_stops_the_business_reads(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, one_row_pages
) -> None:
    """The SDK runs a synchronous tool on a worker thread and waits for it when
    the caller cancels. Without a checkpoint the thread walks on to the last
    page for a caller who has already gone."""
    from qbo_multicompany_mcp.query_grammar import parse
    from qbo_multicompany_mcp.session import QboSession
    from qbo_multicompany_mcp.tools import tool_errors

    _server(mock_settings, registry, credentials, actor)
    session = QboSession(mock_settings, registry)
    entity = registry["acme_main"]
    session.ensure_ready(entity)
    mock_intuit.tables["Vendor"] = _vendors(10)
    mock_intuit.delay_seconds = 0.2
    before = len(mock_intuit.requests)
    finished: list[bool] = []

    def walk() -> None:
        try:
            with tool_errors():
                session.walk(entity, parse("SELECT * FROM Vendor WHERE Active IN (true, false)"))
            finished.append(True)
        except ToolError:
            pass

    async def main() -> None:
        with anyio.move_on_after(0.5):
            await anyio.to_thread.run_sync(walk)

    anyio.run(main)
    sent = len(mock_intuit.requests) - before
    # A count and eleven pages if nothing stops it.
    assert sent < 12, f"{sent} requests: the walk did not stop when its caller left"
    assert finished == [], "the walk completed for a caller who had gone"


def test_no_deadline_outside_a_tool_call(settings, seeded_store, registry) -> None:
    """The CLI and the keepalive are not tool calls; they keep their own
    per-request timeouts and are not cut off by a budget meant for tools."""
    from qbo_multicompany_mcp import deadline

    assert deadline.current() is None
    with _mock_client(settings, seeded_store, _answer()) as client:
        client.get(registry["acme_main"], "query", params={"query": "SELECT * FROM Vendor"})


def test_a_store_lock_wait_is_bounded_by_the_budget(
    mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, short_budget
) -> None:
    """Another holder keeps the canonical lock for three seconds. A call with
    one second left does not queue behind it for the lock's own 35."""
    import threading

    server = _server(mock_settings, registry, credentials, actor)
    store = mock_settings.open_store(credentials=credentials)
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with store.lock():
            held.set()
            release.wait(3.0)

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(5.0)
    try:
        short_budget(1.0)
        started = time.monotonic()
        with pytest.raises(ToolError):
            anyio.run(lambda: server.call_tool("list_vendors", {"entity": "acme_main"}))
        assert time.monotonic() - started < 2.5
    finally:
        release.set()
        thread.join()


def test_the_two_lock_waits_share_one_timeout(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-process mutex and the file lock are waited for in turn. Each with
    the whole timeout, a caller that bounded it by its budget could wait twice
    as long as the budget allows."""
    import threading

    from qbo_multicompany_mcp import store as store_mod

    lock_path = tmp_path / "tokens.lock"
    given: list[float] = []
    real = store_mod._flock_exclusive

    def recording(path, timeout: float) -> int:
        given.append(timeout)
        return real(path, timeout)

    monkeypatch.setattr(store_mod, "_flock_exclusive", recording)
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with store_mod.canonical_lock(lock_path, timeout=5.0):
            held.set()
            release.wait(5.0)

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(5.0)
    given.clear()
    timer = threading.Timer(1.0, release.set)
    timer.start()
    try:
        # The mutex is held for about a second, so the file lock gets the rest.
        with store_mod.canonical_lock(lock_path, timeout=3.0):
            pass
    finally:
        release.set()
        timer.cancel()
        thread.join()
    assert len(given) == 1
    assert given[0] <= 3.0 - 0.9, given


# --------------------------------------------------------------------------
# The deadline bounds every blocking read
# --------------------------------------------------------------------------


class _SplitResponseServer:
    """A loopback origin that answers in two parts: headers and one byte after
    ``first_after`` seconds, the rest ``rest_after`` seconds later. Each part
    arrives inside the request's own read timeout; only the whole exceeds the
    call's budget."""

    def __init__(self, *, first_after: float, rest_after: float) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        body = b'{"Preferences": {}}'
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                time.sleep(first_after)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[:1])
                self.wfile.flush()
                time.sleep(rest_after)
                self.wfile.write(body[1:])
                self.wfile.flush()

            def log_message(self, fmt: str, *args: object) -> None:
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_a_split_response_cannot_outlive_the_deadline(
    settings, credentials, registry, actor, short_budget
) -> None:
    """A 0.7-second budget, headers and one byte after 0.45 seconds, the last
    byte 0.6 seconds later. Each socket read fits the initial timeout, so a
    deadline checked only between reads fires after the overdue read returns,
    at 1.06 seconds. Each blocking read is bounded by what is left of the
    budget, so the call ends at the deadline."""
    from dataclasses import replace

    from qbo_multicompany_mcp.deadline import call_deadline
    from qbo_multicompany_mcp.errors import DeadlineExceeded

    origin = _SplitResponseServer(first_after=0.45, rest_after=0.6)
    try:
        store = settings.open_store(credentials=credentials)
        store.commit_consent(
            entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
        )
        pointed = replace(settings, api_base=origin.base_url)
        short_budget(0.7)
        started = time.monotonic()
        with call_deadline(), AccountingClient(pointed, store) as client:
            with pytest.raises(DeadlineExceeded):
                client.get(registry["acme_main"], "preferences")
        elapsed = time.monotonic() - started
    finally:
        origin.stop()
    assert elapsed < 0.9, f"the deadline error arrived after {elapsed:.2f}s"


def test_a_call_that_finishes_after_its_deadline_is_refused_at_the_end(short_budget) -> None:
    """The end-of-call checkpoint: work done after the budget ran out -- CPU,
    serialisation, anything that consulted no checkpoint -- does not produce
    a result as if it had been in time."""
    from qbo_multicompany_mcp.tools import tool_errors

    short_budget(0.2)
    with pytest.raises(ToolError, match="budget"):
        with tool_errors():
            time.sleep(0.3)


def test_a_call_that_finishes_inside_its_deadline_is_not_refused(short_budget) -> None:
    """The control for the checkpoint above."""
    from qbo_multicompany_mcp.tools import tool_errors

    short_budget(1.0)
    with tool_errors():
        time.sleep(0.05)
