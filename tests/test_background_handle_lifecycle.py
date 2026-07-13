"""Lifecycle truth for process-local background execution handles."""

from __future__ import annotations

import asyncio
import threading

from hypergraph import (
    AsyncRunner,
    EventProcessor,
    Graph,
    NodeContext,
    NodeStartEvent,
    RunEndEvent,
    RunStartEvent,
    RunStatus,
    StopRequestedEvent,
    SyncRunner,
    node,
)


class _RecordingProcessor(EventProcessor):
    def __init__(self) -> None:
        self.events: list[object] = []

    def on_event(self, event) -> None:
        self.events.append(event)


def test_sync_stopped_map_has_only_real_child_events_logs_and_run_ids() -> None:
    entered_second = threading.Event()
    release_second = threading.Event()
    recorder = _RecordingProcessor()
    first_info = {"reason": "stop after two real claims"}

    @node(output_name="processed")
    def process_item(item: int) -> int:
        if item == 0:
            raise ValueError("claimed item failed")
        if item == 1:
            entered_second.set()
            if not release_second.wait(timeout=5):
                raise AssertionError("claimed item was never released")
            return item * 10
        raise AssertionError(f"unstarted item {item} entered the graph")

    runner = SyncRunner()
    handle = runner.start_map(
        Graph([process_item], name="sync_sparse"),
        {"item": [0, 1, 2, 3]},
        map_over="item",
        workflow_id="sync-sparse",
        event_processors=[recorder],
    )

    try:
        assert entered_second.wait(timeout=5), "second real item never started"
        handle.stop(info=first_info)
        handle.stop(info={"reason": "must not replace first metadata"})
    finally:
        release_second.set()

    batch = handle.result(raise_on_failure=False)
    parent_stops = [event for event in recorder.events if isinstance(event, StopRequestedEvent) and event.run_id == batch.run_id]
    child_starts = [event for event in recorder.events if isinstance(event, RunStartEvent) and event.item_index is not None]
    child_ends = [event for event in recorder.events if isinstance(event, RunEndEvent) and event.item_index is not None]

    assert [result.status for result in batch] == [
        RunStatus.FAILED,
        RunStatus.STOPPED,
    ]
    assert batch.unstarted_item_indexes == (2, 3)
    assert {result.workflow_id for result in batch} == {
        "sync-sparse/0",
        "sync-sparse/1",
    }
    assert {event.item_index for event in child_starts} == {0, 1}
    assert {event.item_index for event in child_ends} == {0, 1}
    assert {event.run_id for event in child_starts} == {result.run_id for result in batch}
    assert tuple(log.run_id for log in batch.log.items) == tuple(result.run_id for result in batch)
    assert len(parent_stops) == 1
    assert parent_stops[0].info is first_info

    settled_event_count = len(recorder.events)
    handle.stop(info={"reason": "too late"})
    assert handle.result(raise_on_failure=False) is batch
    assert len(recorder.events) == settled_event_count


async def test_handle_local_stop_reaches_nested_graph_without_workflow_id() -> None:
    entered_inner = asyncio.Event()
    release_inner = asyncio.Event()
    entered_downstream = asyncio.Event()
    recorder = _RecordingProcessor()
    first_info = {"reason": "operator stopped the nested run"}

    @node(output_name="intermediate")
    async def gated_inner(value: int, ctx: NodeContext) -> int:
        entered_inner.set()
        await release_inner.wait()
        assert ctx.stop_requested is True
        return value + 1

    @node(output_name="finished")
    async def downstream_inner(intermediate: int) -> int:
        entered_downstream.set()
        return intermediate * 2

    inner = Graph(
        [gated_inner, downstream_inner],
        name="nested_work",
    ).as_node()
    outer = Graph([inner], name="outer_work")
    handle = AsyncRunner().start_run(
        outer,
        value=2,
        event_processors=[recorder],
    )

    try:
        await asyncio.wait_for(entered_inner.wait(), timeout=5)
        handle.stop(info=first_info)
        handle.stop(info={"reason": "must not replace first metadata"})
    finally:
        release_inner.set()

    result = await asyncio.wait_for(
        handle.result(raise_on_failure=False),
        timeout=5,
    )
    nested_stop_events = [event for event in recorder.events if isinstance(event, StopRequestedEvent)]

    assert result.workflow_id is None
    assert result.status is RunStatus.STOPPED
    assert entered_downstream.is_set() is False
    assert not any(isinstance(event, NodeStartEvent) and event.node_name == "downstream_inner" for event in recorder.events)
    assert nested_stop_events
    assert all(event.info is first_info for event in nested_stop_events)

    settled_event_count = len(recorder.events)
    handle.stop(info={"reason": "too late"})
    assert await handle.result(raise_on_failure=False) is result
    assert result.status is RunStatus.STOPPED
    assert len(recorder.events) == settled_event_count


def test_sync_empty_background_map_is_a_real_empty_result() -> None:
    recorder = _RecordingProcessor()

    @node(output_name="doubled")
    def double(value: int) -> int:
        return value * 2

    runner = SyncRunner()
    graph = Graph([double])
    first_handle = runner.start_map(
        graph,
        {"value": []},
        map_over="value",
        workflow_id="empty-map",
        event_processors=[recorder],
    )
    first = first_handle.result(raise_on_failure=False)
    second = runner.start_map(
        graph,
        {"value": []},
        map_over="value",
        workflow_id="empty-map",
        event_processors=[recorder],
    ).result(raise_on_failure=False)

    assert first.results == ()
    assert first.requested_count == 0
    assert first.unstarted_item_indexes == ()
    assert first.status is RunStatus.COMPLETED
    assert first.run_id is None
    assert first.log.items == ()
    assert first_handle.done is True
    assert first_handle.result(raise_on_failure=False) is first
    assert second.results == ()
    assert recorder.events == []


async def test_async_empty_background_map_is_a_real_empty_result() -> None:
    recorder = _RecordingProcessor()

    @node(output_name="doubled")
    async def double(value: int) -> int:
        return value * 2

    runner = AsyncRunner()
    graph = Graph([double])
    first_handle = runner.start_map(
        graph,
        {"value": []},
        map_over="value",
        workflow_id="empty-map",
        event_processors=[recorder],
    )
    first = await first_handle.result(raise_on_failure=False)
    second = await runner.start_map(
        graph,
        {"value": []},
        map_over="value",
        workflow_id="empty-map",
        event_processors=[recorder],
    ).result(raise_on_failure=False)

    assert first.results == ()
    assert first.requested_count == 0
    assert first.unstarted_item_indexes == ()
    assert first.status is RunStatus.COMPLETED
    assert first.run_id is None
    assert first.log.items == ()
    assert first_handle.done is True
    assert await first_handle.result(raise_on_failure=False) is first
    assert second.results == ()
    assert recorder.events == []
