"""Tests for runner.map_iter() — streaming (index, RunResult) pairs."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from hypergraph import (
    Graph,
    GraphNode,
    NodeContext,
    RunResult,
    RunStatus,
    StreamingChunkEvent,
    TypedEventProcessor,
    node,
)
from hypergraph.checkpointers import MemoryCheckpointer
from hypergraph.runners import AsyncRunner, SyncRunner


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="result")
def fail_on_odd(x: int) -> int:
    if x % 2 != 0:
        raise ValueError(f"odd: {x}")
    return x * 2


def test_sync_map_iter_yields_one_pair_per_item():
    """map_iter yields one (index, RunResult) per input item, in order, with correct values."""
    runner = SyncRunner()
    graph = Graph([double])

    pairs = list(runner.map_iter(graph, {"x": [1, 2, 3]}, map_over="x"))

    assert [idx for idx, _ in pairs] == [0, 1, 2]
    assert all(isinstance(r, RunResult) for _, r in pairs)
    assert [r["doubled"] for _, r in pairs] == [2, 4, 6]


def test_continue_yields_failed_result_and_keeps_going():
    """error_handling='continue' yields a FAILED result for a bad item and keeps streaming."""
    runner = SyncRunner()
    graph = Graph([fail_on_odd])

    pairs = list(runner.map_iter(graph, {"x": [2, 3, 4]}, map_over="x", error_handling="continue"))

    assert [idx for idx, _ in pairs] == [0, 1, 2]
    assert [r.status for _, r in pairs] == [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.COMPLETED]
    assert pairs[0][1]["result"] == 4
    assert pairs[2][1]["result"] == 8


def test_raise_stops_at_failed_item():
    """error_handling='raise' (default) yields successes, then raises when it reaches a failure."""
    runner = SyncRunner()
    graph = Graph([fail_on_odd])

    gen = runner.map_iter(graph, {"x": [2, 3, 4]}, map_over="x")
    idx, result = next(gen)
    assert idx == 0
    assert result["result"] == 4
    with pytest.raises(ValueError, match="odd: 3"):
        next(gen)


def test_lazy_computes_only_consumed_items():
    """The producer does not compute every item before the first yield."""
    calls: list[int] = []

    @node(output_name="seen")
    def record(x: int) -> int:
        calls.append(x)
        return x

    runner = SyncRunner()
    graph = Graph([record])

    gen = runner.map_iter(graph, {"x": [10, 20, 30]}, map_over="x")
    idx, result = next(gen)

    assert (idx, result["seen"]) == (0, 10)
    assert calls == [10]  # 20 and 30 not computed until consumed


def test_empty_map_over_yields_nothing():
    runner = SyncRunner()
    graph = Graph([double])

    assert list(runner.map_iter(graph, {"x": []}, map_over="x")) == []


def test_sync_runner_reports_streaming_capability():
    assert SyncRunner().capabilities.supports_streaming is True


def test_sync_continue_handles_run_validation_error():
    """A per-item validation error becomes a FAILED result under continue, matching async."""

    @node(output_name="s")
    def add(a: int, b: int) -> int:
        return a + b

    runner = SyncRunner()
    graph = Graph([add])

    # map only 'a'; the graph also requires 'b', so each item's run() fails validation
    pairs = list(runner.map_iter(graph, {"a": [1, 2]}, map_over="a", error_handling="continue"))

    assert len(pairs) == 2
    assert all(r.status == RunStatus.FAILED for _, r in pairs)


async def test_async_map_iter_yields_all_items():
    """AsyncRunner.map_iter streams every item with correct values and indices."""
    runner = AsyncRunner()
    graph = Graph([double])

    pairs = [p async for p in runner.map_iter(graph, {"x": [1, 2, 3]}, map_over="x", max_concurrency=2)]
    pairs.sort(key=lambda p: p[0])  # completion order may vary; correlate by index

    assert [idx for idx, _ in pairs] == [0, 1, 2]
    assert [r["doubled"] for _, r in pairs] == [2, 4, 6]


async def test_async_map_iter_streams_with_default_concurrency():
    """Without max_concurrency, map_iter still streams every item (bounded default pool)."""
    runner = AsyncRunner()
    graph = Graph([double])

    pairs = [p async for p in runner.map_iter(graph, {"x": [1, 2, 3]}, map_over="x")]
    pairs.sort(key=lambda p: p[0])

    assert [r["doubled"] for _, r in pairs] == [2, 4, 6]


async def test_async_max_concurrency_caps_in_flight():
    """No more than max_concurrency items execute simultaneously."""
    in_flight = 0
    peak = 0

    @node(output_name="out")
    async def slow(x: int) -> int:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return x

    runner = AsyncRunner()
    graph = Graph([slow])

    pairs = [p async for p in runner.map_iter(graph, {"x": list(range(8))}, map_over="x", max_concurrency=2)]

    assert len(pairs) == 8
    assert peak <= 2


async def test_async_yields_in_completion_order():
    """Faster items yield before slower earlier ones; index preserves correlation."""

    @node(output_name="out")
    async def delayed(x: int) -> int:
        await asyncio.sleep((5 - x) * 0.01)  # x=4 finishes first, x=0 last
        return x * 10

    runner = AsyncRunner()
    graph = Graph([delayed])

    order = []
    async for idx, result in runner.map_iter(graph, {"x": [0, 1, 2, 3, 4]}, map_over="x", max_concurrency=5):
        order.append(idx)
        assert result["out"] == idx * 10  # index correctly correlates to its own result

    assert order == [4, 3, 2, 1, 0]


async def test_async_backpressure_bounds_production():
    """A slow consumer pauses production — the whole batch does not run ahead."""
    produced = 0

    @node(output_name="out")
    async def prod(x: int) -> int:
        nonlocal produced
        produced += 1
        return x

    runner = AsyncRunner()
    graph = Graph([prod])

    agen = runner.map_iter(graph, {"x": list(range(20))}, map_over="x", max_concurrency=2)
    await agen.__anext__()  # consume just one item
    await asyncio.sleep(0.05)  # give workers time to run ahead if they would
    assert produced <= 6  # bounded by buffer(2) + workers(2) + consumed — far below 20
    await agen.aclose()


async def test_async_continue_yields_failed_and_keeps_going():
    runner = AsyncRunner()
    graph = Graph([fail_on_odd])

    pairs = [p async for p in runner.map_iter(graph, {"x": [2, 3, 4]}, map_over="x", max_concurrency=3, error_handling="continue")]
    pairs.sort(key=lambda p: p[0])

    assert [r.status for _, r in pairs] == [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.COMPLETED]


async def test_async_raise_propagates_failure():
    runner = AsyncRunner()
    graph = Graph([fail_on_odd])

    with pytest.raises(ValueError, match="odd"):
        async for _ in runner.map_iter(graph, {"x": [2, 3, 4]}, map_over="x", max_concurrency=3):
            pass


async def test_async_runner_reports_streaming_capability():
    assert AsyncRunner().capabilities.supports_streaming is True


async def test_async_rejects_zero_max_concurrency():
    """max_concurrency=0 fails fast instead of hanging on a zero-permit limiter."""
    runner = AsyncRunner()
    graph = Graph([double])
    with pytest.raises(ValueError, match="max_concurrency"):
        async for _ in runner.map_iter(graph, {"x": [1, 2]}, map_over="x", max_concurrency=0):
            pass


async def test_async_raise_stops_starting_new_items_after_failure():
    """In raise mode, no new items start once one has failed (matches sync/map())."""
    started: list[int] = []

    @node(output_name="out")
    async def maybe_fail(x: int) -> int:
        started.append(x)
        if x == 0:
            raise ValueError("boom")
        return x

    runner = AsyncRunner()
    graph = Graph([maybe_fail])
    with pytest.raises(ValueError, match="boom"):
        async for _ in runner.map_iter(graph, {"x": [0, 1, 2]}, map_over="x", max_concurrency=1):
            pass

    assert started == [0]  # items 1 and 2 never started


async def test_async_surfaces_input_generation_error():
    """A lazy input-generation failure (zip mismatch) is raised, not swallowed."""

    @node(output_name="s")
    def add(a: int, b: int) -> int:
        return a + b

    runner = AsyncRunner()
    graph = Graph([add])
    with pytest.raises(ValueError):
        async for _ in runner.map_iter(graph, {"a": [1, 2, 3], "b": [10, 20]}, map_over=["a", "b"], map_mode="zip", max_concurrency=2):
            pass


async def test_async_propagates_node_base_exception():
    """A BaseException from a node (e.g. cancellation) propagates — never silently dropped."""

    class Boom(BaseException):
        pass

    @node(output_name="out")
    async def boom(x: int) -> int:
        raise Boom("boom")

    runner = AsyncRunner()
    graph = Graph([boom])

    with pytest.raises(Boom):
        async for _ in runner.map_iter(graph, {"x": [1]}, map_over="x", max_concurrency=1):
            pass


# ---------------------------------------------------------------------------
# workflow_id= names the stream so chunks route by (workflow_id, node_name) (#451)
# ---------------------------------------------------------------------------


def _drafting_graph() -> Graph:
    """Child graph that streams from two different nodes."""

    @node(output_name="draft")
    def streamer(topic: str, ctx: NodeContext) -> str:
        ctx.stream(f"drafting:streamer:{topic}")
        return topic

    @node(output_name="drafted")
    def polisher(draft: str, ctx: NodeContext) -> str:
        ctx.stream(f"drafting:polisher:{draft}")
        return draft

    return Graph([streamer, polisher], name="drafting")


def _summary_graph() -> Graph:
    """Sibling child graph whose streaming node shares the local name 'streamer'."""

    @node(output_name="summarized")
    def streamer(topic: str, ctx: NodeContext) -> str:
        ctx.stream(f"summary:streamer:{topic}")
        return topic

    return Graph([streamer], name="summary")


def _nested_streaming_graph() -> Graph:
    """Parent graph with two nested GraphNodes, both streaming."""
    return Graph(
        [GraphNode(_drafting_graph(), name="left"), GraphNode(_summary_graph(), name="right")],
        name="outer",
    )


class _ChunkCollector(TypedEventProcessor):
    """Real EventProcessor subclass, so `graph.with_processors()` accepts it."""

    def __init__(self) -> None:
        self.events: list[StreamingChunkEvent] = []

    def on_streaming_chunk(self, event: StreamingChunkEvent) -> None:
        self.events.append(event)


def _routes(collector: _ChunkCollector) -> set[tuple[str | None, str]]:
    """The documented routing key from docs/05-how-to/observe-execution.md."""
    return {(event.workflow_id, event.node_name) for event in collector.events}


def _expected_routes(prefix: str) -> set[tuple[str, str]]:
    return {
        (f"{prefix}/0/left", "streamer"),
        (f"{prefix}/0/left", "polisher"),
        (f"{prefix}/0/right", "streamer"),
        (f"{prefix}/1/left", "streamer"),
        (f"{prefix}/1/left", "polisher"),
        (f"{prefix}/1/right", "streamer"),
    }


@pytest.mark.parametrize("workflow_id", ["wf", "other"])
def test_sync_map_iter_workflow_id_qualifies_every_chunk_source(workflow_id):
    """Each item runs as <workflow_id>/<i>, so six chunk sources get six routes."""
    collector = _ChunkCollector()
    graph = _nested_streaming_graph().with_processors(collector)

    list(SyncRunner().map_iter(graph, {"topic": ["a", "b"]}, map_over="topic", workflow_id=workflow_id))

    assert len(collector.events) == 6
    assert _routes(collector) == _expected_routes(workflow_id)


@pytest.mark.parametrize("workflow_id", ["wf", "other"])
async def test_async_map_iter_workflow_id_qualifies_every_chunk_source(workflow_id):
    """The async runner produces the identical route set."""
    collector = _ChunkCollector()
    graph = _nested_streaming_graph().with_processors(collector)

    async for _index, _result in AsyncRunner().map_iter(graph, {"topic": ["a", "b"]}, map_over="topic", workflow_id=workflow_id):
        pass

    assert len(collector.events) == 6
    assert _routes(collector) == _expected_routes(workflow_id)


def test_sync_map_iter_matches_map_route_set():
    """map_iter(workflow_id=...) routes byte-identically to map(workflow_id=...)."""
    from_map = _ChunkCollector()
    SyncRunner().map(
        _nested_streaming_graph(),
        {"topic": ["a", "b"]},
        map_over="topic",
        workflow_id="wf",
        event_processors=[from_map],
    )

    from_iter = _ChunkCollector()
    list(
        SyncRunner().map_iter(
            _nested_streaming_graph().with_processors(from_iter),
            {"topic": ["a", "b"]},
            map_over="topic",
            workflow_id="wf",
        )
    )

    assert _routes(from_iter) == _routes(from_map)


def test_sync_map_iter_without_workflow_id_is_unchanged():
    """Omitting workflow_id keeps master's behavior: None everywhere, run_id fallback needed."""
    collector = _ChunkCollector()
    graph = _nested_streaming_graph().with_processors(collector)

    list(SyncRunner().map_iter(graph, {"topic": ["a", "b"]}, map_over="topic"))

    assert len(collector.events) == 6
    assert {event.workflow_id for event in collector.events} == {None}
    assert len(_routes(collector)) == 2
    assert len({(e.workflow_id or e.run_id, e.node_name) for e in collector.events}) == 6


async def test_async_map_iter_without_workflow_id_is_unchanged():
    """Same for the async runner."""
    collector = _ChunkCollector()
    graph = _nested_streaming_graph().with_processors(collector)

    async for _index, _result in AsyncRunner().map_iter(graph, {"topic": ["a", "b"]}, map_over="topic"):
        pass

    assert len(collector.events) == 6
    assert {event.workflow_id for event in collector.events} == {None}
    assert len(_routes(collector)) == 2
    assert len({(e.workflow_id or e.run_id, e.node_name) for e in collector.events}) == 6


def test_sync_map_iter_rejects_slash_in_workflow_id():
    """A caller-supplied '/' is rejected by the same check map() and run() use."""
    executed: list[int] = []

    @node(output_name="out")
    def track(x: int) -> int:
        executed.append(x)
        return x

    runner = SyncRunner()
    graph = Graph([track])

    with pytest.raises(ValueError, match=r"workflow_id cannot contain '/': 'a/b'\."):
        list(runner.map_iter(graph, {"x": [1, 2]}, map_over="x", workflow_id="a/b"))
    assert executed == []


async def test_async_map_iter_rejects_slash_in_workflow_id():
    """Same rejection on the async runner, before any item runs."""
    executed: list[int] = []

    @node(output_name="out")
    async def track(x: int) -> int:
        executed.append(x)
        return x

    runner = AsyncRunner()
    graph = Graph([track])

    with pytest.raises(ValueError, match=r"workflow_id cannot contain '/': 'a/b'\."):
        async for _ in runner.map_iter(graph, {"x": [1, 2]}, map_over="x", workflow_id="a/b"):
            pass
    assert executed == []


def test_sync_map_iter_refuses_workflow_id_when_a_checkpointer_is_attached():
    """map_iter names a stream, not a durable batch: with a checkpointer it refuses."""
    executed: list[int] = []

    @node(output_name="out")
    def track(x: int) -> int:
        executed.append(x)
        return x

    checkpointer = MemoryCheckpointer()
    runner = SyncRunner(checkpointer=checkpointer)
    graph = Graph([track])

    with pytest.raises(ValueError, match="durable batch identity") as excinfo:
        list(runner.map_iter(graph, {"x": [1, 2]}, map_over="x", workflow_id="wf"))

    assert "runner.map(..., workflow_id=...)" in str(excinfo.value)
    assert executed == []


async def test_async_map_iter_refuses_workflow_id_when_a_checkpointer_is_attached():
    """Same refusal on the async runner, before any item runs."""
    executed: list[int] = []

    @node(output_name="out")
    async def track(x: int) -> int:
        executed.append(x)
        return x

    checkpointer = MemoryCheckpointer()
    runner = AsyncRunner(checkpointer=checkpointer)
    graph = Graph([track])

    with pytest.raises(ValueError, match="durable batch identity") as excinfo:
        async for _ in runner.map_iter(graph, {"x": [1, 2]}, map_over="x", workflow_id="wf"):
            pass

    assert "runner.map(..., workflow_id=...)" in str(excinfo.value)
    assert executed == []
    assert await checkpointer.list_runs() == []


async def test_map_iter_without_workflow_id_still_runs_with_a_checkpointer():
    """The refusal is scoped to workflow_id=; the default path is unchanged."""
    checkpointer = MemoryCheckpointer()
    runner = AsyncRunner(checkpointer=checkpointer)
    graph = Graph([double])

    pairs = [pair async for pair in runner.map_iter(graph, {"x": [1, 2]}, map_over="x")]

    assert [r["doubled"] for _, r in pairs] == [2, 4]
    assert await checkpointer.list_runs() == []


def test_sync_map_iter_signature_gained_workflow_id_but_not_max_concurrency():
    """The generated sync half tracks the async source without inheriting async-only options."""
    params = tuple(inspect.signature(SyncRunner.map_iter).parameters)
    assert "workflow_id" in params
    assert "max_concurrency" not in params
    assert "workflow_id" in tuple(inspect.signature(AsyncRunner.map_iter).parameters)
