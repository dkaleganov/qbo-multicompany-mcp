"""Complete-or-loud paging for query walks.

A paged read that silently returns 900 of 1,000 rows is worse than one that
fails: the caller gets a plausible answer and no signal. So every walk is
anchored.

The anchor is a ``COUNT(*)`` carrying **the same WHERE** as the walk. Pages are
taken at 1,000, the documented maximum (Intuit's query syntax page), and the
walk stops on a short page. At the end, the number of **unique ids after
deduplication** must equal the anchor.

Unique ids, because a raw row count can hide a problem. If one
row is duplicated across a page boundary while another is skipped, the raw
count still matches and a length check passes while the result is wrong in two
directions at once. Counting distinct identities catches exactly that.

**What this does not give you is a snapshot.** Nothing in the QuickBooks query
API offers one. Count equality detects some omissions; it cannot detect
someone editing an amount between page two and page three, because that
changes no cardinality at all. Every walk therefore reports
``walk_started_at``, ``walk_finished_at`` and ``consistency: non_snapshot``, so
the limitation is disclosed in the result rather than left for a reader to
assume away. A walk is a best-effort read of a moving system.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from .errors import QboMcpError
from .query_grammar import MAX_RESULTS_CEILING, ParsedQuery

#: At 1,000 rows a page this is 100,000 rows. Past that, something is wrong
#: with the filter rather than with the walk.
MAX_PAGES: Final = 100

CONSISTENCY_NON_SNAPSHOT: Final = "non_snapshot"


class WalkIncomplete(QboMcpError):
    """The walk could not be shown to be complete."""


class WalkConflict(QboMcpError):
    """Two different versions of one row appeared during the walk."""


@dataclass(slots=True)
class WalkResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    count_anchor: int = 0
    unique_ids: int = 0
    duplicates_dropped: int = 0
    pages_walked: int = 0
    truncated: bool = False
    walk_started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    walk_finished_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    consistency: str = CONSISTENCY_NON_SNAPSHOT


def rows_of(payload: Mapping[str, Any], entity: str) -> list[dict[str, Any]]:
    """Extract rows for ``entity`` from a QueryResponse, failing closed.

    An unexpected entity key in the response is an error, not something to
    skip. A newly served type needs a projection before any query or change
    route can reach it, and quietly ignoring an unknown key is how an
    unreviewed payload reaches a model.
    """
    response = payload.get("QueryResponse")
    if not isinstance(response, Mapping):
        raise WalkIncomplete("the query response had no QueryResponse object")

    known_scalars = {
        "startPosition",
        "maxResults",
        "totalCount",
    }
    for key, value in response.items():
        if key in known_scalars:
            continue
        if key != entity and isinstance(value, list):
            raise WalkIncomplete(
                "the query response carried rows for an unexpected type. This "
                "server fails closed on that: a type needs a reviewed "
                "projection before any route can return it."
            )

    rows = response.get(entity)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise WalkIncomplete("the query response rows were not a list")
    return [row for row in rows if isinstance(row, dict)]


def count_of(payload: Mapping[str, Any]) -> int:
    response = payload.get("QueryResponse")
    if not isinstance(response, Mapping):
        raise WalkIncomplete("the count response had no QueryResponse object")
    total = response.get("totalCount")
    if not isinstance(total, int):
        raise WalkIncomplete("the count response carried no totalCount")
    return total


def _identity(row: Mapping[str, Any]) -> str | None:
    value = row.get("Id")
    return str(value) if value is not None else None


def _fingerprint(row: Mapping[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, default=str)


def walk(
    query: ParsedQuery,
    *,
    fetch: Callable[[ParsedQuery], Mapping[str, Any]],
    page_size: int | None = None,
    max_pages: int | None = None,
    client_limit: int | None = None,
) -> WalkResult:
    """Page ``query`` to completion, or raise.

    ``client_limit`` trims the returned rows *after* the completeness check, so
    a caller asking for 10 rows still learns whether the underlying walk was
    sound. Trimming before the check would make every limited call trivially
    "complete".

    ``page_size`` and ``max_pages`` default to the module constants, read here
    rather than bound into the signature, so a test can reach the ceiling
    without serving a hundred thousand rows to get to it. An untested ceiling
    is how a hand-rolled loop runs out of pages in silence.
    """
    page_size = MAX_RESULTS_CEILING if page_size is None else page_size
    max_pages = MAX_PAGES if max_pages is None else max_pages
    started = datetime.now(UTC)
    anchor = count_of(fetch(query.as_count()))

    seen: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    duplicates = 0
    pages = 0
    position = 1

    while pages < max_pages:
        page = fetch(query.with_paging(start_position=position, max_results=page_size))
        pages += 1
        page_rows = rows_of(page, query.entity)

        for row in page_rows:
            identity = _identity(row)
            if identity is None:
                raise WalkIncomplete("a row arrived without an Id; cannot verify the walk")
            fingerprint = _fingerprint(row)
            if identity in seen:
                if seen[identity] != fingerprint:
                    # Two different versions of one row: somebody edited it
                    # mid-walk. Returning either version would be a guess.
                    raise WalkConflict(
                        "a row changed while the list was being read, so the "
                        "result cannot be shown to be consistent. Retry."
                    )
                duplicates += 1
                continue
            seen[identity] = fingerprint
            rows.append(row)

        if len(page_rows) < page_size:
            break
        position += len(page_rows)
    else:
        raise WalkIncomplete(
            f"the walk exceeded {max_pages} pages. Narrow the filter."
        )

    finished = datetime.now(UTC)

    if len(seen) != anchor:
        raise WalkIncomplete(
            f"the walk returned {len(seen)} distinct rows but the count said "
            f"{anchor}. The list may be incomplete, so it is not being returned. "
            f"This usually means rows were added or removed while it was read; "
            f"retry."
        )

    truncated = False
    if client_limit is not None and len(rows) > client_limit:
        rows = rows[:client_limit]
        truncated = True

    return WalkResult(
        rows=rows,
        count_anchor=anchor,
        unique_ids=len(seen),
        duplicates_dropped=duplicates,
        pages_walked=pages,
        truncated=truncated,
        walk_started_at=started,
        walk_finished_at=finished,
        consistency=CONSISTENCY_NON_SNAPSHOT,
    )


def dedupe_by_source_and_id(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
) -> tuple[list[tuple[str, Mapping[str, Any]]], int]:
    """Deduplicate ``(source_type, row)`` pairs by source and id.

    Used where several walks are combined -- the vendor-payment sources, for
    instance. Identity is the pair, because a Purchase and a BillPayment may
    legitimately share an ``Id``: they are different tables.
    """
    seen: dict[tuple[str, str], str] = {}
    out: list[tuple[str, Mapping[str, Any]]] = []
    dropped = 0
    for source, row in entries:
        identity = _identity(row)
        if identity is None:
            continue
        key = (source, identity)
        fingerprint = _fingerprint(row)
        if key in seen:
            if seen[key] != fingerprint:
                raise WalkConflict(
                    "two different versions of one record appeared while "
                    "combining sources. Retry."
                )
            dropped += 1
            continue
        seen[key] = fingerprint
        out.append((source, row))
    return out, dropped
