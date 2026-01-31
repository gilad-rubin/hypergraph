"""Integration tests for SyncRunner event emission."""

from __future__ import annotations

import pytest

from hypergraph import END, Graph, SyncRunner, node, route
from hypergraph.events import EventProcessor, TypedEventProcessor
from hypergraph.events.types import (
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RouteDecisionEvent,
    RunEndEvent,
    RunStartEvent,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class ListProcessor(EventProcessor):
    """Collects all events for assertion."""

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


# ---------------------------------------------------------------------------
# Simple DAG
# ---------------------------------------------------------------------------


class TestSimpleDAGEvents:
    def test_emits_run_start_and_end(self):
        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        graph = Graph([double])
        runner = SyncRunner()
        lp = ListProcessor()

        result = runner.run(graph, {"x": 5}, event_processors=[lp])

        assert result["doubled"] == 10
        starts = lp.of_type(RunStartEvent)
        ends = lp.of_type(RunEndEvent)
        assert len(starts) == 1
        assert len(ends) == 1
        assert starts[0].graph_name == graph.name
        assert ends[0].status == "completed"
        assert ends[0].duration_ms > 0

    def test_emits_node_start_and_end(self):
        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        @node(output_name="tripled")
        def triple(doubled: int) -> int:
            return doubled * 3

        graph = Graph([double, triple])
        runner = SyncRunner()
        lp = ListProcessor()

        runner.run(graph, {"x": 5}, event_processors=[lp])

        node_starts = lp.of_type(NodeStartEvent)
        node_ends = lp.of_type(NodeEndEvent)
        assert len(node_starts) == 2
        assert len(node_ends) == 2
        names = [e.node_name for e in node_starts]
        assert "double" in names
        assert "triple" in names

    def test_event_sequence_order(self):
        @node(output_name="a")
        def step_a(x: int) -> int:
            return x + 1

        @node(output_name="b")
        def step_b(a: int) -> int:
            return a + 1

        graph = Graph([step_a, step_b])
        runner = SyncRunner()
        lp = ListProcessor()

        runner.run(graph, {"x": 0}, event_processors=[lp])

        types = lp.event_types()
        assert types == [
            "RunStartEvent",
            "NodeStartEvent",
            "NodeEndEvent",
            "NodeStartEvent",
            "NodeEndEvent",
            "RunEndEvent",
        ]

    def test_span_hierarchy(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x

        graph = Graph([step])
        runner = SyncRunner()
        lp = ListProcessor()

        runner.run(graph, {"x": 1}, event_processors=[lp])

        run_start = lp.of_type(RunStartEvent)[0]
        node_start = lp.of_type(NodeStartEvent)[0]
        # Node's parent_span_id should be the run's span_id
        assert node_start.parent_span_id == run_start.span_id
        # Run's parent_span_id should be None (top-level)
        assert run_start.parent_span_id is None

    def test_run_id_consistent_across_events(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x

        graph = Graph([step])
        runner = SyncRunner()
        lp = ListProcessor()

        runner.run(graph, {"x": 1}, event_processors=[lp])

        run_ids = {e.run_id for e in lp.events}
        assert len(run_ids) == 1  # All events share one run_id


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorEvents:
    def test_node_error_emitted(self):
        @node(output_name="out")
        def failing(x: int) -> int:
            raise ValueError("boom")

        graph = Graph([failing])
        runner = SyncRunner()
        lp = ListProcessor()

        result = runner.run(graph, {"x": 1}, event_processors=[lp])

        assert result.status.value == "failed"
        errors = lp.of_type(NodeErrorEvent)
        assert len(errors) == 1
        assert errors[0].node_name == "failing"
        assert "boom" in errors[0].error
        assert "ValueError" in errors[0].error_type

    def test_run_end_failed_on_error(self):
        @node(output_name="out")
        def failing(x: int) -> int:
            raise ValueError("boom")

        graph = Graph([failing])
        runner = SyncRunner()
        lp = ListProcessor()

        runner.run(graph, {"x": 1}, event_processors=[lp])

        run_end = lp.of_type(RunEndEvent)[0]
        assert run_end.status == "failed"
        assert "boom" in run_end.error

    def test_processor_failure_does_not_break_execution(self):
        class BadProcessor(EventProcessor):
            def on_event(self, event):
                raise RuntimeError("processor bug")

        @node(output_name="out")
        def step(x: int) -> int:
            return x * 2

        graph = Graph([step])
        runner = SyncRunner()
        good = ListProcessor()

        result = runner.run(graph, {"x": 5}, event_processors=[BadProcessor(), good])

        assert result["out"] == 10
        assert len(good.events) > 0


# ---------------------------------------------------------------------------
# Routing events
# ---------------------------------------------------------------------------


class TestRoutingEvents:
    def test_route_decision_emitted(self):
        @node(output_name="count")
        def increment(count: int) -> int:
            return count + 1

        @route(targets=["increment", END])
        def check(count: int) -> str:
            return END if count >= 1 else "increment"

        graph = Graph([increment, check])
        runner = SyncRunner()
        lp = ListProcessor()

        result = runner.run(graph, {"count": 0}, event_processors=[lp])

        assert result.status.value == "completed", f"Run failed: {result.error}"
        decisions = lp.of_type(RouteDecisionEvent)
        assert len(decisions) >= 1, f"No route decisions. All events: {[(type(e).__name__, getattr(e, 'node_name', '-')) for e in lp.events]}"
        assert decisions[0].node_name == "check"
        # Last decision should be END (count >= 1)
        assert decisions[-1].decision == END


# ---------------------------------------------------------------------------
# Cyclic graph
# ---------------------------------------------------------------------------


class TestCyclicGraphEvents:
    def test_cyclic_graph_emits_multiple_node_events(self):
        @node(output_name="count")
        def increment(count: int) -> int:
            return count + 1

        @route(targets=["increment", END])
        def check(count: int) -> str:
            return END if count >= 3 else "increment"

        graph = Graph([increment, check])
        runner = SyncRunner()
        lp = ListProcessor()

        runner.run(graph, {"count": 0}, event_processors=[lp])

        node_starts = lp.of_type(NodeStartEvent)
        # Multiple iterations: increment and check run multiple times
        assert len(node_starts) > 2

        decisions = lp.of_type(RouteDecisionEvent)
        assert len(decisions) >= 1


# ---------------------------------------------------------------------------
# Nested graph
# ---------------------------------------------------------------------------


class TestNestedGraphEvents:
    def test_nested_graph_emits_inner_run_events(self):
        @node(output_name="inner_out")
        def inner_step(x: int) -> int:
            return x * 2

        inner = Graph([inner_step], name="inner")

        @node(output_name="final")
        def outer_step(inner_out: int) -> int:
            return inner_out + 1

        outer = Graph([inner.as_node(), outer_step], name="outer")
        runner = SyncRunner()
        lp = ListProcessor()

        result = runner.run(outer, {"x": 5}, event_processors=[lp])

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

    def test_nested_graph_inner_events_have_different_run_id(self):
        @node(output_name="inner_out")
        def inner_step(x: int) -> int:
            return x * 2

        inner = Graph([inner_step], name="inner")
        outer = Graph([inner.as_node()], name="outer")
        runner = SyncRunner()
        lp = ListProcessor()

        runner.run(outer, {"x": 5}, event_processors=[lp])

        run_starts = lp.of_type(RunStartEvent)
        assert len(run_starts) == 2
        # Inner and outer have different run_ids
        assert run_starts[0].run_id != run_starts[1].run_id


# ---------------------------------------------------------------------------
# Map events
# ---------------------------------------------------------------------------


class TestMapEvents:
    def test_map_emits_map_run_start(self):
        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        graph = Graph([double])
        runner = SyncRunner()
        lp = ListProcessor()

        results = runner.map(
            graph, {"x": [1, 2, 3]}, map_over="x", event_processors=[lp]
        )

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

    def test_map_emits_run_end(self):
        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        graph = Graph([double])
        runner = SyncRunner()
        lp = ListProcessor()

        runner.map(graph, {"x": [1, 2]}, map_over="x", event_processors=[lp])

        run_ends = lp.of_type(RunEndEvent)
        # 1 map-level RunEnd + 2 individual RunEnds
        assert len(run_ends) == 3


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_called_on_top_level_run(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x

        graph = Graph([step])
        runner = SyncRunner()
        lp = ListProcessor()

        runner.run(graph, {"x": 1}, event_processors=[lp])
        assert lp.shutdown_called

    def test_shutdown_called_on_top_level_map(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x

        graph = Graph([step])
        runner = SyncRunner()
        lp = ListProcessor()

        runner.map(graph, {"x": [1, 2]}, map_over="x", event_processors=[lp])
        assert lp.shutdown_called


# ---------------------------------------------------------------------------
# No processors (backwards compatibility)
# ---------------------------------------------------------------------------


class TestNoProcessors:
    def test_run_without_processors(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x * 2

        graph = Graph([step])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})
        assert result["out"] == 10

    def test_map_without_processors(self):
        @node(output_name="out")
        def step(x: int) -> int:
            return x * 2

        graph = Graph([step])
        runner = SyncRunner()

        results = runner.map(graph, {"x": [1, 2]}, map_over="x")
        assert len(results) == 2
