"""Public-seam tests for process-local background execution handles."""

from __future__ import annotations

import asyncio
import inspect
import threading

import pytest

from hypergraph import (
    AsyncRunner,
    FailureEvidence,
    Graph,
    SyncRunner,
    get_failure_evidence,
    node,
)


def _assert_raised_failure(
    error: BaseException,
    sentinel: BaseException,
) -> FailureEvidence:
    assert error is sentinel
    evidence = get_failure_evidence(error)
    assert len(evidence) == 1
    assert evidence[0].error is sentinel
    return evidence[0]


def test_sync_start_run_returns_before_execution_settles() -> None:
    """A sync start call returns control while real node work is still live."""
    entered = threading.Event()
    start_returned = threading.Event()

    @node(output_name="doubled")
    def gated_double(x: int) -> int:
        entered.set()
        if not start_returned.wait(timeout=5):
            raise AssertionError("start_run() blocked until node execution settled")
        return x * 2

    runner = SyncRunner()
    handle = runner.start_run(Graph([gated_double]), x=9)

    try:
        assert entered.wait(timeout=5), "background node never started"
        assert handle.done is False
    finally:
        start_returned.set()

    result = handle.result()

    assert result["doubled"] == 18
    assert handle.done is True


async def test_cancelling_async_result_waiter_does_not_cancel_execution() -> None:
    """Cancelling one result waiter leaves the background execution live."""
    entered = asyncio.Event()
    release = asyncio.Event()
    waiter_started = asyncio.Event()

    @node(output_name="doubled")
    async def gated_double(x: int) -> int:
        entered.set()
        await release.wait()
        return x * 2

    runner = AsyncRunner()
    handle = runner.start_run(Graph([gated_double]), x=9)
    await asyncio.wait_for(entered.wait(), timeout=5)

    async def retrieve_result():
        waiter_started.set()
        return await handle.result()

    waiter = asyncio.create_task(retrieve_result())
    await asyncio.wait_for(waiter_started.wait(), timeout=5)

    try:
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert handle.done is False
    finally:
        release.set()

    result = await asyncio.wait_for(handle.result(), timeout=5)

    assert result["doubled"] == 18
    assert handle.done is True


async def test_async_start_run_returns_before_execution_settles() -> None:
    """An async start call returns a handle while real node work is still live."""
    entered = asyncio.Event()
    release = asyncio.Event()

    @node(output_name="doubled")
    async def gated_double(x: int) -> int:
        entered.set()
        await release.wait()
        return x * 2

    runner = AsyncRunner()
    handle = runner.start_run(Graph([gated_double]), x=9)

    try:
        assert inspect.isawaitable(handle) is False
        assert handle.done is False
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert handle.done is False
    finally:
        release.set()

    result = await asyncio.wait_for(handle.result(), timeout=5)

    assert result["doubled"] == 18
    assert handle.done is True


def test_sync_failed_run_retrieval_is_stable_across_raise_first() -> None:
    sentinel = RuntimeError("sync background failure")

    @node(output_name="never")
    def fail_in_node() -> int:
        raise sentinel

    handle = SyncRunner().start_run(Graph([fail_in_node]))

    with pytest.raises(RuntimeError) as first_raise:
        handle.result()
    first_evidence = _assert_raised_failure(first_raise.value, sentinel)

    returned = handle.result(raise_on_failure=False)

    with pytest.raises(RuntimeError) as second_raise:
        handle.result()
    second_evidence = _assert_raised_failure(second_raise.value, sentinel)

    returned_again = handle.result(raise_on_failure=False)

    assert returned_again is returned
    assert returned.failed
    assert returned.error is sentinel
    assert returned.failure is first_evidence
    assert returned.node_failures == (first_evidence,)
    assert second_evidence is first_evidence
    assert handle.done is True


async def test_async_failed_run_retrieval_is_stable_across_return_first() -> None:
    sentinel = RuntimeError("async background failure")

    @node(output_name="never")
    async def fail_in_node() -> int:
        raise sentinel

    handle = AsyncRunner().start_run(Graph([fail_in_node]))

    returned = await handle.result(raise_on_failure=False)

    with pytest.raises(RuntimeError) as first_raise:
        await handle.result()
    first_evidence = _assert_raised_failure(first_raise.value, sentinel)

    returned_again = await handle.result(raise_on_failure=False)

    with pytest.raises(RuntimeError) as second_raise:
        await handle.result()
    second_evidence = _assert_raised_failure(second_raise.value, sentinel)

    assert returned_again is returned
    assert returned.failed
    assert returned.error is sentinel
    assert returned.failure is first_evidence
    assert returned.node_failures == (first_evidence,)
    assert second_evidence is first_evidence
    assert handle.done is True
