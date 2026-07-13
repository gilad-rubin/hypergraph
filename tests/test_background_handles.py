"""Public-seam tests for process-local background execution handles."""

from __future__ import annotations

import asyncio
import inspect
import threading

import pytest

from hypergraph import (
    AsyncEventProcessor,
    AsyncRunner,
    FailureEvidence,
    Graph,
    RunEndEvent,
    RunStatus,
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


def test_sync_background_map_collects_all_items_before_raising() -> None:
    failures = {
        1: ValueError("item one failed"),
        3: ValueError("item three failed"),
    }
    entered: list[int] = []

    @node(output_name="processed")
    def process_item(item: int) -> int:
        entered.append(item)
        if item in failures:
            raise failures[item]
        return item * 10

    handle = SyncRunner().start_map(
        Graph([process_item]),
        {"item": [0, 1, 2, 3, 4]},
        map_over="item",
    )

    with pytest.raises(ValueError) as raised:
        handle.result()
    evidence = _assert_raised_failure(raised.value, failures[1])
    batch = handle.result(raise_on_failure=False)

    assert entered == [0, 1, 2, 3, 4]
    assert batch.requested_count == 5
    assert batch.unstarted_item_indexes == ()
    assert [result.status for result in batch] == [
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.COMPLETED,
    ]
    assert batch["processed"] == [0, None, 20, None, 40]
    assert evidence.item_index == 1
    assert handle.done is True


async def test_bounded_async_background_map_collects_later_items_after_failure() -> None:
    failures = {
        1: ValueError("item one failed"),
        3: ValueError("item three failed"),
    }
    entered = [asyncio.Event() for _ in range(5)]
    release_first = asyncio.Event()

    @node(output_name="processed")
    async def process_item(item: int) -> int:
        entered[item].set()
        if item == 0:
            await release_first.wait()
        if item in failures:
            raise failures[item]
        return item * 10

    handle = AsyncRunner().start_map(
        Graph([process_item]),
        {"item": [0, 1, 2, 3, 4]},
        map_over="item",
        max_concurrency=2,
    )

    try:
        await asyncio.wait_for(entered[0].wait(), timeout=5)
        await asyncio.wait_for(entered[1].wait(), timeout=5)
        await asyncio.wait_for(entered[2].wait(), timeout=5)
        assert handle.done is False
    finally:
        release_first.set()

    batch = await asyncio.wait_for(handle.result(raise_on_failure=False), timeout=5)

    with pytest.raises(ValueError) as raised:
        await handle.result()
    evidence = _assert_raised_failure(raised.value, failures[1])

    assert all(event.is_set() for event in entered)
    assert batch.requested_count == 5
    assert batch.unstarted_item_indexes == ()
    assert [result.status for result in batch] == [
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.COMPLETED,
    ]
    assert batch["processed"] == [0, None, 20, None, 40]
    assert evidence.item_index == 1
    assert handle.done is True


def test_sync_stopped_map_contains_only_claimed_results() -> None:
    entered: list[int] = []
    failures: list[ValueError] = []
    second_entered = threading.Event()
    release_second = threading.Event()

    @node(output_name="processed")
    def process_item(item: int) -> int:
        entered.append(item)
        if item == 0:
            failure = ValueError(f"item zero failed on run {len(failures)}")
            failures.append(failure)
            raise failure
        if item == 1:
            second_entered.set()
            if not release_second.wait(timeout=5):
                raise AssertionError("stopped map did not release its claimed item")
        return item * 10

    graph = Graph([process_item])
    runner = SyncRunner()
    early_handle = runner.start_map(
        graph,
        {"item": [0, 1, 2, 3, 4]},
        map_over="item",
    )

    try:
        assert second_entered.wait(timeout=5), "second map item was never claimed"
        early_handle.stop(info={"reason": "user stopped early"})
    finally:
        release_second.set()

    early_batch = early_handle.result(raise_on_failure=False)

    with pytest.raises(ValueError) as raised:
        early_handle.result()
    evidence = _assert_raised_failure(raised.value, failures[0])

    assert entered == [0, 1]
    assert [result.status for result in early_batch] == [
        RunStatus.FAILED,
        RunStatus.STOPPED,
    ]
    assert early_batch.requested_count == 5
    assert early_batch.unstarted_item_indexes == (2, 3, 4)
    assert early_batch.status is RunStatus.STOPPED
    assert early_batch.stopped is True
    assert early_batch.failed is True
    assert early_batch.partial is False
    assert early_batch.failures == [early_batch[0]]
    assert evidence.item_index == 0

    late_handle = runner.start_map(
        graph,
        {"item": [0, 1, 2, 3, 4]},
        map_over="item",
    )
    late_batch = late_handle.result(raise_on_failure=False)
    late_handle.stop(info={"reason": "too late"})

    assert entered == [0, 1, 0, 1, 2, 3, 4]
    assert late_batch.requested_count == 5
    assert late_batch.unstarted_item_indexes == ()
    assert late_batch.status is RunStatus.PARTIAL
    assert late_batch.stopped is False


async def test_bounded_async_stopped_map_is_sparse_and_input_ordered() -> None:
    failure = ValueError("item one failed before stop")
    entered = [asyncio.Event() for _ in range(5)]
    release_first = asyncio.Event()
    failed_child_ended = asyncio.Event()
    release_failed_child_end = asyncio.Event()
    physical_completion_order: list[int] = []

    class HoldFailedChildEnd(AsyncEventProcessor):
        async def on_event_async(self, event) -> None:
            if isinstance(event, RunEndEvent) and event.item_index == 1:
                failed_child_ended.set()
                await release_failed_child_end.wait()

    @node(output_name="processed")
    async def process_item(item: int) -> int:
        entered[item].set()
        if item == 0:
            await release_first.wait()
            physical_completion_order.append(item)
            return item * 10
        if item == 1:
            physical_completion_order.append(item)
            raise failure
        raise AssertionError(f"unstarted item {item} entered the graph")

    handle = AsyncRunner().start_map(
        Graph([process_item]),
        {"item": [0, 1, 2, 3, 4]},
        map_over="item",
        max_concurrency=2,
        event_processors=[HoldFailedChildEnd()],
    )

    try:
        await asyncio.wait_for(entered[0].wait(), timeout=5)
        await asyncio.wait_for(entered[1].wait(), timeout=5)
        await asyncio.wait_for(failed_child_ended.wait(), timeout=5)
        handle.stop(info={"reason": "user stopped early"})
    finally:
        release_first.set()
        release_failed_child_end.set()

    batch = await asyncio.wait_for(handle.result(raise_on_failure=False), timeout=5)

    with pytest.raises(ValueError) as raised:
        await handle.result()
    evidence = _assert_raised_failure(raised.value, failure)

    assert physical_completion_order == [1, 0]
    assert [event.is_set() for event in entered] == [True, True, False, False, False]
    assert [result.status for result in batch] == [
        RunStatus.STOPPED,
        RunStatus.FAILED,
    ]
    assert batch.requested_count == 5
    assert batch.unstarted_item_indexes == (2, 3, 4)
    assert batch.status is RunStatus.STOPPED
    assert batch.stopped is True
    assert batch.failed is True
    assert batch.partial is False
    assert batch.failures == [batch[1]]
    assert evidence.item_index == 1


async def test_async_immediate_stop_before_first_yield_claims_nothing() -> None:
    entered: list[int] = []

    @node(output_name="processed")
    async def process_item(item: int) -> int:
        entered.append(item)
        return item * 10

    handle = AsyncRunner().start_map(
        Graph([process_item]),
        {"item": [0, 1, 2, 3]},
        map_over="item",
    )
    handle.stop(info={"reason": "cancelled before launch"})

    batch = await asyncio.wait_for(handle.result(raise_on_failure=False), timeout=5)

    assert entered == []
    assert batch.results == ()
    assert batch.requested_count == 4
    assert batch.unstarted_item_indexes == (0, 1, 2, 3)
    assert batch.status is RunStatus.STOPPED


async def test_unbounded_async_stop_after_fanout_keeps_all_results_real() -> None:
    failure = ValueError("claimed item failed")
    entered = [asyncio.Event() for _ in range(3)]
    release = asyncio.Event()

    @node(output_name="processed")
    async def process_item(item: int) -> int:
        entered[item].set()
        if item == 0:
            raise failure
        await release.wait()
        return item * 10

    handle = AsyncRunner().start_map(
        Graph([process_item]),
        {"item": [0, 1, 2]},
        map_over="item",
    )

    try:
        for event in entered:
            await asyncio.wait_for(event.wait(), timeout=5)
        handle.stop(info={"reason": "after fanout"})
    finally:
        release.set()

    batch = await asyncio.wait_for(handle.result(raise_on_failure=False), timeout=5)

    assert [result.status for result in batch] == [
        RunStatus.FAILED,
        RunStatus.STOPPED,
        RunStatus.STOPPED,
    ]
    assert batch.requested_count == 3
    assert batch.unstarted_item_indexes == ()
    assert batch.status is RunStatus.FAILED
    assert batch.stopped is False
    assert batch.failed is True
