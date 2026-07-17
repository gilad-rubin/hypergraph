"""Public-seam tests for AsyncRunner.iter()."""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import (
    AsyncEventProcessor,
    AsyncRunner,
    Graph,
    NodeContext,
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RunEndEvent,
    RunStartEvent,
    StreamingChunkEvent,
    SuperstepStartEvent,
    node,
)


async def test_iter_yields_lifecycle_and_chunks_in_execution_order() -> None:
    @node(output_name="a")
    def first(value: int) -> int:
        return value + 1

    @node(output_name="b")
    def stream_middle(a: int, ctx: NodeContext) -> int:
        ctx.stream("one")
        ctx.stream("two")
        ctx.stream("three")
        return a + 1

    @node(output_name="result")
    def last(b: int) -> int:
        return b + 1

    graph = Graph([first, stream_middle, last], name="ordered_stream")
    runner = AsyncRunner()

    async with runner.iter(graph, {"value": 1}) as handle:
        events = [event async for event in handle]

    assert [(type(event), getattr(event, "node_name", None), getattr(event, "chunk", None)) for event in events] == [
        (RunStartEvent, None, None),
        (SuperstepStartEvent, None, None),
        (NodeStartEvent, "first", None),
        (NodeEndEvent, "first", None),
        (SuperstepStartEvent, None, None),
        (NodeStartEvent, "stream_middle", None),
        (StreamingChunkEvent, "stream_middle", "one"),
        (StreamingChunkEvent, "stream_middle", "two"),
        (StreamingChunkEvent, "stream_middle", "three"),
        (NodeEndEvent, "stream_middle", None),
        (SuperstepStartEvent, None, None),
        (NodeStartEvent, "last", None),
        (NodeEndEvent, "last", None),
        (RunEndEvent, None, None),
    ]
    assert (await handle.result())["result"] == 4


async def test_iter_requires_its_context_manager() -> None:
    @node(output_name="result")
    def identity(value: int) -> int:
        return value

    handle = AsyncRunner().iter(Graph([identity]), {"value": 1})

    with pytest.raises(RuntimeError, match="async with"):
        await handle.__anext__()
    with pytest.raises(RuntimeError, match="async with"):
        await handle.result()


async def test_iter_result_matches_plain_run() -> None:
    @node(output_name="doubled")
    async def double(value: int) -> int:
        return value * 2

    graph = Graph([double], name="result_match")
    values = {"value": 7}
    expected = await AsyncRunner().run(graph, values)

    async with AsyncRunner().iter(graph, values) as handle:
        async for _ in handle:
            pass

    result = await handle.result()
    assert result.values == expected.values
    assert result["doubled"] == expected["doubled"] == 14


async def test_iter_yields_error_event_then_result_raises() -> None:
    sentinel = ValueError("iterator boom")

    @node(output_name="never")
    async def fail() -> int:
        raise sentinel

    async with AsyncRunner().iter(Graph([fail], name="failing_iter")) as handle:
        events = [event async for event in handle]

    assert any(isinstance(event, NodeErrorEvent) for event in events)
    assert isinstance(events[-1], RunEndEvent)
    with pytest.raises(ValueError, match="iterator boom") as raised:
        await handle.result()
    assert raised.value is sentinel


async def test_iter_context_exit_cancels_live_run_and_settles_processors() -> None:
    class Recorder(AsyncEventProcessor):
        def __init__(self) -> None:
            self.shutdown_called = False

        async def shutdown_async(self) -> None:
            self.shutdown_called = True

    @node(output_name="never")
    async def wait_forever() -> int:
        await asyncio.Event().wait()
        return 1

    runner = AsyncRunner()
    recorder = Recorder()

    async with runner.iter(Graph([wait_forever]), event_processors=[recorder]) as handle:
        async for event in handle:
            if isinstance(event, NodeStartEvent):
                break

    assert handle.done
    assert recorder.shutdown_called
    assert runner._background_tasks == set()


async def test_iter_drops_oldest_preview_chunks_but_retains_lifecycle_events() -> None:
    chunk_count = 32

    @node(output_name="result")
    def stream_many(ctx: NodeContext) -> int:
        for chunk in range(chunk_count):
            ctx.stream(chunk)
        return chunk_count

    runner = AsyncRunner()
    delivered = []
    async with runner.iter(Graph([stream_many], name="lossy_preview"), buffer_size=4) as handle:
        async for event in handle:
            delivered.append(event)
            assert handle.buffered_event_count <= handle.buffer_size
            await asyncio.sleep(0.001)

    delivered_chunks = [event for event in delivered if isinstance(event, StreamingChunkEvent)]
    lifecycle_types = [type(event) for event in delivered if not isinstance(event, StreamingChunkEvent)]
    assert lifecycle_types == [
        RunStartEvent,
        SuperstepStartEvent,
        NodeStartEvent,
        NodeEndEvent,
        RunEndEvent,
    ]
    assert handle.dropped_chunks > 0
    assert handle.dropped_chunks + len(delivered_chunks) == chunk_count
