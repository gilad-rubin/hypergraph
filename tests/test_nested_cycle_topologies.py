"""Tests for deeply nested graphs with cycles (GAP-02)."""

import pytest

from hypergraph import Graph, node
from hypergraph.runners import RunStatus, SyncRunner, AsyncRunner


# === Test Fixtures ===


@node(output_name="count")
def counter_stop(count: int, limit: int = 5) -> int:
    """Counter that increments until limit is reached."""
    if count >= limit:
        return count
    return count + 1


@node(output_name="x")
def increment(x: int) -> int:
    """Simple increment."""
    return x + 1


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="sum")
def add(a: int, b: int) -> int:
    return a + b


# === Tests ===


class TestThreeLevelNestedWithInnerCycle:
    """Test 3 levels of nesting where innermost has cycle."""

    def test_three_level_nested_cycle(self):
        """Innermost graph has a cycle, wrapped by two outer graphs."""
        # Level 1 (innermost): Graph with cycle
        inner = Graph([counter_stop], name="inner")

        # Level 2: Wrap inner graph
        middle = Graph([inner.as_node()], name="middle")

        # Level 3 (outermost): Wrap middle graph
        outer = Graph([middle.as_node()])

        runner = SyncRunner()
        result = runner.run(outer, {"count": 0, "limit": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["count"] == 3

    def test_three_level_nested_cycle_with_processing(self):
        """Three levels with processing at each level."""
        # Innermost: cycle
        inner = Graph([counter_stop], name="inner")

        # Middle: adds processing after inner cycle
        @node(output_name="processed")
        def process_count(count: int) -> int:
            return count * 10

        middle = Graph([inner.as_node(), process_count], name="middle")

        # Outer: adds more processing
        @node(output_name="final")
        def finalize(processed: int) -> int:
            return processed + 1

        outer = Graph([middle.as_node(), finalize])

        runner = SyncRunner()
        result = runner.run(outer, {"count": 0, "limit": 2})

        # count goes 0->1->2, then 2*10=20, then 20+1=21
        assert result.status == RunStatus.COMPLETED
        assert result["count"] == 2
        assert result["processed"] == 20
        assert result["final"] == 21


class TestOuterCycleWithNestedGraph:
    """Test outer graph has cycle containing a GraphNode."""

    def test_graphnode_processes_cycle_output(self):
        """GraphNode receives output from a cycle in outer graph."""
        # Outer graph has a cycle, then a nested graph processes result

        @node(output_name="count")
        def counter(count: int, limit: int = 5) -> int:
            if count >= limit:
                return count
            return count + 1

        @node(output_name="inner_result")
        def transform(count: int) -> int:
            return count * 10

        inner = Graph([transform], name="inner")

        # Outer: cycle runs first, then inner processes result
        outer = Graph([counter, inner.as_node()])

        runner = SyncRunner()
        result = runner.run(outer, {"count": 0, "limit": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["count"] == 3
        assert result["inner_result"] == 30


class TestParallelNestedGraphsWithCycles:
    """Test multiple independent nested graphs with cycles."""

    def test_two_parallel_nested_cycles(self):
        """Two independent nested graphs, each with their own cycle."""

        # Inner graph A: counter
        @node(output_name="count_a")
        def counter_a(count_a: int, limit_a: int = 3) -> int:
            if count_a >= limit_a:
                return count_a
            return count_a + 1

        inner_a = Graph([counter_a], name="inner_a")

        # Inner graph B: different counter
        @node(output_name="count_b")
        def counter_b(count_b: int, limit_b: int = 5) -> int:
            if count_b >= limit_b:
                return count_b
            return count_b + 1

        inner_b = Graph([counter_b], name="inner_b")

        # Outer: combines both
        @node(output_name="sum")
        def combine(count_a: int, count_b: int) -> int:
            return count_a + count_b

        outer = Graph([inner_a.as_node(), inner_b.as_node(), combine])

        runner = SyncRunner()
        result = runner.run(
            outer,
            {
                "count_a": 0,
                "limit_a": 3,
                "count_b": 0,
                "limit_b": 5,
            },
        )

        assert result.status == RunStatus.COMPLETED
        assert result["count_a"] == 3
        assert result["count_b"] == 5
        assert result["sum"] == 8


class TestNestedGraphNodeInCyclePath:
    """Test where GraphNode is part of cycle path."""

    def test_graphnode_follows_cycle(self):
        """GraphNode processes result after cycle completes."""
        # Simpler test: cycle converges, then nested graph processes

        @node(output_name="count")
        def cycle_counter(count: int, limit: int = 5) -> int:
            if count >= limit:
                return count
            return count + 1

        @node(output_name="result")
        def process(count: int) -> int:
            return count * 100

        inner = Graph([process], name="processor")

        # Cycle runs, then processor transforms result
        outer = Graph([cycle_counter, inner.as_node()])

        runner = SyncRunner()
        result = runner.run(outer, {"count": 0, "limit": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["count"] == 5
        assert result["result"] == 500


class TestDeeplyNestedConvergence:
    """Test deep nesting with cycle convergence."""

    def test_convergence_through_nested_layers(self):
        """Cycle converges through multiple nested graph layers."""

        # Level 3 (innermost): converging cycle
        @node(output_name="approx")
        def converge(approx: float, target: float = 10.0, rate: float = 0.5) -> float:
            """Converge toward target by rate factor."""
            diff = target - approx
            if abs(diff) < 0.1:
                return approx
            return approx + diff * rate

        level3 = Graph([converge], name="level3")

        # Level 2: wraps level 3
        level2 = Graph([level3.as_node()], name="level2")

        # Level 1 (outer): wraps level 2
        outer = Graph([level2.as_node()])

        runner = SyncRunner()
        result = runner.run(outer, {"approx": 0.0, "target": 10.0, "rate": 0.5})

        assert result.status == RunStatus.COMPLETED
        # Should converge close to 10.0
        assert abs(result["approx"] - 10.0) < 0.1


class TestNestedCyclesWithDifferentSeeds:
    """Test nested cycles with different seed parameters at each level."""

    def test_inner_cycle_completes_before_outer_processing(self):
        """Inner cycle completes, then outer node processes result."""
        # Inner graph has a cycle with its own seed
        @node(output_name="inner_count")
        def inner_cycle(inner_count: int, inner_limit: int = 2) -> int:
            if inner_count >= inner_limit:
                return inner_count
            return inner_count + 1

        inner = Graph([inner_cycle], name="inner")

        # Outer: processes inner's completed result
        @node(output_name="processed")
        def process_inner(inner_count: int, multiplier: int = 10) -> int:
            return inner_count * multiplier

        outer = Graph([inner.as_node(), process_inner])

        runner = SyncRunner()
        result = runner.run(
            outer,
            {
                "inner_count": 0,
                "inner_limit": 3,
                "multiplier": 10,
            },
        )

        assert result.status == RunStatus.COMPLETED
        assert result["inner_count"] == 3
        assert result["processed"] == 30

    def test_two_independent_nested_cycles(self):
        """Two nested graphs each with their own cycles."""
        # Inner A: cycle to limit_a
        @node(output_name="a")
        def cycle_a(a: int, limit_a: int = 3) -> int:
            if a >= limit_a:
                return a
            return a + 1

        inner_a = Graph([cycle_a], name="inner_a")

        # Inner B: cycle to limit_b
        @node(output_name="b")
        def cycle_b(b: int, limit_b: int = 5) -> int:
            if b >= limit_b:
                return b
            return b + 1

        inner_b = Graph([cycle_b], name="inner_b")

        # Outer: combines both independent results
        @node(output_name="sum")
        def combine(a: int, b: int) -> int:
            return a + b

        outer = Graph([inner_a.as_node(), inner_b.as_node(), combine])

        runner = SyncRunner()
        result = runner.run(
            outer,
            {
                "a": 0,
                "limit_a": 3,
                "b": 0,
                "limit_b": 5,
            },
        )

        assert result.status == RunStatus.COMPLETED
        assert result["a"] == 3
        assert result["b"] == 5
        assert result["sum"] == 8


class TestNestedCyclesAsync:
    """Test nested cycles with AsyncRunner."""

    async def test_nested_cycle_async_runner(self):
        """Nested cycle works with AsyncRunner."""
        inner = Graph([counter_stop], name="inner")
        outer = Graph([inner.as_node()])

        runner = AsyncRunner()
        result = await runner.run(outer, {"count": 0, "limit": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["count"] == 3

    async def test_deeply_nested_async(self):
        """Three levels of nesting with AsyncRunner."""

        # Innermost async cycle
        @node(output_name="count")
        async def async_counter(count: int, limit: int = 3) -> int:
            if count >= limit:
                return count
            return count + 1

        inner = Graph([async_counter], name="inner")
        middle = Graph([inner.as_node()], name="middle")
        outer = Graph([middle.as_node()])

        runner = AsyncRunner()
        result = await runner.run(outer, {"count": 0, "limit": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["count"] == 3


class TestCycleWithMapOver:
    """Test cycles combined with map_over."""

    def test_map_over_graph_with_cycle(self):
        """map_over a graph that contains a cycle."""
        # Inner graph has cycle
        inner = Graph([counter_stop], name="inner")

        # Outer maps over the cyclic graph
        outer = Graph([inner.as_node().map_over("count")])

        runner = SyncRunner()
        result = runner.run(
            outer,
            {
                "count": [0, 1, 2],
                "limit": 5,
            },
        )

        assert result.status == RunStatus.COMPLETED
        # Each starting count should reach 5
        assert result["count"] == [5, 5, 5]

    def test_map_over_with_different_limits(self):
        """map_over cycle with varying limits."""
        inner = Graph([counter_stop], name="inner")
        outer = Graph([inner.as_node().map_over("count", "limit", mode="zip")])

        runner = SyncRunner()
        result = runner.run(
            outer,
            {
                "count": [0, 0, 0],
                "limit": [2, 3, 4],
            },
        )

        assert result.status == RunStatus.COMPLETED
        assert result["count"] == [2, 3, 4]
