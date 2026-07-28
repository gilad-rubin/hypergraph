"""Provider-resource admission: injected limiters that own external capacity.

Hypergraph has **two** admission controls and they are never the same thing:

- **Host work admission** — ``RunHome.max_active_runs`` (see
  ``hypergraph.host``): how many Runs one worker executes at once. It is a
  Run Home concern, tuned by operators, and over-limit Runs wait in claim
  order as ``WaitingCondition.ADMISSION_LIMITED``.
- **Provider-resource admission** — this module: how many concurrent calls
  an external provider tolerates. It is a graph/node/component concern,
  injected by the workflow author, and a Run waiting for a permit is still
  a *running* Run holding its Host slot.

A provider-permit wait is neither a failure nor a retry attempt: it happens
outside the attempt coordinator, so ordinary throttling never consumes a
``RetryPolicy`` budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import threading
from collections import deque
from types import TracebackType

__all__ = ["ProcessLocalLimiter"]

# A stable total order over limiter INSTANCES, minted at construction.
#
# Holding several of these at once is lock ordering, and lock ordering is only
# deadlock-free when every path takes the shared limiters in the same order.
# "Graph budgets before the node budget" orders the scopes of one execution
# path; it is not an order over instances, because two legal graphs can name
# the same two limiters at opposite scopes and then wait on each other.
#
# The rank is monotonic and never reused for the life of the process. ``id()``
# is not a substitute: CPython recycles addresses, so two limiters that never
# coexist can share one id, and an id-keyed order is neither meaningful nor
# stable across the objects it is supposed to rank.
_ACQUISITION_RANKS = itertools.count()
_ACQUISITION_RANK_LOCK = threading.Lock()


def _next_acquisition_rank() -> int:
    """One never-reused rank. Locked: limiters are built from any thread."""
    with _ACQUISITION_RANK_LOCK:
        return next(_ACQUISITION_RANKS)


class _SyncWaiter:
    """A thread parked in ``__enter__``, queued in arrival order.

    Each waiter owns a ``Condition`` over the limiter's single lock, so a
    release can wake exactly the one taker whose turn it is instead of
    stampeding every parked thread.
    """

    __slots__ = ("cond", "granted")

    def __init__(self, lock: threading.Lock) -> None:
        self.cond = threading.Condition(lock)
        self.granted = False


class _AsyncWaiter:
    """A task suspended in ``__aenter__``, queued in arrival order."""

    __slots__ = ("future",)

    def __init__(self, future: asyncio.Future[None]) -> None:
        self.future = future


def _reject_blocking_the_event_loop() -> None:
    """Refuse to park a thread that is running an event loop.

    ``with limiter:`` blocks the calling thread. Blocking a loop thread also
    stops every task that holds a permit from ever releasing it: the wait
    cannot end. (Sync node bodies under ``AsyncRunner`` run on worker
    threads, so they park legitimately — this guard bites code that really
    is on a loop thread, such as async code taking the sync form.)
    Detection is exact and costs one call — ``get_running_loop()`` raises
    unless this very thread is driving a loop — and it only runs on the path
    that was about to block, so an uncontended acquire is untouched.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "ProcessLocalLimiter: 'with limiter:' would block a thread that is running an event loop, "
        "and the tasks holding the permits cannot release them while that thread is blocked. "
        "This is a hang, not a wait.\n\n"
        "How to fix:\n"
        "  - In async code, take the permit with 'async with limiter:'.\n"
        "  - Or declare the budget as @node(provider_limit=...) or\n"
        "    graph.with_provider_limit(...) and let the runner take it for you."
    )


class ProcessLocalLimiter:
    """A pool of concurrency permits shared inside ONE process.

    The name is the scope contract. This limiter coordinates only the
    threads and event loops of the process that constructed it — two
    workers on two machines each get a full budget of their own. It is not
    a distributed limiter and must never be described as one. Hypergraph
    ships no distributed limiter in this tier; if you need fleet-wide
    coordination, own it in the shared component that talks to the
    provider.

    A ``ProcessLocalLimiter`` is an object you construct once and
    **share**: two concurrent Runs of the same graph draw on the same
    permits.

    Three injection scopes, narrowest budget last:

    - **component** (usually the right owner of a provider quota): the
      shared client holds the limiter and acquires it at the exact scarce
      call, so the permit is held for the provider call and nothing else::

          class SummaryClient:
              def __init__(self) -> None:
                  self._quota = ProcessLocalLimiter(max_in_flight=4)

              async def summarize(self, text: str) -> str:
                  async with self._quota:          # the exact scarce call
                      return await self._http.post(...)

    - **node**: ``@node(..., provider_limit=budget)`` — at most
      ``max_in_flight`` executions of that node run at once, process-wide.
    - **graph**: ``graph.with_provider_limit(budget)`` — at most
      ``max_in_flight`` of that graph's function nodes run at once,
      process-wide, nested graphs included.

    Node and graph scopes are **work budgets**: the permit covers the whole
    node execution, including any retry backoff. They compose as narrower
    limits around a component quota; they never replace it.

    A single limiter is not reentrant — acquiring it twice on one execution
    path deadlocks, exactly like a non-reentrant lock — so the runner
    collapses a limiter injected at two scopes to ONE permit. When the runner
    holds several *different* limiters for one node it takes them in one
    process-wide order (the order the limiters were constructed in), so no two
    execution paths can end up each holding the permit the other is waiting
    for. Acquire in that same order if you take several by hand.

    Threads and tasks share ONE arrival-ordered queue, so neither kind can
    starve the other. ``with limiter:`` blocks a thread that is not running
    an event loop (including the worker thread a sync node body runs on
    under ``AsyncRunner``); on a loop thread it raises instead of hanging
    (use ``async with limiter:`` there).

    Args:
        max_in_flight: Number of permits, an ``int >= 1``.

    Example:
        >>> budget = ProcessLocalLimiter(max_in_flight=2)
        >>> with budget:
        ...     budget.in_flight
        1
    """

    def __init__(self, max_in_flight: int) -> None:
        if isinstance(max_in_flight, bool) or not isinstance(max_in_flight, int) or max_in_flight < 1:
            raise ValueError(
                f"ProcessLocalLimiter(max_in_flight=...) must be an int >= 1 concurrent permits, got {max_in_flight!r}.\n\n"
                "How to fix:\n"
                "  Pass the number of concurrent calls the provider tolerates:\n"
                "  ProcessLocalLimiter(max_in_flight=4)"
            )
        self._max_in_flight = max_in_flight
        # Where this instance sits in the process-wide acquisition order.
        self._acquisition_rank = _next_acquisition_rank()
        self._lock = threading.Lock()
        self._in_flight = 0
        # ONE arrival-ordered queue for both waiter kinds. Two queues (or an
        # async-first release) would let a continuous stream of one kind
        # starve the other.
        self._waiters: deque[_SyncWaiter | _AsyncWaiter] = deque()

    @property
    def max_in_flight(self) -> int:
        """Permit count this limiter was built with (never changes)."""
        return self._max_in_flight

    @property
    def in_flight(self) -> int:
        """Permits held right now (0..``max_in_flight``)."""
        with self._lock:
            return self._in_flight

    def __enter__(self) -> ProcessLocalLimiter:
        """Take one permit, blocking this thread until one is free."""
        with self._lock:
            # Never barge past a queued taker of either kind.
            if self._in_flight < self._max_in_flight and not self._waiters:
                self._in_flight += 1
                return self
            _reject_blocking_the_event_loop()
            waiter = _SyncWaiter(self._lock)
            self._waiters.append(waiter)
            try:
                while not waiter.granted:
                    waiter.cond.wait()
            except BaseException:
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)
                if waiter.granted:
                    # The permit was handed over before the interruption;
                    # give it to the next taker rather than leaking it.
                    self._in_flight -= 1
                    self._release_locked()
                raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._release()

    async def __aenter__(self) -> ProcessLocalLimiter:
        """Take one permit, suspending this task until one is free."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._in_flight < self._max_in_flight and not self._waiters:
                self._in_flight += 1
                return self
            waiter = _AsyncWaiter(loop.create_future())
            self._waiters.append(waiter)
        try:
            await waiter.future
        except BaseException:
            hand_back = False
            with self._lock:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    # Already dequeued: either the permit was handed over
                    # (release it) or the hand-over callback is still
                    # pending and will see a cancelled waiter and release
                    # the permit itself.
                    hand_back = waiter.future.done() and not waiter.future.cancelled()
            if hand_back:
                self._release()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._release()

    def _release(self) -> None:
        """Return one permit, handing it to the longest-waiting taker."""
        with self._lock:
            if self._in_flight <= 0:  # pragma: no cover - defensive
                raise RuntimeError("ProcessLocalLimiter released a permit it never held.")
            self._in_flight -= 1
            self._release_locked()

    def _release_locked(self) -> None:
        """Hand the free permit to the head of the queue; caller holds the lock.

        The permit is handed straight over — ``in_flight`` never dips below
        the handover — so a third taker cannot steal it from the waiter whose
        turn it is.
        """
        while self._waiters:
            waiter = self._waiters.popleft()
            if isinstance(waiter, _SyncWaiter):
                self._in_flight += 1
                waiter.granted = True
                # Safe under the lock: notify() only marks the waiter
                # runnable, and the woken thread reacquires the lock itself.
                waiter.cond.notify()
                return
            if waiter.future.cancelled():
                continue
            self._in_flight += 1
            try:
                waiter.future.get_loop().call_soon_threadsafe(self._grant, waiter.future)
            except RuntimeError:  # pragma: no cover - waiter's loop already closed
                self._in_flight -= 1
                continue
            return

    def _grant(self, waiter: asyncio.Future[None]) -> None:
        """Deliver a handed-over permit in the waiter's own loop."""
        if waiter.cancelled():
            self._release()
            return
        waiter.set_result(None)

    def __repr__(self) -> str:
        return f"ProcessLocalLimiter(max_in_flight={self._max_in_flight}, in_flight={self.in_flight})"
