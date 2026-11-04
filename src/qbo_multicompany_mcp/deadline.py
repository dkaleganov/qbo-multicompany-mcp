"""The whole-call deadline.

Without one, each request has its own 120 seconds, and lock waits, rate waits
and pages each have budgets of their own, so a walk of individually quick pages
can run for as long as it has pages -- and a caller who has given up is still
being fetched for.

So a tool call gets one monotonic deadline, installed by
:func:`qbo_multicompany_mcp.tools.tool_errors` and read wherever time is spent:

* each request's timeout is what is left of the budget, less a reserve for
  building the result, and never more than the 120 seconds after which Intuit
  times a request out (its API call limits and throttles page;
  :mod:`qbo_multicompany_mcp.client`);
* the body is read in chunks, each blocking read is waited for only as long
  as the budget allows, and the deadline is checked between chunks
  (:func:`qbo_multicompany_mcp.netpolicy.send`);
* the call ends with a final checkpoint, so work done after the budget ran
  out does not produce a result as if it had been in time;
* rate-limit waits, store-lock waits and retry sleeps are bounded by it;
* a refresh that could not finish inside what is left is not begun.

Three waits can outlast it, because no timeout here covers them whole: the
operating system's DNS lookup; the connect, the TLS handshake and the first byte,
since each phase gets the whole remainder; and a response whose headers trickle
in, since the read timeout restarts on every socket read. The call ends at the
next check.

**Cancellation rides on the same checkpoints.** The SDK runs a synchronous tool
on a worker thread and, when the caller cancels, waits for that thread to
finish. Every checkpoint therefore also asks AnyIO whether the task that owns
the thread has been cancelled, and stops the business reads if so.

**Credential persistence is the one thing never cut short.** Once a refresh
has been submitted, nothing on its path consults the deadline: abandoning it
after the POST would lose the pair Intuit issued, which costs a consent screen.
That is why the budget check comes *before* a refresh, not during one: under
the store lock, after the wait for it, as the last step before the token is
submitted.

Outside a tool call -- the auth CLI, the keepalive -- there is no deadline, and
requests keep their per-request timeouts.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import anyio.from_thread

from . import constants
from .errors import CANCELLED_TEXT, CallCancelled, DeadlineExceeded, deadline_text


@dataclass
class Deadline:
    budget: float
    expires_at: float
    clock: Callable[[], float] = time.monotonic

    def remaining(self) -> float:
        return self.expires_at - self.clock()

    def usable(self) -> float:
        """What is left for network work: the remaining budget, less the reserve."""
        return self.remaining() - constants.SERIALIZATION_RESERVE_SECONDS

    def checkpoint(self, *, need: float = 0.0) -> None:
        """Stop here if the caller has gone or the budget cannot cover ``need``."""
        _check_cancelled()
        if self.usable() <= need:
            raise DeadlineExceeded(deadline_text(self.budget))

    def bound(self, timeout: float) -> float:
        """``timeout``, cut down to what the budget still allows."""
        self.checkpoint()
        return min(timeout, self.usable())

    def final_checkpoint(self) -> None:
        """The end-of-call check: the whole budget, reserve included, is spent.

        The ordinary checkpoint holds the serialisation reserve back so the
        network work stops in time to build a result; this one runs after the
        result is built, and asks whether the call as a whole overran.
        """
        _check_cancelled()
        if self.remaining() <= 0:
            raise DeadlineExceeded(deadline_text(self.budget))


_CURRENT: ContextVar[Deadline | None] = ContextVar("qbo_call_deadline", default=None)


def current() -> Deadline | None:
    return _CURRENT.get()


@contextmanager
def call_deadline() -> Iterator[Deadline]:
    """Install one deadline for the duration of a tool call.

    A nested call keeps the outer deadline: a budget is never extended by
    starting another one inside it.
    """
    existing = _CURRENT.get()
    if existing is not None:
        yield existing
        return
    budget = constants.PER_CALL_BUDGET_SECONDS
    deadline = Deadline(budget=budget, expires_at=time.monotonic() + budget)
    token = _CURRENT.set(deadline)
    try:
        yield deadline
    finally:
        _CURRENT.reset(token)


def bounded(timeout: float) -> float:
    """``timeout`` bounded by the current deadline, if there is one."""
    deadline = _CURRENT.get()
    return timeout if deadline is None else deadline.bound(timeout)


def _check_cancelled() -> None:
    try:
        anyio.from_thread.check_cancelled()
    except RuntimeError:
        # Not on an AnyIO worker thread: no caller to have cancelled.
        return
    except BaseException:  # noqa: BLE001 - the backend's cancellation exception
        raise CallCancelled(CANCELLED_TEXT) from None
