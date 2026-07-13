"""Public-seam tests for process-local background execution handles."""

from __future__ import annotations

import asyncio
import inspect
import threading

import pytest

from hypergraph import (
    AsyncEventProcessor,
    AsyncRunner,
    EventProcessor,
    FailureEvidence,
    Graph,
    RunEndEvent,
    RunStartEvent,
    RunStatus,
    StopRequestedEvent,
    SyncRunner,
    WorkflowAlreadyRunningError,
    get_failure_evidence,
    node,
)


class _RecordingProcessor(EventProcessor):
    def __init__(self) -> None:
        self.events: list[object] = []

    def on_event(self, event) -> None:
        self.events.append(event)


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
    recorder = _RecordingProcessor()
    first_info = {"reason": "cancelled before launch"}

    @node(output_name="processed")
    async def process_item(item: int) -> int:
        entered.append(item)
        return item * 10

    handle = AsyncRunner().start_map(
        Graph([process_item]),
        {"item": [0, 1, 2, 3]},
        map_over="item",
        event_processors=[recorder],
    )
    handle.stop(info=first_info)
    handle.stop(info={"reason": "must not replace the first request"})

    batch = await asyncio.wait_for(handle.result(raise_on_failure=False), timeout=5)

    parent_starts = [event for event in recorder.events if isinstance(event, RunStartEvent) and event.run_id == batch.run_id]
    parent_stops = [event for event in recorder.events if isinstance(event, StopRequestedEvent) and event.run_id == batch.run_id]
    parent_ends = [event for event in recorder.events if isinstance(event, RunEndEvent) and event.run_id == batch.run_id]

    assert entered == []
    assert batch.results == ()
    assert batch.requested_count == 4
    assert batch.unstarted_item_indexes == (0, 1, 2, 3)
    assert batch.status is RunStatus.STOPPED
    assert len(parent_starts) == 1
    assert len(parent_stops) == 1
    assert parent_stops[0].info is first_info
    assert len(parent_ends) == 1
    assert parent_ends[0].status.value == "stopped"
    assert parent_ends[0].batch_outcome == "stopped"
    assert parent_ends[0].batch_total_items == 0
    assert parent_ends[0].batch_failed_items == 0
    assert parent_ends[0].batch_stopped_items == 0
    assert recorder.events.index(parent_stops[0]) < recorder.events.index(parent_ends[0])


async def test_unbounded_async_stop_after_fanout_keeps_all_results_real() -> None:
    failure = ValueError("claimed item failed")
    entered = [asyncio.Event() for _ in range(3)]
    release = asyncio.Event()
    recorder = _RecordingProcessor()

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
        event_processors=[recorder],
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
    parent_end = next(event for event in recorder.events if isinstance(event, RunEndEvent) and event.run_id == batch.run_id)
    assert parent_end.status.value == "failed"
    assert parent_end.batch_outcome == "failed"
    assert parent_end.batch_total_items == 3
    assert parent_end.batch_failed_items == 1
    assert parent_end.batch_stopped_items == 2


async def test_stopped_background_map_aligns_events_sqlite_and_otel(tmp_path) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    try:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    except ImportError:  # pragma: no cover - compatibility with older SDK layout
        from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

    from hypergraph.checkpointers import SqliteCheckpointer, WorkflowStatus
    from hypergraph.events.otel import OpenTelemetryProcessor

    failure = ValueError("claimed item failed")
    entered = [asyncio.Event() for _ in range(4)]
    release = asyncio.Event()
    recorder = _RecordingProcessor()
    first_info = {"reason": "operator stop"}
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    span_processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(span_processor)
    checkpointer = SqliteCheckpointer(str(tmp_path / "stopped-background-map.db"))

    @node(output_name="processed")
    async def process_item(item: int) -> int:
        entered[item].set()
        await release.wait()
        if item == 0:
            raise failure
        return item * 10

    handle = AsyncRunner(checkpointer=checkpointer).start_map(
        Graph([process_item], name="stopped_batch"),
        {"item": [0, 1, 2, 3]},
        map_over="item",
        max_concurrency=2,
        workflow_id="background-batch",
        event_processors=[
            recorder,
            OpenTelemetryProcessor(tracer_provider=provider),
        ],
    )

    try:
        await asyncio.wait_for(entered[0].wait(), timeout=5)
        await asyncio.wait_for(entered[1].wait(), timeout=5)
        handle.stop(info=first_info)
        handle.stop(info={"reason": "must not replace the first request"})
    finally:
        release.set()

    try:
        batch = await asyncio.wait_for(handle.result(raise_on_failure=False), timeout=5)

        parent_end = next(event for event in recorder.events if isinstance(event, RunEndEvent) and event.run_id == batch.run_id)
        parent_stops = [event for event in recorder.events if isinstance(event, StopRequestedEvent) and event.run_id == batch.run_id]
        child_start_indexes = {event.item_index for event in recorder.events if isinstance(event, RunStartEvent) and event.run_id != batch.run_id}
        child_end_indexes = {event.item_index for event in recorder.events if isinstance(event, RunEndEvent) and event.run_id != batch.run_id}

        assert [result.status for result in batch] == [
            RunStatus.FAILED,
            RunStatus.STOPPED,
        ]
        assert batch.unstarted_item_indexes == (2, 3)
        assert batch.status is RunStatus.STOPPED
        assert parent_end.status.value == "stopped"
        assert parent_end.batch_outcome == "stopped"
        assert parent_end.batch_total_items == 2
        assert parent_end.batch_completed_items == 0
        assert parent_end.batch_failed_items == 1
        assert parent_end.batch_stopped_items == 1
        assert len(parent_stops) == 1
        assert parent_stops[0].info is first_info
        assert recorder.events.index(parent_stops[0]) < recorder.events.index(parent_end)
        assert child_start_indexes == {0, 1}
        assert child_end_indexes == {0, 1}

        persisted_runs = {run.id: run for run in checkpointer.runs()}
        assert set(persisted_runs) == {
            "background-batch",
            "background-batch/0",
            "background-batch/1",
        }
        assert persisted_runs["background-batch"].status is WorkflowStatus.STOPPED
        assert persisted_runs["background-batch"].node_count == 2
        assert persisted_runs["background-batch"].error_count == 1

        map_span = next(span for span in exporter.get_finished_spans() if span.attributes.get("hypergraph.is_map") is True)
        attributes = dict(map_span.attributes)
        assert attributes["hypergraph.run.outcome"] == "stopped"
        assert attributes["hypergraph.batch.outcome"] == "stopped"
        assert attributes["hypergraph.batch.total_items"] == 2
        assert attributes["hypergraph.batch.completed_items"] == 0
        assert attributes["hypergraph.batch.failed_items"] == 1
        assert attributes["hypergraph.batch.stopped_items"] == 1
        assert not any("requested" in key or "unstarted" in key for key in attributes)
    finally:
        await checkpointer.close()
        span_processor.shutdown()


@pytest.mark.parametrize("first_source", ["handle", "runner"])
def test_handle_and_runner_stop_share_one_first_request(first_source: str) -> None:
    entered = threading.Event()
    release = threading.Event()
    recorder = _RecordingProcessor()
    first_info = {"reason": "handle requested stop first"}

    @node(output_name="processed")
    def process_item(item: int) -> int:
        entered.set()
        if not release.wait(timeout=5):
            raise AssertionError("stopped run did not release its claimed node")
        return item * 10

    runner = SyncRunner()
    handle = runner.start_run(
        Graph([process_item]),
        item=1,
        workflow_id="shared-stop-signal",
        event_processors=[recorder],
    )

    try:
        assert entered.wait(timeout=5), "background node never started"
        replacement_info = {"reason": "must not replace first metadata"}
        if first_source == "handle":
            handle.stop(info=first_info)
            runner.stop("shared-stop-signal", info=replacement_info)
        else:
            runner.stop("shared-stop-signal", info=first_info)
            handle.stop(info=replacement_info)
    finally:
        release.set()

    result = handle.result(raise_on_failure=False)
    stop_events = [event for event in recorder.events if isinstance(event, StopRequestedEvent) and event.run_id == result.run_id]

    assert result.status is RunStatus.STOPPED
    assert len(stop_events) == 1
    assert stop_events[0].info is first_info


async def test_runner_stop_targets_background_map_parent_reservation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    recorder = _RecordingProcessor()
    first_info = {"reason": "runner stopped the parent map"}

    @node(output_name="processed")
    async def process_item(item: int) -> int:
        entered.set()
        await release.wait()
        return item * 10

    runner = AsyncRunner()
    handle = runner.start_map(
        Graph([process_item]),
        {"item": [1]},
        map_over="item",
        workflow_id="background-map-parent",
        event_processors=[recorder],
    )

    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        runner.stop("background-map-parent", info=first_info)
        handle.stop(info={"reason": "must not replace runner metadata"})
    finally:
        release.set()

    batch = await asyncio.wait_for(handle.result(raise_on_failure=False), timeout=5)
    parent_stop_events = [event for event in recorder.events if isinstance(event, StopRequestedEvent) and event.run_id == batch.run_id]

    assert batch.status is RunStatus.STOPPED
    assert len(parent_stop_events) == 1
    assert parent_stop_events[0].info is first_info


def test_sync_workflow_reservation_is_atomic_before_handle_return() -> None:
    contender_count = 32
    barrier = threading.Barrier(contender_count + 1)
    release = threading.Event()
    result_lock = threading.Lock()
    body_lock = threading.Lock()
    handles = []
    direct_errors: list[BaseException] = []
    body_entries = 0

    @node(output_name="processed")
    def process_item(item: int) -> int:
        nonlocal body_entries
        with body_lock:
            body_entries += 1
        if not release.wait(timeout=5):
            raise AssertionError("reservation hammer did not release its winner")
        return item * 10

    runner = SyncRunner()
    graph = Graph([process_item])

    def contend() -> None:
        barrier.wait(timeout=5)
        try:
            handle = runner.start_run(
                graph,
                item=1,
                workflow_id="job-42",
            )
        except BaseException as error:
            with result_lock:
                direct_errors.append(error)
        else:
            with result_lock:
                handles.append(handle)

    contenders = [threading.Thread(target=contend) for _ in range(contender_count)]
    for contender in contenders:
        contender.start()
    barrier.wait(timeout=5)
    for contender in contenders:
        contender.join(timeout=5)
        assert not contender.is_alive(), "a reservation contender thread leaked"

    release.set()
    settled_errors: list[BaseException] = []
    for handle in handles:
        try:
            handle.result(raise_on_failure=False)
        except BaseException as error:
            settled_errors.append(error)

    assert len(handles) == 1
    assert len(direct_errors) == contender_count - 1
    assert all(isinstance(error, WorkflowAlreadyRunningError) for error in direct_errors)
    assert settled_errors == []
    assert body_entries == 1


async def test_async_workflow_reservation_precedes_task_execution() -> None:
    release = asyncio.Event()
    handles = []
    direct_errors: list[BaseException] = []
    body_entries = 0

    @node(output_name="processed")
    async def process_item(item: int) -> int:
        nonlocal body_entries
        body_entries += 1
        await release.wait()
        return item * 10

    runner = AsyncRunner()
    graph = Graph([process_item])

    for _ in range(32):
        try:
            handles.append(
                runner.start_run(
                    graph,
                    item=1,
                    workflow_id="job-42",
                )
            )
        except BaseException as error:
            direct_errors.append(error)

    release.set()
    settled_errors: list[BaseException] = []
    for handle in handles:
        try:
            await asyncio.wait_for(
                handle.result(raise_on_failure=False),
                timeout=5,
            )
        except BaseException as error:
            settled_errors.append(error)

    assert len(handles) == 1
    assert len(direct_errors) == 31
    assert all(isinstance(error, WorkflowAlreadyRunningError) for error in direct_errors)
    assert settled_errors == []
    assert body_entries == 1


def test_background_map_reserves_parent_id_against_blocking_run() -> None:
    entered = threading.Event()
    release = threading.Event()
    rejected_events = _RecordingProcessor()

    @node(output_name="processed")
    def process_item(item: int) -> int:
        if item == 0:
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("active background map did not release")
        return item * 10

    runner = SyncRunner()
    graph = Graph([process_item])
    active = runner.start_map(
        graph,
        {"item": [0]},
        map_over="item",
        workflow_id="mixed-workflow",
    )

    try:
        assert entered.wait(timeout=5), "background map item never started"
        with pytest.raises(WorkflowAlreadyRunningError):
            runner.run(
                graph,
                item=99,
                workflow_id="mixed-workflow",
                event_processors=[rejected_events],
            )
    finally:
        release.set()
        active.result(raise_on_failure=False)

    assert rejected_events.events == []


def test_blocking_run_reserves_id_before_background_map_return() -> None:
    entered = threading.Event()
    release = threading.Event()
    rejected_events = _RecordingProcessor()
    active_results = []
    active_errors: list[BaseException] = []

    @node(output_name="processed")
    def process_item(item: int) -> int:
        if item == 0:
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("active blocking run did not release")
        return item * 10

    runner = SyncRunner()
    graph = Graph([process_item])

    def run_active() -> None:
        try:
            active_results.append(
                runner.run(
                    graph,
                    item=0,
                    workflow_id="mixed-workflow",
                )
            )
        except BaseException as error:
            active_errors.append(error)

    active_thread = threading.Thread(target=run_active)
    active_thread.start()
    assert entered.wait(timeout=5), "blocking run never started"

    unexpected_handle = None
    direct_error = None
    try:
        try:
            unexpected_handle = runner.start_map(
                graph,
                {"item": [99]},
                map_over="item",
                workflow_id="mixed-workflow",
                event_processors=[rejected_events],
            )
        except BaseException as error:
            direct_error = error
    finally:
        release.set()
        active_thread.join(timeout=5)
        assert not active_thread.is_alive(), "active blocking run thread leaked"
        if unexpected_handle is not None:
            unexpected_handle.result(raise_on_failure=False)

    assert isinstance(direct_error, WorkflowAlreadyRunningError)
    assert unexpected_handle is None
    assert rejected_events.events == []
    assert active_errors == []
    assert len(active_results) == 1
