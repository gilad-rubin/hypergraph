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
import threading
from collections import deque
from types import TracebackType

__all__ = ["ProcessLocalLimiter"]


class ProcessLocalLimiter:
    """A pool of concurrency permits shared inside ONE process.

    The name is the scope contract. This limiter coordinates only the
    threads and event loops of the process that constructed it — two
    workers on two machines each get a full budget of their own. It is not
    a distributed limiter and must never be described as one. Hypergraph
    ships no distributed limiter in this tier; if you need fleet-wide
    coordination, own it in the shared component that talks to the
    provider.

    Do not confuse it with the runner's ``max_concurrency``, which is a
    per-call work budget for one run. A ``ProcessLocalLimiter`` is an
    object you construct once and **share**: two concurrent Runs of the
    same graph draw on the same permits.

    Three injection scopes, narrowest budget last:

    - **component** (usually the right owner of a provider quota): the
      shared client holds the limiter and acquires it at the exact scarce
      call, so the permit is held for the provider call and nothing else::

          class SummaryClient:
              def __init__(self) -> None:
                  self._quota = ProcessLocalLimiter(max_concurrent=4)

              async def summarize(self, text: str) -> str:
                  async with self._quota:          # the exact scarce call
                      return await self._http.post(...)

    - **node**: ``@node(..., provider_limit=budget)`` — at most
      ``max_concurrent`` executions of that node run at once, process-wide.
    - **graph**: ``graph.with_provider_limit(budget)`` — at most
      ``max_concurrent`` of that graph's function nodes run at once,
      process-wide.

    Node and graph scopes are **work budgets**: the permit covers the whole
    node execution, including any retry backoff. They compose as narrower
    limits around a component quota; they never replace it. Give each scope
    its own limiter instance — acquiring the same limiter twice on one
    execution path deadlocks, exactly like a non-reentrant lock.

    Args:
        max_concurrent: Number of permits, an ``int >= 1``.

    Example:
        >>> budget = ProcessLocalLimiter(max_concurrent=2)
        >>> with budget:
        ...     budget.in_flight
        1
    """

    def __init__(self, max_concurrent: int) -> None:
        if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int) or max_concurrent < 1:
            raise ValueError(f"ProcessLocalLimiter(max_concurrent=...) must be an int >= 1 concurrent permits, got {max_concurrent!r}.")
        self._max_concurrent = max_concurrent
        self._lock = threading.Lock()
        # Shares _lock, so one critical section covers both waiter kinds.
        self._sync_free = threading.Condition(self._lock)
        self._in_flight = 0
        self._async_waiters: deque[asyncio.Future[None]] = deque()

    @property
    def max_concurrent(self) -> int:
        """Permit count this limiter was built with (never changes)."""
        return self._max_concurrent

    @property
    def in_flight(self) -> int:
        """Permits held right now (0..``max_concurrent``)."""
        with self._lock:
            return self._in_flight

    def __enter__(self) -> ProcessLocalLimiter:
        """Take one permit, blocking this thread until one is free."""
        with self._sync_free:
            while self._in_flight >= self._max_concurrent:
                self._sync_free.wait()
            self._in_flight += 1
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
            # Never barge past a queued async waiter: permits go out in
            # arrival order among awaiting tasks.
            if self._in_flight < self._max_concurrent and not self._async_waiters:
                self._in_flight += 1
                return self
            waiter: asyncio.Future[None] = loop.create_future()
            self._async_waiters.append(waiter)
        try:
            await waiter
        except BaseException:
            hand_back = False
            with self._lock:
                try:
                    self._async_waiters.remove(waiter)
                except ValueError:
                    # Already dequeued: either the permit was handed over
                    # (release it) or the hand-over callback is still
                    # pending and will see a cancelled waiter and release
                    # the permit itself.
                    hand_back = waiter.done() and not waiter.cancelled()
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
        granted: asyncio.Future[None] | None = None
        with self._lock:
            if self._in_flight <= 0:  # pragma: no cover - defensive
                raise RuntimeError("ProcessLocalLimiter released a permit it never held.")
            self._in_flight -= 1
            while self._async_waiters:
                waiter = self._async_waiters.popleft()
                if waiter.cancelled():
                    continue
                # Hand the permit straight over: in_flight never dips below
                # the handover, so a third taker cannot steal it.
                self._in_flight += 1
                granted = waiter
                break
            if granted is None:
                self._sync_free.notify()
        if granted is None:
            return
        try:
            granted.get_loop().call_soon_threadsafe(self._grant, granted)
        except RuntimeError:  # pragma: no cover - waiter's loop already closed
            self._release()

    def _grant(self, waiter: asyncio.Future[None]) -> None:
        """Deliver a handed-over permit in the waiter's own loop."""
        if waiter.cancelled():
            self._release()
            return
        waiter.set_result(None)

    def __repr__(self) -> str:
        return f"ProcessLocalLimiter(max_concurrent={self._max_concurrent}, in_flight={self.in_flight})"
