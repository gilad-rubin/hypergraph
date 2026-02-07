"""Tests for AsyncRunner."""

import asyncio
import time

import pytest

from hypergraph import Graph, node
from hypergraph.exceptions import InfiniteLoopError, MissingInputError
from hypergraph.nodes.gate import route, END
from hypergraph.runners import RunStatus, AsyncRunner
from hypergraph.runners._shared import template_async as template_async_module


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

    async def test_run_accepts_kwargs_inputs(self):
        """kwargs can be used instead of values dict."""
        graph = Graph([add])
        runner = AsyncRunner()

        result = await runner.run(graph, a=10, b=20)

        assert result["sum"] == 30

    async def test_run_merges_values_and_kwargs(self):
        """values and kwargs are merged when keys are disjoint."""
        graph = Graph([add])
        runner = AsyncRunner()

        result = await runner.run(graph, {"a": 10}, b=20)

        assert result["sum"] == 30

    async def test_run_duplicate_values_and_kwargs_raises(self):
        """Duplicate keys across values and kwargs are rejected."""
        graph = Graph([double])
        runner = AsyncRunner()

        with pytest.raises(ValueError, match="both values and kwargs"):
            await runner.run(graph, {"x": 1}, x=2)

    async def test_run_nested_dict_input_with_kwargs(self):
        """Nested dict values pass through unchanged."""

        @node(output_name="top_k")
        def pick_top_k(processor: dict[str, int]) -> int:
            return processor["top_k"]

        graph = Graph([pick_top_k])
        runner = AsyncRunner()

        result = await runner.run(graph, processor={"top_k": 5})

        assert result["top_k"] == 5

    async def test_run_input_named_select_requires_values_dict(self):
        """Input names matching options are only accepted via values dict."""

        @node(output_name="result")
        def echo_select(select: str) -> str:
            return select

        graph = Graph([echo_select])
        runner = AsyncRunner()

        result = await runner.run(graph, values={"select": "fast"}, select=["result"])

        assert result["result"] == "fast"

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
        graph = Graph(
            [
                double,
                double2,
                add.with_inputs(a="doubled", b="doubled2"),
            ]
        )
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert result["sum"] == 20

    async def test_diamond_graph(self):
        """Diamond-shaped graph."""
        double2 = double.with_name("double2").with_outputs(doubled="other")
        graph = Graph(
            [
                increment,
                double.with_inputs(x="incremented"),
                double2.with_inputs(x="incremented"),
                add.with_inputs(a="doubled", b="other"),
            ]
        )
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
        timestamps = {}

        @node(output_name="r1")
        async def timed1(x: int) -> int:
            timestamps["s1_start"] = time.monotonic()
            await asyncio.sleep(0.05)
            timestamps["s1_end"] = time.monotonic()
            return x

        @node(output_name="r2")
        async def timed2(x: int) -> int:
            timestamps["s2_start"] = time.monotonic()
            await asyncio.sleep(0.05)
            timestamps["s2_end"] = time.monotonic()
            return x

        graph = Graph([timed1, timed2])
        runner = AsyncRunner()
        result = await runner.run(graph, {"x": 5})

        # Verify execution windows overlap (concurrent, not sequential)
        assert timestamps["s1_start"] < timestamps["s2_end"]
        assert timestamps["s2_start"] < timestamps["s1_end"]
        assert result["r1"] == 5
        assert result["r2"] == 5

    async def test_max_concurrency_limits_parallelism(self):
        """max_concurrency limits parallel execution."""
        slow1 = slow_node.with_name("slow1").with_outputs(result="r1")
        slow2 = slow_node.with_name("slow2").with_outputs(result="r2")

        graph = Graph([slow1, slow2])
        runner = AsyncRunner()

        start = time.time()
        result = await runner.run(graph, {"x": 5, "delay": 0.05}, max_concurrency=1)
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

        @route(targets=["counter_stop", END])
        def cycle_gate(count: int, limit: int = 10) -> str:
            return END if count >= limit else "counter_stop"

        graph = Graph([counter_stop, cycle_gate])
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

        @route(targets=["async_counter_stop", END])
        def async_cycle_gate(count: int, limit: int = 10) -> str:
            return END if count >= limit else "async_counter_stop"

        graph = Graph([async_counter_stop, async_cycle_gate])
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

    async def test_map_accepts_kwargs_inputs(self):
        """map supports kwargs shorthand for input values."""
        graph = Graph([double])
        runner = AsyncRunner()

        results = await runner.map(graph, map_over="x", x=[1, 2, 3])

        assert [r["doubled"] for r in results] == [2, 4, 6]

    async def test_map_merges_values_and_kwargs(self):
        """map merges values dict with kwargs when keys are disjoint."""
        graph = Graph([add])
        runner = AsyncRunner()

        results = await runner.map(
            graph,
            {"a": [1, 2]},
            map_over=["a", "b"],
            b=[10, 20],
        )

        assert [r["sum"] for r in results] == [11, 22]

    async def test_map_duplicate_values_and_kwargs_raises(self):
        """map rejects duplicate keys across values and kwargs."""
        graph = Graph([double])
        runner = AsyncRunner()

        with pytest.raises(ValueError, match="both values and kwargs"):
            await runner.map(graph, {"x": [1, 2]}, map_over="x", x=[3, 4])

    async def test_map_input_named_map_over_requires_values_dict(self):
        """Input names matching map options must be passed via values dict."""

        @node(output_name="sum")
        def add_with_reserved_name(x: int, map_over: int) -> int:
            return x + map_over

        graph = Graph([add_with_reserved_name])
        runner = AsyncRunner()

        results = await runner.map(
            graph,
            values={"x": [1, 2], "map_over": 10},
            map_over="x",
        )

        assert [r["sum"] for r in results] == [11, 12]

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

    async def test_map_continue_handles_item_exceptions(self):
        """continue mode returns FAILED results when per-item run raises."""

        @node(output_name="sum")
        def needs_two_inputs(x: int, y: int) -> int:
            return x + y

        graph = Graph([needs_two_inputs])
        runner = AsyncRunner()

        results = await runner.map(
            graph,
            {"x": [1, 2, 3]},
            map_over="x",
            error_handling="continue",
        )

        assert len(results) == 3
        assert all(r.status == RunStatus.FAILED for r in results)
        assert all(isinstance(r.error, MissingInputError) for r in results)

    async def test_map_unbounded_task_guard_raises(self, monkeypatch):
        """Protect against large unbounded fan-out without max_concurrency."""
        graph = Graph([double])
        runner = AsyncRunner()

        monkeypatch.setattr(template_async_module, "MAX_UNBOUNDED_MAP_TASKS", 2)

        with pytest.raises(ValueError, match="Too many map tasks"):
            await runner.map(graph, {"x": [1, 2, 3]}, map_over="x")


class TestDisconnectedSubgraphs:
    """Tests for disconnected graphs with AsyncRunner (GAP-09)."""

    async def test_disconnected_subgraphs_run_concurrently(self):
        """Independent subgraphs execute in parallel with AsyncRunner."""
        timestamps: dict[str, float] = {}

        @node(output_name="a")
        async def slow_a(x: int) -> int:
            timestamps["a_start"] = time.monotonic()
            await asyncio.sleep(0.03)
            timestamps["a_end"] = time.monotonic()
            return x * 2

        @node(output_name="b")
        async def slow_b(y: int) -> int:
            timestamps["b_start"] = time.monotonic()
            await asyncio.sleep(0.03)
            timestamps["b_end"] = time.monotonic()
            return y * 3

        # Two disconnected subgraphs - no edges between them
        graph = Graph([slow_a, slow_b])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5, "y": 10})

        assert result.status == RunStatus.COMPLETED
        assert result["a"] == 10
        assert result["b"] == 30
        assert timestamps["a_start"] < timestamps["b_end"]
        assert timestamps["b_start"] < timestamps["a_end"]

    async def test_select_from_disconnected_subgraph(self):
        """select= works correctly with disconnected graphs."""

        @node(output_name="a")
        async def subgraph_a(x: int) -> int:
            return x * 2

        @node(output_name="b")
        async def subgraph_b(y: int) -> int:
            return y * 3

        graph = Graph([subgraph_a, subgraph_b])
        runner = AsyncRunner()

        # Select only from one subgraph
        result = await runner.run(graph, {"x": 5, "y": 10}, select=["a"])

        assert result.status == RunStatus.COMPLETED
        assert "a" in result
        assert "b" not in result
        assert result["a"] == 10

    async def test_deeply_nested_async_three_levels(self):
        """3+ levels of GraphNode nesting with async nodes."""

        @node(output_name="x")
        async def level3_node(a: int) -> int:
            await asyncio.sleep(0.01)
            return a * 2

        level3 = Graph([level3_node], name="level3")

        @node(output_name="y")
        async def level2_node(x: int) -> int:
            await asyncio.sleep(0.01)
            return x + 1

        level2 = Graph([level3.as_node(), level2_node], name="level2")

        @node(output_name="z")
        async def level1_node(y: int) -> int:
            await asyncio.sleep(0.01)
            return y * 3

        level1 = Graph([level2.as_node(), level1_node])

        runner = AsyncRunner()
        result = await runner.run(level1, {"a": 5})

        # a=5 -> x=10 -> y=11 -> z=33
        assert result.status == RunStatus.COMPLETED
        assert result["x"] == 10
        assert result["y"] == 11
        assert result["z"] == 33

    async def test_multiple_disconnected_chains(self):
        """Multiple disconnected chains run concurrently."""

        @node(output_name="a1")
        async def chain_a_step1(input_a: int) -> int:
            await asyncio.sleep(0.01)
            return input_a + 1

        @node(output_name="a2")
        async def chain_a_step2(a1: int) -> int:
            await asyncio.sleep(0.01)
            return a1 * 2

        @node(output_name="b1")
        async def chain_b_step1(input_b: int) -> int:
            await asyncio.sleep(0.01)
            return input_b + 10

        @node(output_name="b2")
        async def chain_b_step2(b1: int) -> int:
            await asyncio.sleep(0.01)
            return b1 * 3

        # Two independent chains
        graph = Graph([chain_a_step1, chain_a_step2, chain_b_step1, chain_b_step2])
        runner = AsyncRunner()

        result = await runner.run(graph, {"input_a": 5, "input_b": 2})

        assert result.status == RunStatus.COMPLETED
        # Chain A: 5 -> 6 -> 12
        assert result["a1"] == 6
        assert result["a2"] == 12
        # Chain B: 2 -> 12 -> 36
        assert result["b1"] == 12
        assert result["b2"] == 36

    async def test_mixed_connected_disconnected(self):
        """Graph with both connected and disconnected parts."""

        @node(output_name="a")
        async def node_a(x: int) -> int:
            return x * 2

        @node(output_name="b")
        async def node_b(a: int) -> int:
            return a + 1

        @node(output_name="c")
        async def node_c(y: int) -> int:
            return y * 3

        # a -> b is connected, c is disconnected
        graph = Graph([node_a, node_b, node_c])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5, "y": 10})

        assert result.status == RunStatus.COMPLETED
        assert result["a"] == 10
        assert result["b"] == 11
        assert result["c"] == 30

    async def test_disconnected_with_nested_graphnode(self):
        """Disconnected subgraphs where one contains a nested GraphNode."""

        @node(output_name="inner_result")
        async def inner_node(a: int) -> int:
            return a * 2

        inner = Graph([inner_node], name="inner")

        @node(output_name="other_result")
        async def other_node(b: int) -> int:
            return b + 10

        # inner.as_node() and other_node are disconnected
        outer = Graph([inner.as_node(), other_node])
        runner = AsyncRunner()

        result = await runner.run(outer, {"a": 5, "b": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["inner_result"] == 10
        assert result["other_result"] == 13


class TestDeeplyNestedAsync:
    """Additional tests for deeply nested async execution."""

    async def test_four_level_nesting(self):
        """Four levels of GraphNode nesting with async."""

        @node(output_name="l4")
        async def level4(x: int) -> int:
            return x + 1

        l4_graph = Graph([level4], name="l4")

        @node(output_name="l3")
        async def level3(l4: int) -> int:
            return l4 + 1

        l3_graph = Graph([l4_graph.as_node(), level3], name="l3")

        @node(output_name="l2")
        async def level2(l3: int) -> int:
            return l3 + 1

        l2_graph = Graph([l3_graph.as_node(), level2], name="l2")

        @node(output_name="l1")
        async def level1(l2: int) -> int:
            return l2 + 1

        l1_graph = Graph([l2_graph.as_node(), level1])

        runner = AsyncRunner()
        result = await runner.run(l1_graph, {"x": 0})

        # 0 -> 1 -> 2 -> 3 -> 4
        assert result.status == RunStatus.COMPLETED
        assert result["l4"] == 1
        assert result["l3"] == 2
        assert result["l2"] == 3
        assert result["l1"] == 4

    async def test_nested_with_parallel_inner_nodes(self):
        """Nested graph with parallel nodes inside."""

        @node(output_name="a")
        async def inner_a(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 2

        @node(output_name="b")
        async def inner_b(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 3

        @node(output_name="sum")
        async def inner_combine(a: int, b: int) -> int:
            return a + b

        inner = Graph([inner_a, inner_b, inner_combine], name="inner")
        outer = Graph([inner.as_node()])

        runner = AsyncRunner()
        result = await runner.run(outer, {"x": 5})

        # a=10, b=15, sum=25
        assert result.status == RunStatus.COMPLETED
        assert result["a"] == 10
        assert result["b"] == 15
        assert result["sum"] == 25


class TestGlobalConcurrencyLimit:
    """Tests for global max_concurrency shared across all execution levels.

    The max_concurrency limit should be shared across:
    - All map items
    - All nested graphs
    - All nodes at all levels
    """

    async def test_nested_graph_shares_concurrency_limit(self):
        """Nested graphs share the parent's concurrency limit."""
        # Track concurrent operations
        concurrent_count = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        @node(output_name="inner_result")
        async def inner_slow(x: int) -> int:
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1
            return x * 2

        inner = Graph([inner_slow], name="inner")

        @node(output_name="outer_result")
        async def outer_slow(y: int) -> int:
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1
            return y + 1

        # Both inner graph and outer node should share the concurrency limit
        outer = Graph([inner.as_node(), outer_slow])
        runner = AsyncRunner()

        result = await runner.run(outer, {"x": 5, "y": 10}, max_concurrency=1)

        assert result.status == RunStatus.COMPLETED
        # With max_concurrency=1, only one operation should run at a time
        assert max_concurrent == 1

    async def test_map_shares_concurrency_across_items(self):
        """Map operation shares concurrency limit across all items."""
        concurrent_count = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        @node(output_name="result")
        async def tracked_slow(x: int) -> int:
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1
            return x * 2

        graph = Graph([tracked_slow])
        runner = AsyncRunner()

        # 5 items but max_concurrency=2
        results = await runner.map(
            graph,
            {"x": [1, 2, 3, 4, 5]},
            map_over="x",
            max_concurrency=2,
        )

        assert len(results) == 5
        # Should never exceed the concurrency limit
        assert max_concurrent <= 2

    async def test_nested_map_shares_global_concurrency(self):
        """GraphNode with map_over shares concurrency with parent graph."""
        concurrent_count = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        @node(output_name="inner_result")
        async def inner_tracked(item: int) -> int:
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1
            return item * 2

        inner = Graph([inner_tracked], name="inner")
        inner_mapped = inner.as_node().map_over("item")

        @node(output_name="outer_result")
        async def outer_tracked(x: int) -> int:
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1
            return x + 100

        outer = Graph([inner_mapped, outer_tracked])
        runner = AsyncRunner()

        result = await runner.run(
            outer,
            {"item": [1, 2, 3], "x": 5},
            max_concurrency=2,
        )

        assert result.status == RunStatus.COMPLETED
        # All operations (outer node + 3 inner map items) share the limit
        assert max_concurrent <= 2

    async def test_deeply_nested_shares_concurrency(self):
        """Three levels of nesting all share the same concurrency limit."""
        concurrent_count = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        @node(output_name="l3")
        async def level3(a: int) -> int:
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1
            return a * 2

        l3_graph = Graph([level3], name="l3")

        @node(output_name="l2")
        async def level2(l3: int) -> int:
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1
            return l3 + 1

        l2_graph = Graph([l3_graph.as_node(), level2], name="l2")

        @node(output_name="l1")
        async def level1(l2: int) -> int:
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1
            return l2 * 3

        l1_graph = Graph([l2_graph.as_node(), level1])

        runner = AsyncRunner()
        result = await runner.run(l1_graph, {"a": 5}, max_concurrency=1)

        assert result.status == RunStatus.COMPLETED
        # All three levels share max_concurrency=1
        assert max_concurrent == 1

    async def test_map_with_nested_graph_shares_concurrency(self):
        """runner.map() with nested GraphNodes shares global concurrency."""
        concurrent_count = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        @node(output_name="inner_result")
        async def inner_tracked(x: int) -> int:
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1
            return x * 2

        inner = Graph([inner_tracked], name="inner")
        outer = Graph([inner.as_node()])

        runner = AsyncRunner()

        # 4 map items, each with a nested graph
        results = await runner.map(
            outer,
            {"x": [1, 2, 3, 4]},
            map_over="x",
            max_concurrency=2,
        )

        assert len(results) == 4
        # All 4 map items * nested graph operations share the limit
        assert max_concurrent <= 2

    async def test_concurrency_limit_not_inherited_when_not_set(self):
        """When max_concurrency is not set, no limit is applied."""
        concurrent_count = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        @node(output_name="result")
        async def tracked_node(x: int) -> int:
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1
            return x

        # 4 independent nodes
        nodes = [
            tracked_node.with_name(f"n{i}").with_inputs(x=f"x{i}").with_outputs(result=f"r{i}")
            for i in range(4)
        ]
        graph = Graph(nodes)

        runner = AsyncRunner()
        result = await runner.run(graph, {f"x{i}": i for i in range(4)})

        assert result.status == RunStatus.COMPLETED
        # Without limit, all 4 should run concurrently
        assert max_concurrent == 4

    async def test_mixed_sync_async_respects_concurrency(self):
        """Mix of sync and async nodes with concurrency limit.

        Note: Sync functions execute immediately without acquiring the semaphore.
        Only async operations are limited by max_concurrency.
        """
        async_concurrent = 0
        max_async_concurrent = 0
        lock = asyncio.Lock()

        @node(output_name="sync_result")
        def sync_node(x: int) -> int:
            # Sync functions don't block on semaphore
            return x * 2

        @node(output_name="async_result")
        async def async_tracked(y: int) -> int:
            nonlocal async_concurrent, max_async_concurrent
            async with lock:
                async_concurrent += 1
                max_async_concurrent = max(max_async_concurrent, async_concurrent)
            await asyncio.sleep(0.02)
            async with lock:
                async_concurrent -= 1
            return y + 1

        # Two sync nodes and two async nodes (all independent)
        sync1 = sync_node.with_name("sync1").with_inputs(x="x1").with_outputs(sync_result="s1")
        sync2 = sync_node.with_name("sync2").with_inputs(x="x2").with_outputs(sync_result="s2")
        async1 = async_tracked.with_name("async1").with_inputs(y="y1").with_outputs(async_result="a1")
        async2 = async_tracked.with_name("async2").with_inputs(y="y2").with_outputs(async_result="a2")

        graph = Graph([sync1, sync2, async1, async2])
        runner = AsyncRunner()

        result = await runner.run(
            graph,
            {"x1": 1, "x2": 2, "y1": 10, "y2": 20},
            max_concurrency=1,
        )

        assert result.status == RunStatus.COMPLETED
        assert result["s1"] == 2
        assert result["s2"] == 4
        assert result["a1"] == 11
        assert result["a2"] == 21
        # Async nodes should respect the concurrency limit
        assert max_async_concurrent == 1
