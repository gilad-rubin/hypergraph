"""AsyncRunner runs plain ``def`` node bodies on worker threads.

The contract under test:

- a sync body never runs on the event-loop thread, so blocking or CPU work
  in one node cannot stall concurrent work in the same run;
- ``async def`` bodies run on the loop, exactly as before;
- sync generator bodies (user code too) are consumed inside the worker
  thread;
- the dispatch is SETTLED: cancellation — including a second cancellation
  arriving mid-drain — waits for the thread to finish before it surfaces,
  so a threaded body is never abandoned mid-write.

Every blocking wait is event/barrier ordered; ``wait(15)`` bounds are
deadlock guards, not elapsed-time assertions.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from hypergraph import AsyncRunner, Graph, node


async def test_sync_node_blocks_without_stalling_async_sibling() -> None:
    """A ``def`` body parked on an Event runs CONCURRENTLY with an
    ``async def`` sibling in the same run: the async node completes (and
    releases the sync node) while the sync node is still blocked. Inline
    sync execution would block the loop and the release could never come."""
    release = threading.Event()

    @node(output_name="a")
    def blocker() -> bool:
        # True only if the async sibling ran to completion DURING this body.
        return release.wait(15)

    @node(output_name="b")
    async def free() -> int:
        release.set()
        return 1

    result = await AsyncRunner().run(Graph([blocker, free], name="offloop"))

    assert result["a"] is True
    assert result["b"] == 1


async def test_def_runs_off_loop_and_async_def_on_loop() -> None:
    loop_thread = threading.get_ident()

    @node(output_name="sync_thread")
    def sync_probe() -> int:
        return threading.get_ident()

    @node(output_name="async_thread")
    async def async_probe() -> int:
        return threading.get_ident()

    result = await AsyncRunner().run(Graph([sync_probe, async_probe], name="threads"))

    assert result["sync_thread"] != loop_thread
    assert result["async_thread"] == loop_thread


async def test_sync_generator_yields_items_and_body_runs_in_thread() -> None:
    loop_thread = threading.get_ident()
    body_threads: list[int] = []

    @node(output_name="items")
    def chunks(n: int):
        for i in range(n):
            body_threads.append(threading.get_ident())
            yield i

    result = await AsyncRunner().run(Graph([chunks], name="gen"), {"n": 3})

    assert result["items"] == [0, 1, 2]
    assert body_threads and all(t != loop_thread for t in body_threads)


async def test_sync_body_stopiteration_stays_an_ordinary_error() -> None:
    """A raw StopIteration from user sync code must not cross the
    thread->coroutine boundary; it surfaces as a RuntimeError naming it."""

    @node(output_name="out")
    def exhausted() -> int:
        return next(iter([]))

    with pytest.raises(RuntimeError, match="StopIteration"):
        await AsyncRunner().run(Graph([exhausted], name="stopiter"))


async def test_cancelled_run_settles_sync_thread_before_cancellation_surfaces() -> None:
    """Cancel a run while a sync node's thread is mid-write: the write
    COMPLETES before CancelledError reaches the caller, and until the
    thread settles the cancellation is HELD (the run is not done)."""
    started = threading.Event()
    proceed = threading.Event()
    writes: list[int] = []

    @node(output_name="out")
    def slow_write(x: int) -> int:
        started.set()
        proceed.wait(15)
        writes.append(x)
        return x

    run_task = asyncio.create_task(AsyncRunner().run(Graph([slow_write], name="settle"), {"x": 7}))
    assert await asyncio.to_thread(started.wait, 15)

    run_task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    # The drain is holding the cancellation while the thread is parked.
    assert not run_task.done()
    assert writes == []

    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert writes == [7]


async def test_second_cancel_during_drain_still_settles_sync_thread() -> None:
    """The review-found drain gap: a second CancelledError arriving during
    the drain must restart the wait, not return while the thread runs."""
    started = threading.Event()
    proceed = threading.Event()
    writes: list[int] = []

    @node(output_name="out")
    def slow_write(x: int) -> int:
        started.set()
        proceed.wait(15)
        writes.append(x)
        return x

    run_task = asyncio.create_task(AsyncRunner().run(Graph([slow_write], name="settle2"), {"x": 7}))
    assert await asyncio.to_thread(started.wait, 15)

    run_task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    run_task.cancel()  # lands inside the drain wait
    for _ in range(5):
        await asyncio.sleep(0)
    assert not run_task.done()
    assert writes == []

    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert writes == [7]
