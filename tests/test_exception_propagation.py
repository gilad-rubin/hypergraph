"""Tests for exception behavior in complex topologies (GAP-05)."""

import pytest

from hypergraph import Graph, node
from hypergraph.nodes.gate import route, END
from hypergraph.runners import RunStatus, SyncRunner, AsyncRunner


# === Test Fixtures ===


class CustomError(Exception):
    """Custom exception for testing."""

    pass


@node(output_name="result")
def failing_node(x: int) -> int:
    raise CustomError("intentional failure")


@node(output_name="result")
def succeeding_node(x: int) -> int:
    return x * 2


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


# === Tests ===


class TestExceptionInDiamondTopology:
    """Tests for exceptions in diamond-shaped graphs."""

    def test_exception_in_one_parallel_branch(self):
        """Exception in one branch of diamond, other branch succeeds."""

        @node(output_name="a")
        def branch_a(x: int) -> int:
            return x * 2

        @node(output_name="b")
        def branch_b(x: int) -> int:
            raise CustomError("branch b failed")

        @node(output_name="result")
        def combine(a: int, b: int) -> int:
            return a + b

        graph = Graph([branch_a, branch_b, combine])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)

    def test_exception_at_diamond_join_point(self):
        """Exception at the join point of a diamond."""

        @node(output_name="a")
        def branch_a(x: int) -> int:
            return x * 2

        @node(output_name="b")
        def branch_b(x: int) -> int:
            return x * 3

        @node(output_name="result")
        def combine(a: int, b: int) -> int:
            raise CustomError("combine failed")

        graph = Graph([branch_a, branch_b, combine])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)

    def test_exception_at_diamond_start(self):
        """Exception at the start node of a diamond."""

        @node(output_name="start")
        def start_node(x: int) -> int:
            raise CustomError("start failed")

        @node(output_name="a")
        def branch_a(start: int) -> int:
            return start * 2

        @node(output_name="b")
        def branch_b(start: int) -> int:
            return start * 3

        graph = Graph([start_node, branch_a, branch_b])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)


class TestExceptionInNestedGraphNode:
    """Tests for exceptions inside nested graphs."""

    def test_exception_inside_nested_graph(self):
        """Exception inside nested graph propagates to outer."""

        @node(output_name="inner_result")
        def inner_failing(x: int) -> int:
            raise CustomError("inner graph failed")

        inner = Graph([inner_failing], name="inner")
        outer = Graph([inner.as_node()])

        runner = SyncRunner()
        result = runner.run(outer, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)

    def test_exception_in_deeply_nested_graph(self):
        """Exception in deeply nested graph propagates."""

        @node(output_name="deep_result")
        def deep_failing(x: int) -> int:
            raise CustomError("deep failure")

        level3 = Graph([deep_failing], name="level3")
        level2 = Graph([level3.as_node()], name="level2")
        level1 = Graph([level2.as_node()])

        runner = SyncRunner()
        result = runner.run(level1, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)

    def test_exception_after_successful_nested_graph(self):
        """Exception after nested graph completes successfully."""

        @node(output_name="inner_result")
        def inner_success(x: int) -> int:
            return x * 2

        inner = Graph([inner_success], name="inner")

        @node(output_name="outer_result")
        def outer_failing(inner_result: int) -> int:
            raise CustomError("outer failed after inner success")

        outer = Graph([inner.as_node(), outer_failing])

        runner = SyncRunner()
        result = runner.run(outer, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)


class TestExceptionInCycle:
    """Tests for exceptions in cyclic graphs."""

    def test_exception_in_cycle_iteration(self):
        """Exception during cycle iteration driven by gate."""
        iteration = 0

        @node(output_name="count")
        def counter_with_error(count: int, fail_at: int = 3) -> int:
            nonlocal iteration
            iteration += 1
            if count >= fail_at:
                raise CustomError(f"failed at iteration {iteration}")
            return count + 1

        @route(targets=["counter_with_error", END])
        def cycle_gate(count: int) -> str:
            return END if count >= 10 else "counter_with_error"

        graph = Graph([counter_with_error, cycle_gate])
        runner = SyncRunner()

        result = runner.run(graph, {"count": 0, "fail_at": 3})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)

    def test_exception_in_convergence_cycle(self):
        """Exception in a convergence cycle driven by gate."""

        @node(output_name="value")
        def converge_with_error(value: float, target: float = 10.0) -> float:
            if abs(target - value) < 1.0:
                raise CustomError("convergence error")
            return value + (target - value) * 0.5

        @route(targets=["converge_with_error", END])
        def converge_gate(value: float, target: float = 10.0) -> str:
            return END if abs(target - value) < 0.01 else "converge_with_error"

        graph = Graph([converge_with_error, converge_gate])
        runner = SyncRunner()

        result = runner.run(graph, {"value": 0.0, "target": 10.0})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)


class TestExceptionInMapOver:
    """Tests for exceptions in map_over iterations."""

    def test_exception_in_one_map_iteration(self):
        """Exception in one iteration of map_over."""

        @node(output_name="result")
        def conditional_fail(x: int) -> int:
            if x == 3:
                raise CustomError(f"failed on x={x}")
            return x * 2

        inner = Graph([conditional_fail], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3, 4, 5]})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)

    def test_exception_in_first_map_iteration(self):
        """Exception in first iteration of map_over."""

        @node(output_name="result")
        def always_fail(x: int) -> int:
            raise CustomError("always fails")

        inner = Graph([always_fail], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)


class TestExceptionPreservesPartialResults:
    """Tests for partial result preservation on failure."""

    def test_results_before_failure_available_in_values(self):
        """Results computed before failure are returned in values."""

        @node(output_name="a")
        def step_a(x: int) -> int:
            return x * 2

        @node(output_name="b")
        def step_b(a: int) -> int:
            raise CustomError("step b failed")

        @node(output_name="c")
        def step_c(b: int) -> int:
            return b + 1

        graph = Graph([step_a, step_b, step_c])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)
        # step_a completed before step_b failed, so "a" should be in values
        assert "a" in result.values
        assert result.values["a"] == 10
        # step_c never ran, so "c" should not be in values
        assert "c" not in result.values

    def test_independent_branches_with_one_failure(self):
        """Independent branches - one fails, checking behavior."""

        @node(output_name="a")
        def branch_a(x: int) -> int:
            return x * 2

        @node(output_name="b")
        def branch_b(x: int) -> int:
            raise CustomError("branch b failed")

        graph = Graph([branch_a, branch_b])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED

    def test_partial_values_with_select(self):
        """Partial values respect the select parameter."""

        @node(output_name="a")
        def step_a(x: int) -> int:
            return x * 2

        @node(output_name="b")
        def step_b(a: int) -> int:
            raise CustomError("step b failed")

        graph = Graph([step_a, step_b])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5}, select=["a"])

        assert result.status == RunStatus.FAILED
        assert result.values == {"a": 10}

    def test_failure_at_first_node_returns_empty_values(self):
        """When the first node fails, no outputs are available."""

        @node(output_name="a")
        def step_a(x: int) -> int:
            raise CustomError("first node failed")

        @node(output_name="b")
        def step_b(a: int) -> int:
            return a + 1

        graph = Graph([step_a, step_b])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert "a" not in result.values
        assert "b" not in result.values


class TestAsyncExceptionHandling:
    """Tests for exception handling with AsyncRunner."""

    async def test_async_node_exception(self):
        """Exception in async node propagates correctly."""

        @node(output_name="result")
        async def async_failing(x: int) -> int:
            raise CustomError("async failure")

        graph = Graph([async_failing])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)

    async def test_async_exception_in_nested_graph(self):
        """Exception in async nested graph propagates."""

        @node(output_name="inner_result")
        async def async_inner_fail(x: int) -> int:
            raise CustomError("async inner failure")

        inner = Graph([async_inner_fail], name="inner")
        outer = Graph([inner.as_node()])

        runner = AsyncRunner()
        result = await runner.run(outer, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)

    async def test_async_partial_values_on_failure(self):
        """Async runner returns partial values when a node fails."""

        @node(output_name="a")
        async def step_a(x: int) -> int:
            return x * 2

        @node(output_name="b")
        async def step_b(a: int) -> int:
            raise CustomError("step b failed")

        graph = Graph([step_a, step_b])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)
        assert "a" in result.values
        assert result.values["a"] == 10

    async def test_async_parallel_with_one_failure(self):
        """Parallel async nodes with one failure."""
        import asyncio

        @node(output_name="a")
        async def async_a(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 2

        @node(output_name="b")
        async def async_b(x: int) -> int:
            await asyncio.sleep(0.01)
            raise CustomError("async b failed")

        graph = Graph([async_a, async_b])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)


class TestExceptionTypes:
    """Tests for different exception types."""

    def test_value_error_propagates(self):
        """ValueError propagates correctly."""

        @node(output_name="result")
        def value_error_node(x: int) -> int:
            raise ValueError("invalid value")

        graph = Graph([value_error_node])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, ValueError)

    def test_type_error_propagates(self):
        """TypeError propagates correctly."""

        @node(output_name="result")
        def type_error_node(x: int) -> int:
            raise TypeError("wrong type")

        graph = Graph([type_error_node])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, TypeError)

    def test_runtime_error_propagates(self):
        """RuntimeError propagates correctly."""

        @node(output_name="result")
        def runtime_error_node(x: int) -> int:
            raise RuntimeError("runtime problem")

        graph = Graph([runtime_error_node])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, RuntimeError)

    def test_keyboard_interrupt_not_caught(self):
        """KeyboardInterrupt should not be caught."""

        @node(output_name="result")
        def interrupt_node(x: int) -> int:
            raise KeyboardInterrupt()

        graph = Graph([interrupt_node])
        runner = SyncRunner()

        # KeyboardInterrupt is BaseException, should propagate through
        with pytest.raises(KeyboardInterrupt):
            runner.run(graph, {"x": 5})


class TestExceptionInChain:
    """Tests for exceptions in linear chains."""

    def test_exception_at_start_of_chain(self):
        """Exception at start of linear chain."""

        @node(output_name="a")
        def step1(x: int) -> int:
            raise CustomError("step1 failed")

        @node(output_name="b")
        def step2(a: int) -> int:
            return a * 2

        @node(output_name="c")
        def step3(b: int) -> int:
            return b + 1

        graph = Graph([step1, step2, step3])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)

    def test_exception_in_middle_of_chain(self):
        """Exception in middle of linear chain."""

        @node(output_name="a")
        def step1(x: int) -> int:
            return x * 2

        @node(output_name="b")
        def step2(a: int) -> int:
            raise CustomError("step2 failed")

        @node(output_name="c")
        def step3(b: int) -> int:
            return b + 1

        graph = Graph([step1, step2, step3])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)

    def test_exception_at_end_of_chain(self):
        """Exception at end of linear chain."""

        @node(output_name="a")
        def step1(x: int) -> int:
            return x * 2

        @node(output_name="b")
        def step2(a: int) -> int:
            return a + 1

        @node(output_name="c")
        def step3(b: int) -> int:
            raise CustomError("step3 failed")

        graph = Graph([step1, step2, step3])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomError)
