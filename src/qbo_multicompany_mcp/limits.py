"""Request-rate enforcement, across processes.

Intuit documents two limits per company (its API call limits and throttles
page):

* **500 requests per minute** per company, and
* **10 requests per second** per company and app.

The second is *a rate, not a concurrency ceiling*, and the distinction is the
whole reason this module exists. An in-flight semaphore of ten would happily
issue ten requests, see them all complete in 50ms, and issue ten more --
twenty requests in a tenth of a second, comfortably inside any concurrency
bound and comfortably over the rate. Bounding a burst needs a window, not a
count of what is currently outstanding.

It also has to hold *across processes*. This package is explicitly built to be
run as several processes against one company: an MCP server answering a
session, a scheduled keepalive, an operator at the auth CLI. Three processes
each politely staying under ten per second is thirty per second at Intuit.
So the window state lives in a file under its own lock.

That lock is deliberately **not** the canonical token-store lock. The store
lock is held across a token POST; borrowing it here would serialise every
read request in the process behind whatever refresh happened to be running.
Different hold times, different contention, different lock.

**A 429 is not simply "slow down".** It means wait at least 60 seconds (that
page again), longer if a valid ``Retry-After`` says so -- and no
``Retry-After`` header is documented there. It can also mean the app's
*monthly* quota is exhausted, which no amount of sleeping replenishes. So a 429
is reported as a bounded, sanitized throttling-or-quota failure rather than
slept through with an invented reset time.

**The file names no company id.** Intuit asks that a company id be stored
encrypted and not exposed. The token store encrypts its document; this file is
plaintext, so it files each company under its registry entity key. That is
still one bucket per realm in each environment: the registry refuses two keys
sharing a realm, or a key that spells a realm id, and a request carrying a
stored credential is sent only under the key its realm's row is bound to. The
file is written the way the store writes its document, 0600 from creation and
atomically, and a file in any other format -- above all a realm-keyed one -- is
discarded at first contact, not migrated.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterator

from .errors import QboMcpError, StoreError
from .store import _mkdir_private, write_atomic

#: Documented per-company-and-app rate.
SHORT_WINDOW_SECONDS: Final = 1.0
SHORT_WINDOW_MAX: Final = 10

#: Documented per-company rate.
MINUTE_WINDOW_SECONDS: Final = 60.0
MINUTE_WINDOW_MAX: Final = 500

#: A 429 means wait at least this long, whatever any header says.
MIN_THROTTLE_WAIT_SECONDS: Final = 60.0

#: Refuse a Retry-After beyond this; a wildly large value is more likely a
#: malformed header than a real instruction, and a tool call cannot honour it.
MAX_HONOURED_RETRY_AFTER_SECONDS: Final = 3600.0

LIMITS_LOCK_FILENAME: Final = "ratelimit.lock"

#: The state file's format: each company's entry under its entity key, inside
#: ``entities``. Anything else, such as a file keyed by realm id, is discarded.
STATE_FORMAT: Final = 2

#: The state file's mode from creation: private to its user, whatever the umask.
STATE_FILE_MODE: Final = 0o600

#: How long a single acquire() may spend waiting for headroom before giving
#: up. Sized well inside the per-call budget so a queued call still leaves time
#: for the request itself.
DEFAULT_ACQUIRE_TIMEOUT_SECONDS: Final = 30.0

_POLL_SECONDS: Final = 0.02

Clock = Callable[[], float]


class RateLimited(QboMcpError):
    """Local headroom could not be obtained: not in time, or not shown at all,
    because the shared state could not be read or written.

    Local: nothing was sent, and no upstream limit was breached.
    """


class UpstreamThrottled(QboMcpError):
    """Intuit returned 429, or a recorded 429 block is still in force."""


THROTTLED_TEXT: Final = (
    "QuickBooks refused the request: rate limited or monthly quota exhausted "
    "(status=429). Wait at least 60 seconds before retrying. If it persists, "
    "the app's monthly request quota may be exhausted, which waiting does not "
    "replenish -- check the app's usage in the Intuit developer dashboard."
)

STATE_UNAVAILABLE_TEXT: Final = (
    "the shared rate-limit state could not be read or written, so this call "
    "cannot be counted against the request windows. Nothing was sent. Check "
    "that this user can read and write the token store's folder, then retry."
)


@dataclass(frozen=True, slots=True)
class Window:
    seconds: float
    limit: int


DEFAULT_WINDOWS: Final[tuple[Window, ...]] = (
    Window(SHORT_WINDOW_SECONDS, SHORT_WINDOW_MAX),
    Window(MINUTE_WINDOW_SECONDS, MINUTE_WINDOW_MAX),
)


def parse_retry_after(value: str | None, *, now: float) -> float | None:
    """Parse ``Retry-After``: delta-seconds or an HTTP date.

    Returns ``None`` for anything unparseable, which is treated as "no useful
    header" rather than as zero. There is no guaranteed header here, so absence
    is the normal case, not an error.
    """
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - now
    if seconds < 0:
        return None
    return min(seconds, MAX_HONOURED_RETRY_AFTER_SECONDS)


def _entities_of(document: object) -> dict[str, dict[str, Any]]:
    """The entries of a document in the current format, and nothing else.

    A realm-keyed document, an unknown format and corrupt state all yield no
    entries. An entry beside ``entities`` is outside it, so it is never read.
    """
    if not isinstance(document, dict) or document.get("format") != STATE_FORMAT:
        return {}
    section = document.get("entities")
    if not isinstance(section, dict):
        return {}
    return {key: entry for key, entry in section.items() if isinstance(entry, dict)}


def _document_of(entities: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"format": STATE_FORMAT, "entities": entities}


class RateLimiter:
    """Sliding-window logs per company, shared through one file.

    The state file holds, per company, the wall-clock timestamps of recent
    requests plus any 429 block, filed under the company's entity key and
    never its realm id. Wall clock rather than monotonic on purpose:
    monotonic clocks are not comparable between processes, and this state is
    shared between them.
    """

    def __init__(
        self,
        directory: Path,
        *,
        environment: str,
        windows: tuple[Window, ...] = DEFAULT_WINDOWS,
        clock: Clock = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.directory = Path(directory).expanduser()
        self.environment = environment
        self.windows = windows
        self.clock = clock
        self.sleep = sleep

    @property
    def state_path(self) -> Path:
        return self.directory / f"ratelimit.{self.environment}.json"

    @property
    def lock_path(self) -> Path:
        return self.directory / LIMITS_LOCK_FILENAME

    # -- storage -----------------------------------------------------------

    @contextmanager
    def _locked(self, timeout: float) -> Iterator[None]:
        # The token store's folder, created by the store's own routine: 0700
        # whichever of the two reaches it first, whatever the umask. A folder
        # that exists keeps its mode, the operator's.
        _mkdir_private(self.directory)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = self.clock() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if self.clock() >= deadline:
                        raise RateLimited(
                            "could not read the shared rate-limit state in time"
                        ) from exc
                    time.sleep(_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read(self) -> dict[str, dict[str, Any]]:
        """Each company's window state, by entity key. Called under the lock.

        Only the current format is read. Anything else -- above all a
        realm-keyed file -- is discarded, not migrated: it is recent
        call history, the last minute's calls and any 429 block (an hour at
        most), and mapping a realm back to its entity would need the registry,
        which this module does not have. The cost is at most one request that
        rediscovers a block. A file that is not exactly in the
        current format is rewritten at once, so no realm-keyed entry outlives
        the first process that reads it, whether or not it records a call.

        A file that cannot be read is refused, not taken as empty. Empty state
        is permission to send, and every process would read the same nothing.
        """
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            raise RateLimited(STATE_UNAVAILABLE_TEXT) from None
        except ValueError:
            # Not text: corrupt, and rebuilt below like corrupt JSON.
            raw = ""
        try:
            document: object = json.loads(raw)
        except ValueError:
            # Corrupt state is state we rebuild. Being briefly over-permissive
            # once is better than refusing every request until a human looks.
            document = None
        entities = _entities_of(document)
        if document != _document_of(entities):
            self._write(entities)
        return entities

    def _write(self, entities: dict[str, dict[str, Any]]) -> None:
        """Persist the state the way the token store persists its document.

        ``write_atomic``: a temp file created 0600 with ``O_EXCL``, its
        contents fsynced, ``os.replace``, the folder fsynced. The mode is the
        file's from creation, so no umask widens it, and the rename replaces a
        link at the state path rather than following it.
        """
        payload = json.dumps(_document_of(entities)).encode("utf-8")
        try:
            write_atomic(self.state_path, payload, mode=STATE_FILE_MODE)
        except (StoreError, OSError):
            # A call this file cannot record is not sent. Uncounted, it is
            # invisible to every other process's windows, and a write that
            # fails once tends to fail for every call after it.
            raise RateLimited(STATE_UNAVAILABLE_TEXT) from None

    # -- the gate ----------------------------------------------------------

    def acquire(
        self, entity_key: str, *, timeout: float = DEFAULT_ACQUIRE_TIMEOUT_SECONDS
    ) -> None:
        """Block until this company has headroom in every window, then record.

        ``entity_key`` is the company's registry key, never its realm id.
        Never sleeps while holding the lock: every other process would queue
        behind the sleep, turning a 100ms wait into a stampede when it ends.
        Each pass waits for the file only as long as ``timeout`` has left.
        """
        deadline = self.clock() + timeout
        while True:
            with self._locked(timeout=max(0.0, deadline - self.clock())):
                entities = self._read()
                entry = entities.get(entity_key, {})
                now = self.clock()

                blocked_until = float(entry.get("blocked_until") or 0.0)
                if blocked_until > now:
                    raise UpstreamThrottled(THROTTLED_TEXT)

                stamps = [
                    float(t)
                    for t in entry.get("calls", [])
                    if isinstance(t, (int, float))
                ]
                horizon = max(window.seconds for window in self.windows)
                stamps = [t for t in stamps if t > now - horizon]

                wait = self._wait_needed(stamps, now)
                if wait <= 0:
                    stamps.append(now)
                    entry["calls"] = stamps
                    entities[entity_key] = entry
                    self._write(entities)
                    return

            if self.clock() + wait > deadline:
                raise RateLimited(
                    "the local request-rate window has no headroom within the "
                    "time available. Nothing was sent; retry shortly."
                )
            # Outside the lock, on purpose.
            self.sleep(min(wait, 1.0))

    def _wait_needed(self, stamps: list[float], now: float) -> float:
        wait = 0.0
        for window in self.windows:
            recent = sorted(t for t in stamps if t > now - window.seconds)
            if len(recent) >= window.limit:
                # The oldest call in this window leaves it at this moment.
                wait = max(wait, (recent[0] + window.seconds) - now)
        return wait

    # -- 429 ---------------------------------------------------------------

    def note_throttled(self, entity_key: str, retry_after: str | None = None) -> float:
        """Record a 429 and return how long this company is blocked for.

        At least 60 seconds, longer if a valid ``Retry-After`` says so. The
        block makes subsequent calls in every process fail fast with the same
        fixed text, rather than each discovering the 429 for itself and
        spending a request to do it.
        """
        now = self.clock()
        hinted = parse_retry_after(retry_after, now=now)
        wait = max(MIN_THROTTLE_WAIT_SECONDS, hinted or 0.0)
        try:
            with self._locked(timeout=5.0):
                entities = self._read()
                entry = entities.get(entity_key, {})
                entry["blocked_until"] = now + wait
                entities[entity_key] = entry
                self._write(entities)
        except (RateLimited, OSError):
            # Failing to record a 429 must not mask the 429 itself.
            pass
        return wait

    def blocked_for(self, entity_key: str) -> float:
        """Seconds remaining on a recorded 429 block, or 0."""
        try:
            with self._locked(timeout=5.0):
                entities = self._read()
        except (RateLimited, OSError):
            return 0.0
        entry = entities.get(entity_key, {})
        remaining = float(entry.get("blocked_until") or 0.0) - self.clock()
        return max(0.0, remaining)
