"""Proves the walk is complete-or-loud, and that the rate limiter bounds a
burst rather than a concurrency level."""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from qbo_multicompany_mcp.limits import (
    MIN_THROTTLE_WAIT_SECONDS,
    THROTTLED_TEXT,
    RateLimited,
    RateLimiter,
    UpstreamThrottled,
    Window,
    parse_retry_after,
)
from qbo_multicompany_mcp.pagination import (
    CONSISTENCY_NON_SNAPSHOT,
    WalkConflict,
    WalkIncomplete,
    dedupe_by_source_and_id,
    walk,
)
from qbo_multicompany_mcp.query_grammar import parse

QUERY = parse("SELECT * FROM Vendor")


def fetcher(rows, count, *, entity="Vendor"):
    def fetch(q):
        if q.count_only:
            return {"QueryResponse": {"totalCount": count}}
        start = q.start_position or 1
        size = q.max_results or 1000
        return {"QueryResponse": {entity: rows[start - 1 : start - 1 + size]}}

    return fetch


def make_rows(n: int) -> list[dict]:
    return [{"Id": str(i), "DisplayName": f"V{i}"} for i in range(1, n + 1)]


# --------------------------------------------------------------------------
# Completeness
# --------------------------------------------------------------------------


def test_a_complete_walk_reports_its_anchor() -> None:
    rows = make_rows(5)
    result = walk(QUERY, fetch=fetcher(rows, 5), page_size=2)
    assert result.unique_ids == 5
    assert result.count_anchor == 5
    assert result.pages_walked == 3
    assert result.duplicates_dropped == 0


def test_unique_ids_not_raw_count_is_what_is_compared() -> None:
    """One row duplicated and another skipped: the raw count still matches, so
    a length check would pass while the result is wrong in two directions."""
    rows = make_rows(5)
    broken = [rows[0], rows[0], rows[2], rows[3], rows[4]]
    assert len(broken) == 5  # a raw length check would be satisfied
    with pytest.raises(WalkIncomplete, match="4 distinct rows but the count said 5"):
        walk(QUERY, fetch=fetcher(broken, 5), page_size=2)


def test_a_duplicate_across_a_page_boundary_is_counted_and_dropped() -> None:
    rows = make_rows(4)
    with_dupe = [rows[0], rows[1], rows[1], rows[2], rows[3]]
    result = walk(QUERY, fetch=fetcher(with_dupe, 4), page_size=3)
    assert result.unique_ids == 4
    assert result.duplicates_dropped == 1


def test_two_versions_of_one_row_fail_rather_than_pick_one() -> None:
    rows = make_rows(3)
    conflicting = [rows[0], {"Id": "1", "DisplayName": "EDITED"}, rows[1], rows[2]]
    with pytest.raises(WalkConflict, match="changed while the list was being read"):
        walk(QUERY, fetch=fetcher(conflicting, 3), page_size=2)


def test_a_row_without_an_id_cannot_be_verified() -> None:
    with pytest.raises(WalkIncomplete, match="without an Id"):
        walk(QUERY, fetch=fetcher([{"DisplayName": "nameless"}], 1))


def test_an_unexpected_type_in_the_response_fails_closed() -> None:
    def rogue(q):
        if q.count_only:
            return {"QueryResponse": {"totalCount": 1}}
        return {"QueryResponse": {"Vendor": make_rows(1), "Employee": [{"Id": "9"}]}}

    with pytest.raises(WalkIncomplete, match="unexpected type"):
        walk(QUERY, fetch=rogue)


def test_the_page_cap_is_enforced() -> None:
    rows = make_rows(50)
    with pytest.raises(WalkIncomplete, match="exceeded 5 pages"):
        walk(QUERY, fetch=fetcher(rows, 50), page_size=2, max_pages=5)


# --------------------------------------------------------------------------
# The limitation that cannot be fixed, only disclosed
# --------------------------------------------------------------------------


def test_an_equal_cardinality_mutation_is_not_detected_but_is_disclosed() -> None:
    """An amount edited between pages changes no cardinality.

    Count equality cannot see it. The walk therefore does not claim a snapshot:
    it reports its window and says so in every result.
    """
    rows = make_rows(4)
    state = {"calls": 0}

    def mutating_fetch(q):
        if q.count_only:
            return {"QueryResponse": {"totalCount": 4}}
        state["calls"] += 1
        page = [dict(row) for row in rows[(q.start_position or 1) - 1 :][: q.max_results or 1000]]
        if state["calls"] > 1:
            # Same ids, same count, different money.
            for row in page:
                row["Balance"] = 999.99
        return {"QueryResponse": {"Vendor": page}}

    result = walk(QUERY, fetch=mutating_fetch, page_size=2)

    assert result.unique_ids == result.count_anchor == 4  # undetected, as expected
    assert result.consistency == CONSISTENCY_NON_SNAPSHOT
    assert result.walk_finished_at >= result.walk_started_at
    balances = {row.get("Balance") for row in result.rows}
    assert balances == {None, 999.99}, "the inconsistency really is present"


def test_client_trimming_happens_after_the_completeness_check() -> None:
    """Trimming first would make every limited call trivially complete."""
    rows = make_rows(10)
    result = walk(QUERY, fetch=fetcher(rows, 10), page_size=4, client_limit=3)
    assert result.unique_ids == 10  # the whole walk was verified
    assert len(result.rows) == 3
    assert result.truncated is True


# --------------------------------------------------------------------------
# Combining sources
# --------------------------------------------------------------------------


def test_dedupe_is_by_source_and_id_together() -> None:
    """A Purchase and a BillPayment may legitimately share an Id: different
    tables."""
    entries = [
        ("Purchase", {"Id": "1", "TotalAmt": 10}),
        ("BillPayment", {"Id": "1", "TotalAmt": 20}),
        ("Purchase", {"Id": "1", "TotalAmt": 10}),
    ]
    kept, dropped = dedupe_by_source_and_id(entries)
    assert len(kept) == 2
    assert dropped == 1


def test_conflicting_versions_across_sources_fail() -> None:
    entries = [
        ("Purchase", {"Id": "1", "TotalAmt": 10}),
        ("Purchase", {"Id": "1", "TotalAmt": 99}),
    ]
    with pytest.raises(WalkConflict):
        dedupe_by_source_and_id(entries)


# --------------------------------------------------------------------------
# Rate limits
# --------------------------------------------------------------------------


@pytest.fixture
def limiter_dir():
    return pathlib.Path(tempfile.mkdtemp())


def fake_clock():
    now = [1000.0]
    slept: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    return clock, sleep, slept, now


def test_the_short_window_bounds_a_burst_not_a_concurrency_level(limiter_dir) -> None:
    """Ten in flight is not the limit; ten per second is. A semaphore would
    allow ten, see them finish in 50ms, and allow ten more."""
    clock, sleep, slept, _ = fake_clock()
    limiter = RateLimiter(
        limiter_dir, environment="sandbox",
        windows=(Window(1.0, 10),), clock=clock, sleep=sleep,
    )
    for _ in range(10):
        limiter.acquire("acme_main")
    assert slept == []
    limiter.acquire("acme_main")
    assert slept, "the eleventh call in the same second must wait"


def test_the_minute_window_refuses_rather_than_sleeping_past_its_budget(
    limiter_dir,
) -> None:
    """Enforcement, not patience.

    A minute window with no headroom would mean sleeping most of a minute
    inside a tool call. The limiter refuses instead, and says nothing was sent
    -- which is a transient, local, retryable answer rather than a call that
    hangs and then does the work anyway.
    """
    clock, sleep, slept, _ = fake_clock()
    limiter = RateLimiter(
        limiter_dir, environment="sandbox",
        windows=(Window(60.0, 5),), clock=clock, sleep=sleep,
    )
    for _ in range(5):
        limiter.acquire("acme_main")
    assert slept == []
    with pytest.raises(RateLimited, match="Nothing was sent"):
        limiter.acquire("acme_main")


def test_the_minute_window_waits_when_there_is_budget_for_it(limiter_dir) -> None:
    clock, sleep, slept, _ = fake_clock()
    limiter = RateLimiter(
        limiter_dir, environment="sandbox",
        windows=(Window(60.0, 5),), clock=clock, sleep=sleep,
    )
    for _ in range(5):
        limiter.acquire("acme_main")
    limiter.acquire("acme_main", timeout=120.0)
    assert sum(slept) >= 60.0


def test_each_wait_for_the_shared_file_gets_only_what_the_timeout_has_left(limiter_dir) -> None:
    """A full window makes acquire sleep and look again, and every look waits
    for the file's lock. With the whole timeout each time, a contended file
    could keep the call long past it."""
    clock, sleep, _, _ = fake_clock()
    limiter = RateLimiter(
        limiter_dir, environment="sandbox",
        windows=(Window(1.0, 2),), clock=clock, sleep=sleep,
    )
    limiter.acquire("acme_main")
    limiter.acquire("acme_main")
    given: list[tuple[float, float]] = []
    real = limiter._locked

    def recording(timeout: float):
        given.append((clock(), timeout))
        return real(timeout)

    limiter._locked = recording
    start = clock()
    limiter.acquire("acme_main", timeout=5.0)
    assert len(given) >= 2, "the full window should have made it look twice"
    for at, timeout in given:
        assert timeout <= start + 5.0 - at + 1e-9, (at - start, timeout)


def test_windows_are_per_realm(limiter_dir) -> None:
    clock, sleep, slept, _ = fake_clock()
    limiter = RateLimiter(
        limiter_dir, environment="sandbox",
        windows=(Window(1.0, 2),), clock=clock, sleep=sleep,
    )
    limiter.acquire("acme_main")
    limiter.acquire("acme_main")
    limiter.acquire("acme_ops")
    assert slept == [], "one company's burst must not throttle another"


def test_the_window_state_is_shared_across_limiter_instances(limiter_dir) -> None:
    """Three processes each politely staying under the limit is three times the
    limit at Intuit."""
    clock, sleep, slept, _ = fake_clock()
    first = RateLimiter(
        limiter_dir, environment="sandbox",
        windows=(Window(1.0, 2),), clock=clock, sleep=sleep,
    )
    second = RateLimiter(
        limiter_dir, environment="sandbox",
        windows=(Window(1.0, 2),), clock=clock, sleep=sleep,
    )
    first.acquire("acme_main")
    second.acquire("acme_main")
    second.acquire("acme_main")
    assert slept, "the second process must see the first process's calls"


def test_a_429_blocks_for_at_least_sixty_seconds(limiter_dir) -> None:
    clock, sleep, _, _ = fake_clock()
    limiter = RateLimiter(limiter_dir, environment="sandbox", clock=clock, sleep=sleep)
    assert limiter.note_throttled("acme_main") == MIN_THROTTLE_WAIT_SECONDS
    assert limiter.note_throttled("acme_ops", retry_after="5") == (
        MIN_THROTTLE_WAIT_SECONDS
    ), "a shorter Retry-After does not shorten the minimum"
    assert limiter.note_throttled("acme_other", retry_after="300") == 300.0


def test_a_blocked_realm_fails_fast_with_the_quota_wording(limiter_dir) -> None:
    """A 429 can mean the monthly quota is gone, which waiting does not
    replenish -- so no reset time is invented."""
    clock, sleep, _, _ = fake_clock()
    limiter = RateLimiter(limiter_dir, environment="sandbox", clock=clock, sleep=sleep)
    limiter.note_throttled("acme_main")
    with pytest.raises(UpstreamThrottled) as caught:
        limiter.acquire("acme_main")
    text = str(caught.value)
    assert "monthly quota" in text
    assert "at least 60 seconds" in text
    assert text == THROTTLED_TEXT


def test_a_block_expires(limiter_dir) -> None:
    clock, sleep, _, now = fake_clock()
    limiter = RateLimiter(limiter_dir, environment="sandbox", clock=clock, sleep=sleep)
    limiter.note_throttled("acme_main")
    now[0] += MIN_THROTTLE_WAIT_SECONDS + 1
    limiter.acquire("acme_main")


@pytest.mark.parametrize(
    ("header", "expected"),
    [("30", 30.0), ("0", 0.0), ("-5", None), ("nonsense", None), (None, None), ("", None)],
)
def test_retry_after_parsing(header, expected) -> None:
    assert parse_retry_after(header, now=0.0) == expected


def test_an_http_date_retry_after_is_understood() -> None:
    from email.utils import formatdate

    value = formatdate(timeval=1000.0 + 120, usegmt=True)
    parsed = parse_retry_after(value, now=1000.0)
    assert parsed is not None and 110 <= parsed <= 130


def test_acquire_gives_up_rather_than_waiting_past_its_budget(limiter_dir) -> None:
    clock, sleep, _, _ = fake_clock()
    limiter = RateLimiter(
        limiter_dir, environment="sandbox",
        windows=(Window(60.0, 1),), clock=clock, sleep=sleep,
    )
    limiter.acquire("acme_main")
    with pytest.raises(RateLimited, match="Nothing was sent"):
        limiter.acquire("acme_main", timeout=1.0)


def test_the_limiter_creates_nothing_until_it_is_used(limiter_dir) -> None:
    unused = limiter_dir / "never"
    limiter = RateLimiter(unused, environment="sandbox")
    assert not unused.exists()
    assert limiter.blocked_for("acme_main") == 0.0


# --------------------------------------------------------------------------
# list_accounts is a walk, not a hand-rolled loop
# --------------------------------------------------------------------------
#
# A hand-rolled loop -- with `truncated=False` and `duplicates_dropped=0` written
# into the result as literals, and a page ceiling that ends the loop without
# saying so -- reports 1,001 rows from 1,000 distinct accounts as a complete list
# with zero duplicates. So the contract
# is: deduplicate by source type and id, and a full analytic walk either meets
# its completion conditions or fails. These rows carry money, so a silently
# repeated row is a silently wrong total.


def _tool_server(mock_settings, registry, credentials, actor):
    import anyio
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    from .conftest import REALM_MAIN, make_token_response

    store = mock_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(), actor=actor,
    )
    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    return lambda name, args: anyio.run(lambda: server.call_tool(name, args))


def _accounts(n: int, *, first: int = 1) -> list[dict]:
    return [
        {
            "Id": str(i),
            "Name": f"Account {i}",
            "AccountType": "Expense",
            "CurrentBalance": 1.0,
            "CurrentBalanceWithSubAccounts": 1.0,
        }
        for i in range(first, first + n)
    ]


def test_list_accounts_drops_a_row_that_moved_across_a_page_boundary(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """A page boundary that moves between requests repeats an account.

    1,000 accounts read at the documented page size of 1,000. Between the first
    page and the second somebody adds an account at the front, everything
    shifts down one, and the row that was last on page one arrives again as the
    whole of page two. Summing `CurrentBalance` over the returned rows is what
    a caller does next, so a row counted twice is money counted twice.
    """
    mock_intuit.accounts = _accounts(1000)
    original = mock_intuit.query_payload
    pages = {"served": 0}

    def shifting(statement: str) -> dict:
        payload = original(statement)
        if "COUNT(*)" not in statement.upper().replace(" ", ""):
            pages["served"] += 1
            if pages["served"] == 1:
                mock_intuit.accounts.insert(0, _accounts(1, first=2001)[0])
        return payload

    mock_intuit.query_payload = shifting

    call = _tool_server(mock_settings, registry, credentials, actor)
    result = call("list_accounts", {"entity": "acme_main"})
    assert result.is_error is False, result.content

    body = result.structured_content
    ids = [row["id"] for row in body["rows"]]
    assert len(ids) == len(set(ids)), "the same account was returned twice"
    assert body["count"] == len(body["rows"])
    assert body["duplicates_dropped"] == 1, (
        "the repeat is reported, not hidden: a non-zero count here is how a "
        "paging problem shows itself"
    )
    assert body["truncated"] is False


def test_list_accounts_fails_rather_than_ending_at_the_page_cap(
    mock_settings, registry, credentials, actor, mock_intuit, monkeypatch
) -> None:
    """Running out of pages is not a complete list.

    The page size and the cap are pulled down so this does not have to serve a
    hundred thousand rows to reach the ceiling; the behaviour under test is
    what happens at it.
    """
    from qbo_multicompany_mcp import pagination

    monkeypatch.setattr(pagination, "MAX_RESULTS_CEILING", 2)
    monkeypatch.setattr(pagination, "MAX_PAGES", 3)

    mock_intuit.accounts = _accounts(20)
    call = _tool_server(mock_settings, registry, credentials, actor)

    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as caught:
        call("list_accounts", {"entity": "acme_main"})
    assert "pages" in str(caught.value)


def test_list_accounts_discloses_its_walk_window(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """Every walk reports its window and that it is not a snapshot. Count
    equality cannot see a balance edited between two pages, so the limitation
    is stated in the result rather than left to be assumed away.
    """
    mock_intuit.accounts = _accounts(3)
    call = _tool_server(mock_settings, registry, credentials, actor)
    body = call("list_accounts", {"entity": "acme_main"}).structured_content

    assert body["consistency"] == CONSISTENCY_NON_SNAPSHOT
    assert body["walk_started_at"] <= body["walk_finished_at"]
    assert body["count"] == 3
    assert body["pages_walked"] == 1
