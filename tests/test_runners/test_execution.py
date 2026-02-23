"""Tests for execution logic: ready nodes, supersteps, executors, etc."""

import asyncio
import time

import pytest

from hypergraph import Graph, node
from hypergraph.nodes.gate import END, route
from hypergraph.runners._shared.helpers import (
    collect_inputs_for_node,
    filter_outputs,
    generate_map_inputs,
    get_ready_nodes,
    initialize_state,
)
from hypergraph.runners._shared.types import GraphState, NodeExecution
from hypergraph.runners.async_.executors import AsyncFunctionNodeExecutor
from hypergraph.runners.async_.superstep import (
    reset_concurrency_limiter,
    run_superstep_async,
    set_concurrency_limiter,
)
from hypergraph.runners.sync.executors import SyncFunctionNodeExecutor
from hypergraph.runners.sync.superstep import run_superstep_sync

# === Test Fixtures ===


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="sum")
def add(a: int, b: int) -> int:
    return a + b


@node(output_name="result")
def with_default(x: int, y: int = 10) -> int:
    return x + y


@node(output_name="count")
def counter(count: int) -> int:
    return count + 1


@node(output_name=("first", "second"))
def split(x: int) -> tuple[int, int]:
    return x, x + 1


@node(output_name="items")
def gen_items(n: int):
    yield from range(n)


@node(output_name="doubled")
async def async_double(x: int) -> int:
    return x * 2


@node(output_name="items")
async def async_gen_items(n: int):
    for i in range(n):
        yield i


@node
def side_effect(x: int) -> None:
    pass  # No output


# === Tests for get_ready_nodes ===


class TestGetReadyNodes:
    """Tests for finding ready nodes."""

    def test_source_nodes_ready_immediately(self):
        """Nodes with no upstream edges are immediately ready."""
        graph = Graph([double])
        state = initialize_state(graph, {"x": 5})

        ready = get_ready_nodes(graph, state)
        assert len(ready) == 1
        assert ready[0].name == "double"

    def test_node_not_ready_without_inputs(self):
        """Node is not ready if inputs aren't available."""
        graph = Graph([double])
        state = GraphState()  # No x value

        ready = get_ready_nodes(graph, state)
        assert len(ready) == 0

    def test_node_ready_when_inputs_satisfied(self):
        """Node becomes ready when inputs are available."""
        # double needs x, add needs a and b
        graph = Graph([double, add.with_inputs(a="doubled")])
        state = initialize_state(graph, {"x": 5, "b": 10})

        # Initially only double is ready
        ready = get_ready_nodes(graph, state)
        assert len(ready) == 1
        assert ready[0].name == "double"

        # After double runs, add becomes ready
        state.update_value("doubled", 10)
        state.node_executions["double"] = NodeExecution(
            node_name="double",
            input_versions={"x": 1},
            outputs={"doubled": 10},
        )

        ready = get_ready_nodes(graph, state)
        assert len(ready) == 1
        assert ready[0].name == "add"

    def test_multiple_ready_nodes_returned(self):
        """Multiple independent nodes can be ready simultaneously."""
        # Two independent nodes both need x
        double.with_name("double2").with_outputs(doubled="tripled")

        @node(output_name="tripled")
        def triple(x: int) -> int:
            return x * 3

        graph = Graph([double, triple])
        state = initialize_state(graph, {"x": 5})

        ready = get_ready_nodes(graph, state)
        assert len(ready) == 2
        names = {n.name for n in ready}
        assert names == {"double", "triple"}

    def test_fan_in_waits_for_all_inputs(self):
        """Node with multiple inputs waits for all."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        state = initialize_state(graph, {"x": 5})  # Missing b

        # Only double is ready, add needs both doubled and b
        ready = get_ready_nodes(graph, state)
        assert len(ready) == 1
        assert ready[0].name == "double"

    def test_cycle_node_ready_with_seed(self):
        """Cyclic node is ready when seed input is provided."""
        graph = Graph([counter])
        state = initialize_state(graph, {"count": 0})

        ready = get_ready_nodes(graph, state)
        assert len(ready) == 1
        assert ready[0].name == "counter"

    def test_stale_node_becomes_ready_again(self):
        """Node becomes ready again when inputs change (cycles with gates)."""

        @route(targets=["counter", END])
        def cycle_gate(count: int) -> str:
            return "counter" if count < 10 else END

        graph = Graph([counter, cycle_gate])
        state = initialize_state(graph, {"count": 0})

        # Execute counter once
        state.update_value("count", 1)
        state.node_executions["counter"] = NodeExecution(
            node_name="counter",
            input_versions={"count": 1},  # Consumed version 1
            outputs={"count": 1},
        )

        # Execute gate to activate counter
        state.routing_decisions["cycle_gate"] = "counter"
        state.node_executions["cycle_gate"] = NodeExecution(
            node_name="cycle_gate",
            input_versions={"count": 2},
            outputs={},
        )

        # count is now at version 2, so counter is stale (and gate-activated)
        assert state.versions["count"] == 2

        ready = get_ready_nodes(graph, state)
        assert len(ready) == 1
        assert ready[0].name == "counter"


# === Tests for collect_inputs_for_node ===


class TestCollectInputsForNode:
    """Tests for input collection with precedence."""

    def test_edge_value_takes_priority(self):
        """State values (from edges) take precedence."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        state = initialize_state(graph, {"x": 5, "b": 100})
        state.update_value("doubled", 10)  # Edge value

        # The node has input "doubled" (renamed from "a") and "b"
        add_node = graph._nodes["add"]
        inputs = collect_inputs_for_node(
            add_node,
            graph,
            state,
            {"doubled": 999, "b": 100},  # 999 should be ignored for edge value
        )
        # "doubled" input comes from edge value in state (10), not provided (999)
        assert inputs["doubled"] == 10

    def test_input_value_over_bound(self):
        """Provided values override bound values."""
        graph = Graph([add]).bind(a=5, b=10)

        state = GraphState()
        inputs = collect_inputs_for_node(
            graph._nodes["add"],
            graph,
            state,
            {"a": 100, "b": 200},  # Override both
        )
        assert inputs["a"] == 100
        assert inputs["b"] == 200

    def test_bound_value_over_default(self):
        """Bound values override function defaults."""
        graph = Graph([with_default]).bind(y=50)
        state = initialize_state(graph, {"x": 1})

        inputs = collect_inputs_for_node(
            graph._nodes["with_default"],
            graph,
            state,
            {"x": 1},
        )
        assert inputs["y"] == 50  # Bound, not default 10

    def test_function_default_used_last(self):
        """Function defaults are used when nothing else provides value."""
        graph = Graph([with_default])
        state = initialize_state(graph, {"x": 1})

        inputs = collect_inputs_for_node(
            graph._nodes["with_default"],
            graph,
            state,
            {"x": 1},
        )
        assert inputs["y"] == 10  # Default value


# === Tests for SyncFunctionNodeExecutor ===


class TestSyncFunctionNodeExecutor:
    """Tests for synchronous node execution."""

    def setup_method(self):
        self.executor = SyncFunctionNodeExecutor()
        self.state = GraphState()

    def test_executes_function_with_inputs(self):
        """Function is called with provided inputs."""
        outputs = self.executor(double, self.state, {"x": 5})
        assert outputs == {"doubled": 10}

    def test_returns_dict_with_outputs(self):
        """Returns dict mapping output names to values."""
        outputs = self.executor(add, self.state, {"a": 3, "b": 4})
        assert outputs == {"sum": 7}

    def test_single_output_wrapped_in_dict(self):
        """Single return value is wrapped in dict."""
        outputs = self.executor(double, self.state, {"x": 10})
        assert outputs == {"doubled": 20}

    def test_multiple_outputs_unpacked(self):
        """Tuple return is unpacked to multiple outputs."""
        outputs = self.executor(split, self.state, {"x": 5})
        assert outputs == {"first": 5, "second": 6}

    def test_generator_accumulated_to_list(self):
        """Generator output is accumulated to list."""
        outputs = self.executor(gen_items, self.state, {"n": 3})
        assert outputs == {"items": [0, 1, 2]}

    def test_side_effect_node_returns_empty_dict(self):
        """Side-effect only nodes return empty dict."""
        outputs = self.executor(side_effect, self.state, {"x": 5})
        assert outputs == {}

    def test_exception_propagates(self):
        """Exceptions from node functions propagate."""

        @node(output_name="result")
        def failing(x: int) -> int:
            raise ValueError("intentional error")

        with pytest.raises(ValueError, match="intentional error"):
            self.executor(failing, self.state, {"x": 1})


# === Tests for AsyncFunctionNodeExecutor ===


class TestAsyncFunctionNodeExecutor:
    """Tests for async node execution."""

    def setup_method(self):
        self.executor = AsyncFunctionNodeExecutor()
        self.state = GraphState()

    async def test_executes_sync_function(self):
        """Sync functions work in async context."""
        outputs = await self.executor(double, self.state, {"x": 5})
        assert outputs == {"doubled": 10}

    async def test_executes_async_function(self):
        """Async functions are awaited."""
        outputs = await self.executor(async_double, self.state, {"x": 5})
        assert outputs == {"doubled": 10}

    async def test_async_generator_accumulated(self):
        """Async generators are accumulated to list."""
        outputs = await self.executor(async_gen_items, self.state, {"n": 3})
        assert outputs == {"items": [0, 1, 2]}

    async def test_sync_generator_in_async(self):
        """Sync generators work in async context."""
        outputs = await self.executor(gen_items, self.state, {"n": 3})
        assert outputs == {"items": [0, 1, 2]}


# === Tests for run_superstep_sync ===


class TestRunSuperstepSync:
    """Tests for synchronous superstep execution."""

    def setup_method(self):
        self.executor = SyncFunctionNodeExecutor()

    def _execute_node(self, node, state, inputs):
        """Simple executor that only handles FunctionNode."""
        return self.executor(node, state, inputs)

    def test_executes_all_ready_nodes(self):
        """All ready nodes are executed."""

        @node(output_name="tripled")
        def triple(x: int) -> int:
            return x * 3

        graph = Graph([double, triple])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)

        new_state = run_superstep_sync(
            graph, state, ready, {"x": 5}, self._execute_node
        )

        assert new_state.values["doubled"] == 10
        assert new_state.values["tripled"] == 15

    def test_updates_state_values(self):
        """State values are updated with node outputs."""
        graph = Graph([double])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)

        new_state = run_superstep_sync(
            graph, state, ready, {"x": 5}, self._execute_node
        )

        assert "doubled" in new_state.values
        assert new_state.values["doubled"] == 10

    def test_increments_versions(self):
        """Output versions are incremented."""
        graph = Graph([double])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)

        new_state = run_superstep_sync(
            graph, state, ready, {"x": 5}, self._execute_node
        )

        assert new_state.versions["doubled"] == 1

    def test_returns_new_state(self):
        """Original state is not mutated."""
        graph = Graph([double])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)

        new_state = run_superstep_sync(
            graph, state, ready, {"x": 5}, self._execute_node
        )

        assert new_state is not state
        assert "doubled" not in state.values
        assert "doubled" in new_state.values


# === Tests for run_superstep_async ===


class TestRunSuperstepAsync:
    """Tests for async superstep execution."""

    def setup_method(self):
        self.executor = AsyncFunctionNodeExecutor()

    async def _execute_node(self, node, state, inputs):
        """Simple executor that only handles FunctionNode."""
        return await self.executor(node, state, inputs)

    async def test_executes_nodes_concurrently(self):
        """Multiple nodes execute concurrently."""

        @node(output_name="a")
        async def slow_a(x: int) -> int:
            await asyncio.sleep(0.1)
            return x + 1

        @node(output_name="b")
        async def slow_b(x: int) -> int:
            await asyncio.sleep(0.1)
            return x + 2

        graph = Graph([slow_a, slow_b])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)

        start = time.time()
        new_state = await run_superstep_async(
            graph, state, ready, {"x": 5}, self._execute_node
        )
        elapsed = time.time() - start

        # Should be ~0.1s (concurrent), not ~0.2s (sequential)
        assert elapsed < 0.18

        assert new_state.values["a"] == 6
        assert new_state.values["b"] == 7

    async def test_respects_max_concurrency(self):
        """max_concurrency limits parallel execution via global semaphore.

        Note: Concurrency is controlled at the FunctionNode executor level
        via a global ContextVar semaphore, not at the superstep level.
        """
        execution_times = []

        @node(output_name="a")
        async def track_a(x: int) -> int:
            execution_times.append(("a_start", time.time()))
            await asyncio.sleep(0.05)
            execution_times.append(("a_end", time.time()))
            return x + 1

        @node(output_name="b")
        async def track_b(x: int) -> int:
            execution_times.append(("b_start", time.time()))
            await asyncio.sleep(0.05)
            execution_times.append(("b_end", time.time()))
            return x + 2

        graph = Graph([track_a, track_b])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)

        # Set up global concurrency limiter (this is normally done by AsyncRunner)
        semaphore = asyncio.Semaphore(1)
        token = set_concurrency_limiter(semaphore)

        try:
            start = time.time()
            await run_superstep_async(
                graph, state, ready, {"x": 5}, self._execute_node, max_concurrency=1
            )
            elapsed = time.time() - start

            # With max_concurrency=1, should be sequential (~0.1s)
            assert elapsed >= 0.09
        finally:
            reset_concurrency_limiter(token)

    async def test_updates_state_atomically(self):
        """All node outputs are in final state."""
        graph = Graph([async_double])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)

        new_state = await run_superstep_async(
            graph, state, ready, {"x": 5}, self._execute_node
        )

        assert new_state.values["doubled"] == 10


# === Tests for generate_map_inputs ===


class TestGenerateMapInputs:
    """Tests for map input generation."""

    def test_zip_mode_single_param(self):
        """Zip mode with single parameter."""
        inputs = list(
            generate_map_inputs(
                {"x": [1, 2, 3], "y": 10},
                map_over=["x"],
                map_mode="zip",
            )
        )
        assert inputs == [
            {"x": 1, "y": 10},
            {"x": 2, "y": 10},
            {"x": 3, "y": 10},
        ]

    def test_zip_mode_multiple_params(self):
        """Zip mode with multiple parameters."""
        inputs = list(
            generate_map_inputs(
                {"x": [1, 2], "y": [10, 20], "z": 100},
                map_over=["x", "y"],
                map_mode="zip",
            )
        )
        assert inputs == [
            {"x": 1, "y": 10, "z": 100},
            {"x": 2, "y": 20, "z": 100},
        ]

    def test_zip_mode_unequal_lengths_raises(self):
        """Zip mode with unequal lengths raises error."""
        with pytest.raises(ValueError, match="equal lengths"):
            list(
                generate_map_inputs(
                    {"x": [1, 2, 3], "y": [10, 20]},
                    map_over=["x", "y"],
                    map_mode="zip",
                )
            )

    def test_product_mode_single_param(self):
        """Product mode with single parameter (same as zip)."""
        inputs = list(
            generate_map_inputs(
                {"x": [1, 2], "y": 10},
                map_over=["x"],
                map_mode="product",
            )
        )
        assert inputs == [
            {"x": 1, "y": 10},
            {"x": 2, "y": 10},
        ]

    def test_product_mode_two_params(self):
        """Product mode generates cartesian product."""
        inputs = list(
            generate_map_inputs(
                {"x": [1, 2], "y": [10, 20]},
                map_over=["x", "y"],
                map_mode="product",
            )
        )
        assert inputs == [
            {"x": 1, "y": 10},
            {"x": 1, "y": 20},
            {"x": 2, "y": 10},
            {"x": 2, "y": 20},
        ]

    def test_product_mode_count_is_cartesian(self):
        """Product count is product of list lengths."""
        inputs = list(
            generate_map_inputs(
                {"a": [1, 2, 3], "b": [10, 20]},
                map_over=["a", "b"],
                map_mode="product",
            )
        )
        assert len(inputs) == 3 * 2  # 6 combinations

    def test_empty_list_yields_nothing(self):
        """Empty map_over list still works."""
        inputs = list(
            generate_map_inputs(
                {"x": [1, 2, 3]},
                map_over=["x"],
                map_mode="zip",
            )
        )
        assert len(inputs) == 3


# === Tests for filter_outputs ===


class TestFilterOutputs:
    """Tests for output filtering."""

    def test_select_filters_outputs(self):
        """Select filters to specified outputs only."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        state = GraphState(values={"doubled": 10, "sum": 15, "x": 5, "b": 5})

        result = filter_outputs(state, graph, select=["sum"])
        assert result == {"sum": 15}

    def test_select_all_returns_all_outputs(self):
        """select="**" returns all graph outputs."""
        graph = Graph([double])
        state = GraphState(values={"doubled": 10, "x": 5})

        result = filter_outputs(state, graph, select="**")
        # Only graph outputs, not inputs
        assert result == {"doubled": 10}

    def test_default_select_returns_all_outputs(self):
        """No select argument returns all graph outputs."""
        graph = Graph([double])
        state = GraphState(values={"doubled": 10, "x": 5})

        result = filter_outputs(state, graph)
        assert result == {"doubled": 10}

    def test_single_string_select(self):
        """select="name" (single string) works as shorthand for select=["name"]."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        state = GraphState(values={"doubled": 10, "sum": 15, "x": 5, "b": 5})

        result = filter_outputs(state, graph, select="sum")
        assert result == {"sum": 15}

    def test_invalid_on_missing_raises(self):
        """Invalid on_missing value raises ValueError."""
        graph = Graph([double])
        state = GraphState(values={"doubled": 10})

        with pytest.raises(ValueError, match="Invalid on_missing"):
            filter_outputs(state, graph, select=["nonexistent"], on_missing="raise")

    def test_missing_select_key_ignored_by_default(self):
        """Missing select keys silently omitted with on_missing='ignore' (default)."""
        graph = Graph([double])
        state = GraphState(values={"doubled": 10})

        result = filter_outputs(state, graph, select=["doubled", "nonexistent"])
        assert result == {"doubled": 10}

    def test_missing_select_key_warns(self):
        """on_missing='warn' emits warning for missing select keys."""
        graph = Graph([double])
        state = GraphState(values={"doubled": 10})

        with pytest.warns(UserWarning, match="Requested outputs not found"):
            result = filter_outputs(state, graph, select=["doubled", "nonexistent"], on_missing="warn")
        assert result == {"doubled": 10}

    def test_missing_select_key_errors(self):
        """on_missing='error' raises ValueError for missing select keys."""
        graph = Graph([double])
        state = GraphState(values={"doubled": 10})

        with pytest.raises(ValueError, match="Requested outputs not found"):
            filter_outputs(state, graph, select=["doubled", "nonexistent"], on_missing="error")
