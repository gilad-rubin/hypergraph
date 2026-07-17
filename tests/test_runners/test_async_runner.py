"""Tests for AsyncRunner."""

import asyncio

import pytest

from hypergraph import Graph, node
from hypergraph.exceptions import MissingInputError
from hypergraph.nodes.gate import END, route
from hypergraph.runners import AsyncRunner, RunStatus
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

    def test_supports_cooperative_timeout_true(self):
        runner = AsyncRunner()
        assert runner.capabilities.supports_cooperative_timeout is True


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
        graph = Graph([double, add.rename_inputs(a="doubled")])
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

    async def test_run_bound_input_outside_active_scope_allowed_as_kwarg(self):
        """Bound input names stay valid kwargs even when inactive after select()."""

        @node(output_name="b_out")
        def make_b(seed: int) -> int:
            return seed + 1

        @node(output_name="c_out")
        def make_c(other: int) -> int:
            return other * 3

        graph = Graph([make_b, make_c]).bind(seed=5).select("c_out")
        runner = AsyncRunner()

        with pytest.warns(UserWarning, match="seed"):
            result = await runner.run(graph, other=2, seed=10)

        assert result["c_out"] == 6

    async def test_run_input_named_select_requires_values_dict(self):
        """Input names matching options are only accepted via values dict."""

        @node(output_name="result")
        def echo_select(select: str) -> str:
            return select

        graph = Graph([echo_select]).select("result")
        runner = AsyncRunner()

        result = await runner.run(graph, values={"select": "fast"})

        assert result["result"] == "fast"

    async def test_run_input_named_map_over_requires_values_dict(self):
        """A map_over input is accepted through values, not kwargs."""

        @node(output_name="result")
        def echo_map_over(map_over: str) -> str:
            return map_over

        graph = Graph([echo_map_over])
        runner = AsyncRunner()

        result = await runner.run(graph, values={"map_over": "items"})

        assert result["result"] == "items"

    async def test_run_rejects_map_over_kwarg(self):
        """run() treats map_over as reserved for runner.map()."""
        graph = Graph([double])
        runner = AsyncRunner()

        with pytest.raises(ValueError, match="runner\\.run\\(\\) does not accept map_over=.*runner\\.map"):
            await runner.run(graph, x=1, map_over="x")

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
        double2 = double.with_name("double2").rename_outputs(doubled="doubled2")
        graph = Graph(
            [
                double,
                double2,
                add.rename_inputs(a="doubled", b="doubled2"),
            ]
        )
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert result["sum"] == 20

    async def test_diamond_graph(self):
        """Diamond-shaped graph."""
        double2 = double.with_name("double2").rename_outputs(doubled="other")
        graph = Graph(
            [
                increment,
                double.rename_inputs(x="incremented"),
                double2.rename_inputs(x="incremented"),
                add.rename_inputs(a="doubled", b="other"),
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
        graph = Graph([double, async_add.rename_inputs(a="doubled")])
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
        arrived = 0
        both_arrived = asyncio.Event()

        async def wait_for_both() -> None:
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_arrived.set()
            await asyncio.wait_for(both_arrived.wait(), timeout=15)

        @node(output_name="r1")
        async def wait1(x: int) -> int:
            await wait_for_both()
            return x

        @node(output_name="r2")
        async def wait2(x: int) -> int:
            await wait_for_both()
            return x

        graph = Graph([wait1, wait2])
        runner = AsyncRunner()
        result = await runner.run(graph, {"x": 5})

        assert arrived == 2
        assert result["r1"] == 5
        assert result["r2"] == 5

    async def test_max_concurrency_limits_parallelism(self):
        """max_concurrency limits parallel execution."""
        execution_order: list[str] = []

        async def yield_to_ready_tasks() -> None:
            resume = asyncio.Event()
            asyncio.get_running_loop().call_soon(resume.set)
            await resume.wait()

        @node(output_name="r1")
        async def track1(x: int) -> int:
            execution_order.append("r1_start")
            await yield_to_ready_tasks()
            execution_order.append("r1_end")
            return x

        @node(output_name="r2")
        async def track2(x: int) -> int:
            execution_order.append("r2_start")
            await yield_to_ready_tasks()
            execution_order.append("r2_end")
            return x

        graph = Graph([track1, track2])
        runner = AsyncRunner()

        await runner.run(graph, {"x": 5}, max_concurrency=1)

        assert execution_order in (
            ["r1_start", "r1_end", "r2_start", "r2_end"],
            ["r2_start", "r2_end", "r1_start", "r1_end"],
        )

    async def test_concurrency_one_is_sequential(self):
        """max_concurrency=1 forces sequential execution."""
        execution_order = []

        async def yield_to_ready_tasks() -> None:
            resume = asyncio.Event()
            asyncio.get_running_loop().call_soon(resume.set)
            await resume.wait()

        @node(output_name="a")
        async def track_a(x: int) -> int:
            execution_order.append("a_start")
            await yield_to_ready_tasks()
            execution_order.append("a_end")
            return x

        @node(output_name="b")
        async def track_b(x: int) -> int:
            execution_order.append("b_start")
            await yield_to_ready_tasks()
            execution_order.append("b_end")
            return x

        graph = Graph([track_a, track_b])
        runner = AsyncRunner()

        await runner.run(graph, {"x": 5}, max_concurrency=1)

        assert execution_order in (
            ["a_start", "a_end", "b_start", "b_end"],
            ["b_start", "b_end", "a_start", "a_end"],
        )

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
        """Graph-level select filters outputs."""
        graph = Graph([double, add.rename_inputs(a="doubled")]).select("sum")
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5, "b": 3})

        assert "sum" in result
        assert "doubled" not in result

    async def test_internal_edge_produced_overrides_are_rejected(self):
        """Internal edge-produced overrides are rejected."""

        @node(output_name=("left", "right"))
        def split(x: int) -> tuple[int, int]:
            return x, x + 1

        @node(output_name="double_left")
        def use_left(left: int) -> int:
            return left * 2

        @node(output_name="double_right")
        def use_right(right: int) -> int:
            return right * 2

        graph = Graph([split, use_left, use_right])
        runner = AsyncRunner()

        with pytest.raises(ValueError, match="internal parameters"):
            await runner.run(graph, {"left": 100, "right": 200})

    async def test_removed_internal_override_argument_is_rejected(self):
        """run() rejects removed runner options as unexpected kwargs."""
        graph = Graph([double])
        runner = AsyncRunner()

        with pytest.raises(ValueError, match="runner\\.run\\(\\) got unexpected input keyword 'on_internal_override'"):
            await runner.run(graph, {"x": 1}, on_internal_override="warn")  # type: ignore[call-arg]

    # Cycles

    async def test_cycle_executes_until_stable(self):
        """Cyclic graph runs until outputs stabilize."""

        @route(targets=["counter_stop", END])
        def cycle_gate(count: int, limit: int = 10) -> str:
            return END if count >= limit else "counter_stop"

        graph = Graph([counter_stop, cycle_gate], entrypoint="counter_stop")
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

        graph = Graph([async_counter_stop, async_cycle_gate], entrypoint="async_counter_stop")
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
        outer = Graph([inner.as_node(), async_add.rename_inputs(a="doubled")])
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
        """Node exceptions propagate by default (error_handling='raise')."""

        @node(output_name="result")
        async def failing(x: int) -> int:
            raise ValueError("intentional error")

        graph = Graph([failing])
        runner = AsyncRunner()

        with pytest.raises(ValueError, match="intentional error"):
            await runner.run(graph, {"x": 5})


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

    async def test_map_unknown_kwarg_input_raises(self):
        """map kwargs shorthand only accepts flat graph input names."""
        graph = Graph([double])
        runner = AsyncRunner()

        with pytest.raises(ValueError, match="runner\\.map\\(\\) got unexpected input keyword 'typo'"):
            await runner.map(graph, map_over="x", x=[1], typo=2)

    async def test_map_dotted_kwarg_input_raises(self):
        """Dotted input addresses in map() must go through values."""
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(namespaced=True)])
        runner = AsyncRunner()

        with pytest.raises(ValueError, match="Dotted input address 'inner\\.x'.*values=\\{'inner\\.x':"):
            await runner.map(outer, map_over="inner.x", **{"inner.x": [1]})

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

    async def test_map_input_named_max_iterations_requires_values_dict(self):
        """Run-only option names remain valid map inputs through values."""

        @node(output_name="sum")
        def add_with_reserved_name(x: int, max_iterations: int) -> int:
            return x + max_iterations

        graph = Graph([add_with_reserved_name])
        runner = AsyncRunner()

        results = await runner.map(
            graph,
            values={"x": [1, 2], "max_iterations": 10},
            map_over="x",
        )

        assert [r["sum"] for r in results] == [11, 12]

    async def test_map_rejects_internal_edge_produced_overrides(self):
        """Internal edge-produced overrides are rejected in map()."""

        @node(output_name=("left", "right"))
        def split(x: int) -> tuple[int, int]:
            return x, x + 1

        @node(output_name="double_left")
        def use_left(left: int) -> int:
            return left * 2

        @node(output_name="double_right")
        def use_right(right: int) -> int:
            return right * 2

        graph = Graph([split, use_left, use_right])
        runner = AsyncRunner()

        with pytest.raises(ValueError, match="internal parameters"):
            await runner.map(
                graph,
                {"left": [100], "right": 200},
                map_over="left",
            )

    async def test_map_removed_internal_override_argument_is_rejected(self):
        """map() rejects removed runner options as unexpected kwargs."""
        graph = Graph([double])
        runner = AsyncRunner()

        with pytest.raises(ValueError, match="runner\\.map\\(\\) got unexpected input keyword 'on_internal_override'"):
            await runner.map(graph, {"x": [1]}, map_over="x", on_internal_override="warn")  # type: ignore[call-arg]

    async def test_map_runs_concurrently(self):
        """Map executions run concurrently."""
        total_items = 3
        arrived = 0
        all_arrived = asyncio.Event()

        @node(output_name="result")
        async def wait_for_all(x: int) -> int:
            nonlocal arrived
            arrived += 1
            if arrived == total_items:
                all_arrived.set()
            await asyncio.wait_for(all_arrived.wait(), timeout=15)
            return x

        graph = Graph([wait_for_all])
        runner = AsyncRunner()

        results = await runner.map(
            graph,
            {"x": [1, 2, 3]},
            map_over="x",
        )

        assert len(results) == 3
        assert arrived == total_items

    async def test_map_respects_max_concurrency(self):
        """Map respects max_concurrency."""
        execution_order: list[tuple[int, str]] = []

        @node(output_name="result")
        async def track(x: int) -> int:
            execution_order.append((x, "start"))
            resume = asyncio.Event()
            asyncio.get_running_loop().call_soon(resume.set)
            await resume.wait()
            execution_order.append((x, "end"))
            return x

        graph = Graph([track])
        runner = AsyncRunner()

        results = await runner.map(
            graph,
            {"x": [1, 2, 3]},
            map_over="x",
            max_concurrency=1,
        )

        assert len(results) == 3
        assert all(
            execution_order[index][0] == execution_order[index + 1][0]
            and execution_order[index][1:] == ("start",)
            and execution_order[index + 1][1:] == ("end",)
            for index in range(0, len(execution_order), 2)
        )

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
        arrived = 0
        both_arrived = asyncio.Event()

        async def wait_for_both() -> None:
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_arrived.set()
            await asyncio.wait_for(both_arrived.wait(), timeout=15)

        @node(output_name="a")
        async def wait_a(x: int) -> int:
            await wait_for_both()
            return x * 2

        @node(output_name="b")
        async def wait_b(y: int) -> int:
            await wait_for_both()
            return y * 3

        # Two disconnected subgraphs - no edges between them
        graph = Graph([wait_a, wait_b])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5, "y": 10})

        assert result.status == RunStatus.COMPLETED
        assert result["a"] == 10
        assert result["b"] == 30
        assert arrived == 2

    async def test_select_from_disconnected_subgraph(self):
        """Graph-level select works with disconnected graphs."""

        @node(output_name="a")
        async def subgraph_a(x: int) -> int:
            return x * 2

        @node(output_name="b")
        async def subgraph_b(y: int) -> int:
            return y * 3

        graph = Graph([subgraph_a, subgraph_b]).select("a")
        runner = AsyncRunner()

        # Select only from one subgraph
        result = await runner.run(graph, {"x": 5})

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
        arrived = 0
        both_arrived = asyncio.Event()

        async def wait_for_both() -> None:
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_arrived.set()
            await asyncio.wait_for(both_arrived.wait(), timeout=15)

        @node(output_name="a")
        async def inner_a(x: int) -> int:
            await wait_for_both()
            return x * 2

        @node(output_name="b")
        async def inner_b(x: int) -> int:
            await wait_for_both()
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
        assert arrived == 2


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

        # 4 map items, each with a nested graph.
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
        nodes = [tracked_node.with_name(f"n{i}").rename_inputs(x=f"x{i}").rename_outputs(result=f"r{i}") for i in range(4)]
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
        sync1 = sync_node.with_name("sync1").rename_inputs(x="x1").rename_outputs(sync_result="s1")
        sync2 = sync_node.with_name("sync2").rename_inputs(x="x2").rename_outputs(sync_result="s2")
        async1 = async_tracked.with_name("async1").rename_inputs(y="y1").rename_outputs(async_result="a1")
        async2 = async_tracked.with_name("async2").rename_inputs(y="y2").rename_outputs(async_result="a2")

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
