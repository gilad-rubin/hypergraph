"""Integration tests for AsyncRunner event emission."""

from __future__ import annotations

import pytest

from hypergraph import END, AsyncRunner, Graph, node, route
from hypergraph.events import AsyncEventProcessor, EventProcessor
from hypergraph.events.types import (
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RouteDecisionEvent,
    RunEndEvent,
    RunStartEvent,
    RunStatus,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class ListProcessor(EventProcessor):
    """Collects all events synchronously for assertion."""

    def __init__(self):
        self.events: list = []
        self.shutdown_called = False

    def on_event(self, event):
        self.events.append(event)

    def shutdown(self):
        self.shutdown_called = True

    def of_type(self, cls):
        return [e for e in self.events if isinstance(e, cls)]

    def event_types(self):
        return [type(e).__name__ for e in self.events]


class AsyncListProcessor(AsyncEventProcessor):
    """Collects all events via async path for assertion."""

    def __init__(self):
        self.events: list = []
        self.shutdown_called = False

    def on_event(self, event):
        self.events.append(event)

    async def on_event_async(self, event):
        self.events.append(event)

    def shutdown(self):
        self.shutdown_called = True

    async def shutdown_async(self):
        self.shutdown_called = True

    def of_type(self, cls):
        return [e for e in self.events if isinstance(e, cls)]

    def event_types(self):
        return [type(e).__name__ for e in self.events]


# ---------------------------------------------------------------------------
# Simple DAG
# ---------------------------------------------------------------------------


class TestSimpleDAGEvents:
    @pytest.mark.asyncio
    async def test_emits_run_start_and_end(self):
        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        graph = Graph([double])
        runner = AsyncRunner()
        lp = ListProcessor()

        result = await runner.run(graph, {"x": 5}, event_processors=[lp])

        assert result["doubled"] == 10
        starts = lp.of_type(RunStartEvent)
        ends = lp.of_type(RunEndEvent)
        assert len(starts) == 1
        assert len(ends) == 1
        assert starts[0].graph_name == graph.name
        assert ends[0].status == RunStatus.COMPLETED
        assert ends[0].duration_ms > 0

    @pytest.mark.asyncio
    async def test_emits_node_start_and_end(self):
        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        @node(output_name="tripled")
        def triple(doubled: int) -> int:
            return doubled * 3

        graph = Graph([double, triple])
        runner = AsyncRunner()
        lp = ListProcessor()

        await runner.run(graph, {"x": 5}, event_processors=[lp])

        node_starts = lp.of_type(NodeStartEvent)
        node_ends = lp.of_type(NodeEndEvent)
        assert len(node_starts) == 2
        assert len(node_ends) == 2
        names = [e.node_name for e in node_starts]
        assert "double" in names
        assert "triple" in names

    @pytest.mark.asyncio
    async def test_span_hierarchy(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x

        graph = Graph([step])
        runner = AsyncRunner()
        lp = ListProcessor()

        await runner.run(graph, {"x": 1}, event_processors=[lp])

        run_start = lp.of_type(RunStartEvent)[0]
        node_start = lp.of_type(NodeStartEvent)[0]
        # Node's parent_span_id should be the run's span_id
        assert node_start.parent_span_id == run_start.span_id
        # Run's parent_span_id should be None (top-level)
        assert run_start.parent_span_id is None

    @pytest.mark.asyncio
    async def test_run_id_consistent_across_events(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x

        graph = Graph([step])
        runner = AsyncRunner()
        lp = ListProcessor()

        await runner.run(graph, {"x": 1}, event_processors=[lp])

        run_ids = {e.run_id for e in lp.events}
        assert len(run_ids) == 1  # All events share one run_id

    @pytest.mark.asyncio
    async def test_async_processor_used(self):
        """AsyncEventProcessor.on_event_async is called by async runner."""

        @node(output_name="out")
        def step(x: int) -> int:
            return x

        graph = Graph([step])
        runner = AsyncRunner()
        alp = AsyncListProcessor()

        await runner.run(graph, {"x": 1}, event_processors=[alp])

        assert len(alp.events) > 0
        assert any(isinstance(e, RunStartEvent) for e in alp.events)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorEvents:
    @pytest.mark.asyncio
    async def test_node_error_emitted(self):
        @node(output_name="out")
        def failing(x: int) -> int:
            raise ValueError("boom")

        graph = Graph([failing])
        runner = AsyncRunner()
        lp = ListProcessor()

        result = await runner.run(graph, {"x": 1}, error_handling="continue", event_processors=[lp])

        assert result.status.value == "failed"
        errors = lp.of_type(NodeErrorEvent)
        assert len(errors) == 1
        assert errors[0].node_name == "failing"
        assert "boom" in errors[0].error
        assert "ValueError" in errors[0].error_type

    @pytest.mark.asyncio
    async def test_run_end_failed_on_error(self):
        @node(output_name="out")
        def failing(x: int) -> int:
            raise ValueError("boom")

        graph = Graph([failing])
        runner = AsyncRunner()
        lp = ListProcessor()

        await runner.run(graph, {"x": 1}, error_handling="continue", event_processors=[lp])

        run_end = lp.of_type(RunEndEvent)[0]
        assert run_end.status == RunStatus.FAILED
        assert "boom" in run_end.error

    @pytest.mark.asyncio
    async def test_processor_failure_does_not_break_execution(self):
        class BadProcessor(EventProcessor):
            def on_event(self, event):
                raise RuntimeError("processor bug")

        @node(output_name="out")
        def step(x: int) -> int:
            return x * 2

        graph = Graph([step])
        runner = AsyncRunner()
        good = ListProcessor()

        result = await runner.run(graph, {"x": 5}, event_processors=[BadProcessor(), good])

        assert result["out"] == 10
        assert len(good.events) > 0


# ---------------------------------------------------------------------------
# Routing events
# ---------------------------------------------------------------------------


class TestRoutingEvents:
    @pytest.mark.asyncio
    async def test_route_decision_emitted(self):
        @node(output_name="count")
        def increment(count: int) -> int:
            return count + 1

        @route(targets=["increment", END])
        def check(count: int) -> str:
            return END if count >= 1 else "increment"

        graph = Graph([increment, check])
        runner = AsyncRunner()
        lp = ListProcessor()

        result = await runner.run(graph, {"count": 0}, event_processors=[lp])

        assert result.status.value == "completed", f"Run failed: {result.error}"
        decisions = lp.of_type(RouteDecisionEvent)
        assert len(decisions) >= 1
        assert decisions[0].node_name == "check"
        # Last decision should be END (count >= 1)
        assert decisions[-1].decision == END


# ---------------------------------------------------------------------------
# Cyclic graph
# ---------------------------------------------------------------------------


class TestCyclicGraphEvents:
    @pytest.mark.asyncio
    async def test_cyclic_graph_emits_multiple_node_events(self):
        @node(output_name="count")
        def increment(count: int) -> int:
            return count + 1

        @route(targets=["increment", END])
        def check(count: int) -> str:
            return END if count >= 3 else "increment"

        graph = Graph([increment, check])
        runner = AsyncRunner()
        lp = ListProcessor()

        await runner.run(graph, {"count": 0}, event_processors=[lp])

        node_starts = lp.of_type(NodeStartEvent)
        # Multiple iterations: increment and check run multiple times
        assert len(node_starts) > 2

        decisions = lp.of_type(RouteDecisionEvent)
        assert len(decisions) >= 1


# ---------------------------------------------------------------------------
# Nested graph
# ---------------------------------------------------------------------------


class TestNestedGraphEvents:
    @pytest.mark.asyncio
    async def test_nested_graph_emits_inner_run_events(self):
        @node(output_name="inner_out")
        def inner_step(x: int) -> int:
            return x * 2

        inner = Graph([inner_step], name="inner")

        @node(output_name="final")
        def outer_step(inner_out: int) -> int:
            return inner_out + 1

        outer = Graph([inner.as_node(), outer_step], name="outer")
        runner = AsyncRunner()
        lp = ListProcessor()

        result = await runner.run(outer, {"x": 5}, event_processors=[lp])

        assert result["final"] == 11

        run_starts = lp.of_type(RunStartEvent)
        # Outer run + inner run
        assert len(run_starts) == 2
        outer_start = run_starts[0]
        inner_start = run_starts[1]
        assert outer_start.graph_name == "outer"
        assert inner_start.graph_name == "inner"
        # Inner run's parent_span_id should link to outer node's span
        assert inner_start.parent_span_id is not None

    @pytest.mark.asyncio
    async def test_nested_graph_inner_events_have_different_run_id(self):
        @node(output_name="inner_out")
        def inner_step(x: int) -> int:
            return x * 2

        inner = Graph([inner_step], name="inner")
        outer = Graph([inner.as_node()], name="outer")
        runner = AsyncRunner()
        lp = ListProcessor()

        await runner.run(outer, {"x": 5}, event_processors=[lp])

        run_starts = lp.of_type(RunStartEvent)
        assert len(run_starts) == 2
        # Inner and outer have different run_ids
        assert run_starts[0].run_id != run_starts[1].run_id


# ---------------------------------------------------------------------------
# Map events
# ---------------------------------------------------------------------------


class TestMapEvents:
    @pytest.mark.asyncio
    async def test_map_emits_map_run_start(self):
        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        graph = Graph([double])
        runner = AsyncRunner()
        lp = ListProcessor()

        results = await runner.map(graph, {"x": [1, 2, 3]}, map_over="x", event_processors=[lp])

        assert len(results) == 3

        run_starts = lp.of_type(RunStartEvent)
        # 1 map-level RunStart + 3 individual RunStarts
        map_starts = [e for e in run_starts if e.is_map]
        assert len(map_starts) == 1
        assert map_starts[0].map_size == 3

        individual_starts = [e for e in run_starts if not e.is_map]
        assert len(individual_starts) == 3

        # Individual runs should have map's span_id as parent
        for s in individual_starts:
            assert s.parent_span_id == map_starts[0].span_id

    @pytest.mark.asyncio
    async def test_map_emits_run_end(self):
        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        graph = Graph([double])
        runner = AsyncRunner()
        lp = ListProcessor()

        await runner.map(graph, {"x": [1, 2]}, map_over="x", event_processors=[lp])

        run_ends = lp.of_type(RunEndEvent)
        # 1 map-level RunEnd + 2 individual RunEnds
        assert len(run_ends) == 3


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_called_on_top_level_run(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x

        graph = Graph([step])
        runner = AsyncRunner()
        alp = AsyncListProcessor()

        await runner.run(graph, {"x": 1}, event_processors=[alp])
        assert alp.shutdown_called

    @pytest.mark.asyncio
    async def test_shutdown_called_on_top_level_map(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x

        graph = Graph([step])
        runner = AsyncRunner()
        alp = AsyncListProcessor()

        await runner.map(graph, {"x": [1, 2]}, map_over="x", event_processors=[alp])
        assert alp.shutdown_called


# ---------------------------------------------------------------------------
# No processors (backwards compatibility)
# ---------------------------------------------------------------------------


class TestNoProcessors:
    @pytest.mark.asyncio
    async def test_run_without_processors(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x * 2

        graph = Graph([step])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})
        assert result["out"] == 10

    @pytest.mark.asyncio
    async def test_concurrent_graph_nodes_event_context(self):
        """Two GraphNodes in the same superstep get correct parent_span_ids.

        Regression test for race condition: when two GraphNodes execute
        concurrently, set_event_context() on the shared executor must not
        overwrite one node's context before it runs.
        """
        import asyncio

        @node(output_name="a_out")
        async def inner_a(x: int) -> int:
            await asyncio.sleep(0.01)  # Ensure overlap
            return x + 1

        @node(output_name="b_out")
        async def inner_b(y: int) -> int:
            await asyncio.sleep(0.01)
            return y + 2

        graph_a = Graph([inner_a], name="graph_a")
        graph_b = Graph([inner_b], name="graph_b")

        # Both GraphNodes take "val" as input → both ready in same superstep
        @node(output_name="val")
        def produce(x: int) -> int:
            return x

        outer = Graph(
            [
                produce,
                graph_a.as_node().with_inputs(x="val"),
                graph_b.as_node().with_inputs(y="val"),
            ],
            name="outer",
        )

        lp = ListProcessor()
        runner = AsyncRunner()
        result = await runner.run(outer, {"x": 5}, event_processors=[lp])

        # Both nested graphs should have completed
        assert result["a_out"] == 6
        assert result["b_out"] == 7

        # Each nested RunStartEvent's parent_span_id should match
        # a NodeStartEvent for the corresponding GraphNode
        nested_run_starts = [e for e in lp.of_type(RunStartEvent) if e.parent_span_id is not None and e.graph_name in ("graph_a", "graph_b")]
        assert len(nested_run_starts) == 2

        node_starts = {e.span_id: e for e in lp.of_type(NodeStartEvent)}
        for rs in nested_run_starts:
            parent_node = node_starts.get(rs.parent_span_id)
            assert parent_node is not None, (
                f"Nested run for {rs.graph_name} has parent_span_id={rs.parent_span_id} which doesn't match any NodeStartEvent"
            )
            assert parent_node.node_name in ("graph_a", "graph_b"), f"Expected parent node to be a graph node, got {parent_node.node_name}"

    @pytest.mark.asyncio
    async def test_map_without_processors(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x * 2

        graph = Graph([step])
        runner = AsyncRunner()

        results = await runner.map(graph, {"x": [1, 2]}, map_over="x")
        assert len(results) == 2
