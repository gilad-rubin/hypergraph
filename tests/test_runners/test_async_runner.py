"""Tests for AsyncRunner."""

import asyncio
import time

import pytest

from hypergraph import Graph, node
from hypergraph.exceptions import InfiniteLoopError, MissingInputError
from hypergraph.runners._types import RunStatus
from hypergraph.runners.async_ import AsyncRunner


# === Test Fixtures ===


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="incremented")
def increment(x: int) -> int:
    return x + 1


@node(output_name="sum")
def add(a: int, b: int) -> int:
    return a + b


@node(output_name="result")
def with_default(x: int, y: int = 10) -> int:
    return x + y


@node(output_name="count")
def counter_stop(count: int, limit: int = 10) -> int:
    if count >= limit:
        return count
    return count + 1


@node(output_name="doubled")
async def async_double(x: int) -> int:
    return x * 2


@node(output_name="sum")
async def async_add(a: int, b: int) -> int:
    return a + b


@node(output_name="items")
async def async_gen_items(n: int):
    for i in range(n):
        yield i


@node(output_name="result")
async def slow_node(x: int, delay: float = 0.05) -> int:
    await asyncio.sleep(delay)
    return x


# === Tests ===


class TestAsyncRunnerCapabilities:
    """Tests for AsyncRunner capabilities."""

    def test_supports_cycles_true(self):
        runner = AsyncRunner()
        assert runner.capabilities.supports_cycles is True

    def test_supports_async_nodes_true(self):
        runner = AsyncRunner()
        assert runner.capabilities.supports_async_nodes is True

    def test_returns_coroutine_true(self):
        runner = AsyncRunner()
        assert runner.capabilities.returns_coroutine is True


class TestAsyncRunnerRun:
    """Tests for AsyncRunner.run()."""

    # Basic execution

    async def test_single_node_graph(self):
        """Execute graph with single node."""
        graph = Graph([double])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == 10

    async def test_linear_dag(self):
        """Execute linear graph."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5, "b": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == 10
        assert result["sum"] == 13

    async def test_fan_out_graph(self):
        """Multiple nodes consume same input."""

        @node(output_name="tripled")
        def triple(x: int) -> int:
            return x * 3

        graph = Graph([double, triple])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert result["doubled"] == 10
        assert result["tripled"] == 15

    async def test_fan_in_graph(self):
        """Node consumes outputs from multiple nodes."""
        double2 = double.with_name("double2").with_outputs(doubled="doubled2")
        graph = Graph([
            double,
            double2,
            add.with_inputs(a="doubled", b="doubled2"),
        ])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert result["sum"] == 20

    async def test_diamond_graph(self):
        """Diamond-shaped graph."""
        double2 = double.with_name("double2").with_outputs(doubled="other")
        graph = Graph([
            increment,
            double.with_inputs(x="incremented"),
            double2.with_inputs(x="incremented"),
            add.with_inputs(a="doubled", b="other"),
        ])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert result["sum"] == 24

    # Async nodes

    async def test_async_node_awaited(self):
        """Async nodes are properly awaited."""
        graph = Graph([async_double])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert result["doubled"] == 10

    async def test_mixed_sync_async_nodes(self):
        """Graph with both sync and async nodes."""
        graph = Graph([double, async_add.with_inputs(a="doubled")])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5, "b": 3})

        assert result["doubled"] == 10
        assert result["sum"] == 13

    async def test_async_generator_accumulated(self):
        """Async generators are accumulated to list."""
        graph = Graph([async_gen_items])
        runner = AsyncRunner()

        result = await runner.run(graph, {"n": 3})

        assert result["items"] == [0, 1, 2]

    # Concurrency

    async def test_parallel_nodes_run_concurrently(self):
        """Independent nodes run concurrently."""
        slow1 = slow_node.with_name("slow1").with_outputs(result="r1")
        slow2 = slow_node.with_name("slow2").with_outputs(result="r2")

        graph = Graph([slow1, slow2])
        runner = AsyncRunner()

        start = time.time()
        result = await runner.run(graph, {"x": 5, "delay": 0.05})
        elapsed = time.time() - start

        # Should be ~0.05s (concurrent), not ~0.1s (sequential)
        assert elapsed < 0.08
        assert result["r1"] == 5
        assert result["r2"] == 5

    async def test_max_concurrency_limits_parallelism(self):
        """max_concurrency limits parallel execution."""
        slow1 = slow_node.with_name("slow1").with_outputs(result="r1")
        slow2 = slow_node.with_name("slow2").with_outputs(result="r2")

        graph = Graph([slow1, slow2])
        runner = AsyncRunner()

        start = time.time()
        result = await runner.run(
            graph, {"x": 5, "delay": 0.05}, max_concurrency=1
        )
        elapsed = time.time() - start

        # With max_concurrency=1, should be sequential (~0.1s)
        assert elapsed >= 0.09

    async def test_concurrency_one_is_sequential(self):
        """max_concurrency=1 forces sequential execution."""
        execution_order = []

        @node(output_name="a")
        async def track_a(x: int) -> int:
            execution_order.append("a_start")
            await asyncio.sleep(0.02)
            execution_order.append("a_end")
            return x

        @node(output_name="b")
        async def track_b(x: int) -> int:
            execution_order.append("b_start")
            await asyncio.sleep(0.02)
            execution_order.append("b_end")
            return x

        graph = Graph([track_a, track_b])
        runner = AsyncRunner()

        await runner.run(graph, {"x": 5}, max_concurrency=1)

        # With sequential execution, one should complete before other starts
        # The exact order depends on iteration, but we should see
        # start/end pairs not interleaved
        assert execution_order[1] in ("a_end", "b_end")

    # Input/output

    async def test_returns_runresult(self):
        """Returns RunResult object."""
        graph = Graph([double])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert hasattr(result, "values")
        assert hasattr(result, "status")
        assert hasattr(result, "run_id")

    async def test_select_filters_outputs(self):
        """Select parameter filters outputs."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5, "b": 3}, select=["sum"])

        assert "sum" in result
        assert "doubled" not in result

    # Cycles

    async def test_cycle_executes_until_stable(self):
        """Cyclic graph runs until outputs stabilize."""
        graph = Graph([counter_stop])
        runner = AsyncRunner()

        result = await runner.run(graph, {"count": 0, "limit": 5})

        assert result["count"] == 5

    async def test_cycle_with_async_nodes(self):
        """Cycles work with async nodes."""

        @node(output_name="count")
        async def async_counter_stop(count: int, limit: int = 10) -> int:
            if count >= limit:
                return count
            return count + 1

        graph = Graph([async_counter_stop])
        runner = AsyncRunner()

        result = await runner.run(graph, {"count": 0, "limit": 5})

        assert result["count"] == 5

    # Nested graphs

    async def test_nested_graph_inherits_runner(self):
        """Nested graph uses same runner."""
        inner = Graph([async_double], name="inner")
        outer = Graph([inner.as_node()])
        runner = AsyncRunner()

        result = await runner.run(outer, {"x": 5})

        assert result["doubled"] == 10

    async def test_nested_sync_graph_in_async(self):
        """Sync inner graph works in async runner."""
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(), async_add.with_inputs(a="doubled")])
        runner = AsyncRunner()

        result = await runner.run(outer, {"x": 5, "b": 3})

        assert result["sum"] == 13

    # Errors

    async def test_missing_input_raises(self):
        """Missing required input raises error."""
        graph = Graph([double])
        runner = AsyncRunner()

        with pytest.raises(MissingInputError):
            await runner.run(graph, {})

    async def test_node_exception_propagates(self):
        """Node exceptions result in FAILED status."""

        @node(output_name="result")
        async def failing(x: int) -> int:
            raise ValueError("intentional error")

        graph = Graph([failing])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, ValueError)


class TestAsyncRunnerMap:
    """Tests for AsyncRunner.map()."""

    async def test_map_over_single_param(self):
        """Map over a single parameter."""
        graph = Graph([double])
        runner = AsyncRunner()

        results = await runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

        assert len(results) == 3
        assert results[0]["doubled"] == 2
        assert results[1]["doubled"] == 4
        assert results[2]["doubled"] == 6

    async def test_map_runs_concurrently(self):
        """Map executions run concurrently."""
        graph = Graph([slow_node])
        runner = AsyncRunner()

        start = time.time()
        results = await runner.map(
            graph,
            {"x": [1, 2, 3], "delay": 0.05},
            map_over="x",
        )
        elapsed = time.time() - start

        # 3 executions, each 0.05s, should be ~0.05s concurrent, not ~0.15s
        assert elapsed < 0.1
        assert len(results) == 3

    async def test_map_respects_max_concurrency(self):
        """Map respects max_concurrency."""
        graph = Graph([slow_node])
        runner = AsyncRunner()

        start = time.time()
        results = await runner.map(
            graph,
            {"x": [1, 2, 3], "delay": 0.05},
            map_over="x",
            max_concurrency=1,
        )
        elapsed = time.time() - start

        # Sequential: ~0.15s
        assert elapsed >= 0.14
        assert len(results) == 3

    async def test_map_with_async_nodes(self):
        """Map works with async nodes."""
        graph = Graph([async_double])
        runner = AsyncRunner()

        results = await runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

        values = [r["doubled"] for r in results]
        assert values == [2, 4, 6]

    async def test_zip_mode(self):
        """Zip mode iterates in parallel."""
        graph = Graph([add])
        runner = AsyncRunner()

        results = await runner.map(
            graph,
            {"a": [1, 2, 3], "b": [10, 20, 30]},
            map_over=["a", "b"],
            map_mode="zip",
        )

        sums = [r["sum"] for r in results]
        assert sums == [11, 22, 33]

    async def test_product_mode(self):
        """Product mode generates cartesian product."""
        graph = Graph([add])
        runner = AsyncRunner()

        results = await runner.map(
            graph,
            {"a": [1, 2], "b": [10, 20]},
            map_over=["a", "b"],
            map_mode="product",
        )

        assert len(results) == 4
        sums = sorted(r["sum"] for r in results)
        assert sums == [11, 12, 21, 22]
