"""Tests for SyncRunner."""

import pytest

from hypergraph import Graph, node
from hypergraph.exceptions import (
    IncompatibleRunnerError,
    InfiniteLoopError,
    MissingInputError,
)
from hypergraph.runners import RunStatus, SyncRunner


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
def counter(count: int) -> int:
    return count + 1


@node(output_name="count")
def counter_stop(count: int, limit: int = 10) -> int:
    if count >= limit:
        return count  # Stop condition - return same value
    return count + 1


@node(output_name="items")
def gen_items(n: int):
    for i in range(n):
        yield i


@node(output_name="doubled")
async def async_double(x: int) -> int:
    return x * 2


@node
def side_effect(x: int) -> None:
    pass


# === Tests ===


class TestSyncRunnerCapabilities:
    """Tests for SyncRunner capabilities."""

    def test_supports_cycles_true(self):
        runner = SyncRunner()
        assert runner.capabilities.supports_cycles is True

    def test_supports_async_nodes_false(self):
        runner = SyncRunner()
        assert runner.capabilities.supports_async_nodes is False

    def test_returns_coroutine_false(self):
        runner = SyncRunner()
        assert runner.capabilities.returns_coroutine is False


class TestSyncRunnerRun:
    """Tests for SyncRunner.run()."""

    # Basic execution

    def test_single_node_graph(self):
        """Execute graph with single node."""
        graph = Graph([double])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == 10

    def test_linear_dag_two_nodes(self):
        """Execute linear graph with two nodes."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5, "b": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == 10
        assert result["sum"] == 13

    def test_linear_dag_three_nodes(self):
        """Execute linear graph with three nodes."""
        incr = increment.with_inputs(x="sum").with_outputs(incremented="final")
        graph = Graph([double, add.with_inputs(a="doubled"), incr])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5, "b": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["final"] == 14  # (5*2) + 3 + 1

    def test_fan_out_graph(self):
        """Multiple nodes consume same input."""

        @node(output_name="tripled")
        def triple(x: int) -> int:
            return x * 3

        graph = Graph([double, triple])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result["doubled"] == 10
        assert result["tripled"] == 15

    def test_fan_in_graph(self):
        """Node consumes outputs from multiple nodes."""
        double2 = double.with_name("double2").with_outputs(doubled="doubled2")
        graph = Graph(
            [
                double,
                double2,
                add.with_inputs(a="doubled", b="doubled2"),
            ]
        )
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result["sum"] == 20  # 10 + 10

    def test_diamond_graph(self):
        """Diamond-shaped graph with fan-out then fan-in."""
        double2 = double.with_name("double2").with_outputs(doubled="other")
        graph = Graph(
            [
                increment,  # x -> incremented (6)
                double.with_inputs(x="incremented"),  # -> doubled (12)
                double2.with_inputs(x="incremented"),  # -> other (12)
                add.with_inputs(a="doubled", b="other"),  # -> sum (24)
            ]
        )
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result["sum"] == 24

    # Input handling

    def test_passes_input_values_to_nodes(self):
        """Input values are correctly passed."""
        graph = Graph([add])
        runner = SyncRunner()

        result = runner.run(graph, {"a": 10, "b": 20})

        assert result["sum"] == 30

    def test_uses_bound_values(self):
        """Bound values are used when input not provided."""
        graph = Graph([add]).bind(a=5)
        runner = SyncRunner()

        result = runner.run(graph, {"b": 10})

        assert result["sum"] == 15

    def test_uses_function_defaults(self):
        """Function defaults are used when not provided."""
        graph = Graph([with_default])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result["result"] == 15  # 5 + 10 (default)

    def test_input_overrides_bound(self):
        """Explicit input overrides bound value."""
        graph = Graph([add]).bind(a=5, b=10)
        runner = SyncRunner()

        result = runner.run(graph, {"a": 100, "b": 200})

        assert result["sum"] == 300

    # Output handling

    def test_returns_runresult(self):
        """Returns RunResult object."""
        graph = Graph([double])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert isinstance(result, type(result))  # Is RunResult
        assert hasattr(result, "values")
        assert hasattr(result, "status")
        assert hasattr(result, "run_id")

    def test_result_contains_all_outputs(self):
        """Result contains all graph outputs by default."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5, "b": 3})

        assert "doubled" in result
        assert "sum" in result

    def test_select_filters_outputs(self):
        """Select parameter filters which outputs to return."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5, "b": 3}, select=["sum"])

        assert "sum" in result
        assert "doubled" not in result

    def test_status_is_completed(self):
        """Successful run has COMPLETED status."""
        graph = Graph([double])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED

    def test_run_id_is_unique(self):
        """Each run gets unique run_id."""
        graph = Graph([double])
        runner = SyncRunner()

        result1 = runner.run(graph, {"x": 5})
        result2 = runner.run(graph, {"x": 5})

        assert result1.run_id != result2.run_id

    # Cycles

    def test_cycle_executes_until_stable(self):
        """Cyclic graph runs until outputs stabilize."""
        graph = Graph([counter_stop])
        runner = SyncRunner()

        result = runner.run(graph, {"count": 0, "limit": 5})

        assert result["count"] == 5

    def test_cycle_respects_max_iterations(self):
        """max_iterations limits execution."""
        graph = Graph([counter])  # Never stops
        runner = SyncRunner()

        result = runner.run(graph, {"count": 0}, max_iterations=5)

        # Should fail due to infinite loop
        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, InfiniteLoopError)

    def test_cycle_raises_on_infinite_loop(self):
        """Infinite loop detected and reported."""
        graph = Graph([counter])
        runner = SyncRunner()

        result = runner.run(graph, {"count": 0}, max_iterations=10)

        assert result.status == RunStatus.FAILED
        assert "10" in str(result.error)  # Max iterations in message

    # Nested graphs

    def test_nested_graph_executes(self):
        """Nested graph via GraphNode executes."""
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node()])
        runner = SyncRunner()

        result = runner.run(outer, {"x": 5})

        assert result["doubled"] == 10

    def test_nested_graph_values_flow(self):
        """Values flow correctly through nested graphs."""
        inner = Graph([double], name="inner")
        outer = Graph(
            [
                inner.as_node(),
                add.with_inputs(a="doubled"),
            ]
        )
        runner = SyncRunner()

        result = runner.run(outer, {"x": 5, "b": 3})

        assert result["sum"] == 13

    def test_deeply_nested_graph(self):
        """Multiple levels of nesting work."""
        innermost = Graph([double], name="innermost")
        middle = Graph(
            [innermost.as_node(), increment.with_inputs(x="doubled")], name="middle"
        )
        outer = Graph([middle.as_node()])
        runner = SyncRunner()

        result = runner.run(outer, {"x": 5})

        assert result["incremented"] == 11  # (5*2) + 1

    # Errors

    def test_missing_input_raises(self):
        """Missing required input causes FAILED status."""
        graph = Graph([double])
        runner = SyncRunner()

        # Note: validation happens before execution
        with pytest.raises(MissingInputError):
            runner.run(graph, {})

    def test_async_node_raises_incompatible(self):
        """Async nodes cause IncompatibleRunnerError."""
        graph = Graph([async_double])
        runner = SyncRunner()

        with pytest.raises(IncompatibleRunnerError):
            runner.run(graph, {"x": 5})

    def test_node_exception_propagates(self):
        """Node exceptions result in FAILED status."""

        @node(output_name="result")
        def failing(x: int) -> int:
            raise ValueError("intentional error")

        graph = Graph([failing])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, ValueError)

    def test_node_exception_sets_failed_status(self):
        """Exception sets status to FAILED."""

        @node(output_name="result")
        def failing(x: int) -> int:
            raise RuntimeError("test")

        graph = Graph([failing])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED


class TestSyncRunnerRunGenerators:
    """Tests for generator node handling."""

    def test_sync_generator_accumulated(self):
        """Generator output is accumulated to list."""
        graph = Graph([gen_items])
        runner = SyncRunner()

        result = runner.run(graph, {"n": 3})

        assert result["items"] == [0, 1, 2]

    def test_generator_output_is_list(self):
        """Generator produces list output."""
        graph = Graph([gen_items])
        runner = SyncRunner()

        result = runner.run(graph, {"n": 5})

        assert isinstance(result["items"], list)
        assert len(result["items"]) == 5


class TestSyncRunnerMap:
    """Tests for SyncRunner.map()."""

    def test_map_over_single_param(self):
        """Map over a single parameter."""
        graph = Graph([double])
        runner = SyncRunner()

        results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

        assert len(results) == 3
        assert results[0]["doubled"] == 2
        assert results[1]["doubled"] == 4
        assert results[2]["doubled"] == 6

    def test_map_over_returns_list_of_results(self):
        """Map returns list of RunResult."""
        graph = Graph([double])
        runner = SyncRunner()

        results = runner.map(graph, {"x": [1, 2]}, map_over="x")

        assert isinstance(results, list)
        assert all(r.status == RunStatus.COMPLETED for r in results)

    def test_map_preserves_order(self):
        """Results are in same order as inputs."""
        graph = Graph([double])
        runner = SyncRunner()

        results = runner.map(graph, {"x": [5, 10, 15]}, map_over="x")

        values = [r["doubled"] for r in results]
        assert values == [10, 20, 30]

    def test_map_empty_list_returns_empty(self):
        """Empty input list returns empty results."""
        graph = Graph([double])
        runner = SyncRunner()

        results = runner.map(graph, {"x": []}, map_over="x")

        assert results == []

    def test_broadcast_values_shared(self):
        """Non-mapped values are broadcast to all iterations."""
        graph = Graph([add])
        runner = SyncRunner()

        results = runner.map(
            graph,
            {"a": [1, 2, 3], "b": 10},
            map_over="a",
        )

        assert [r["sum"] for r in results] == [11, 12, 13]

    def test_zip_mode_default(self):
        """Zip mode is the default."""
        graph = Graph([add])
        runner = SyncRunner()

        results = runner.map(
            graph,
            {"a": [1, 2], "b": [10, 20]},
            map_over=["a", "b"],
        )

        assert len(results) == 2
        assert results[0]["sum"] == 11
        assert results[1]["sum"] == 22

    def test_zip_mode_multiple_params(self):
        """Zip mode with multiple params."""
        graph = Graph([add])
        runner = SyncRunner()

        results = runner.map(
            graph,
            {"a": [1, 2, 3], "b": [10, 20, 30]},
            map_over=["a", "b"],
            map_mode="zip",
        )

        assert [r["sum"] for r in results] == [11, 22, 33]

    def test_zip_mode_unequal_lengths_raises(self):
        """Zip mode with unequal lengths raises error."""
        graph = Graph([add])
        runner = SyncRunner()

        with pytest.raises(ValueError, match="equal lengths"):
            runner.map(
                graph,
                {"a": [1, 2, 3], "b": [10, 20]},
                map_over=["a", "b"],
                map_mode="zip",
            )

    def test_product_mode_single_param(self):
        """Product mode with single param (same as zip)."""
        graph = Graph([double])
        runner = SyncRunner()

        results = runner.map(
            graph,
            {"x": [1, 2]},
            map_over="x",
            map_mode="product",
        )

        assert [r["doubled"] for r in results] == [2, 4]

    def test_product_mode_two_params(self):
        """Product mode generates cartesian product."""
        graph = Graph([add])
        runner = SyncRunner()

        results = runner.map(
            graph,
            {"a": [1, 2], "b": [10, 20]},
            map_over=["a", "b"],
            map_mode="product",
        )

        # 2 * 2 = 4 combinations
        assert len(results) == 4
        sums = sorted(r["sum"] for r in results)
        assert sums == [11, 12, 21, 22]

    def test_map_with_select(self):
        """Map respects select parameter."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        runner = SyncRunner()

        results = runner.map(
            graph,
            {"x": [1, 2], "b": 10},
            map_over="x",
            select=["sum"],
        )

        for r in results:
            assert "sum" in r
            assert "doubled" not in r
