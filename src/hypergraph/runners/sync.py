"""Synchronous runner for graph execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from hypergraph.exceptions import InfiniteLoopError
from hypergraph.runners._execution import (
    collect_inputs_for_node,
    filter_outputs,
    generate_map_inputs,
    get_ready_nodes,
    initialize_state,
    run_superstep_sync,
)
from hypergraph.runners._types import (
    GraphState,
    NodeExecution,
    RunnerCapabilities,
    RunResult,
    RunStatus,
)
from hypergraph.runners._validation import (
    validate_inputs,
    validate_map_compatible,
    validate_runner_compatibility,
)
from hypergraph.runners.base import BaseRunner

if TYPE_CHECKING:
    from hypergraph.graph import Graph
    from hypergraph.nodes.base import HyperNode

# Default max iterations for cyclic graphs
DEFAULT_MAX_ITERATIONS = 1000


class SyncRunner(BaseRunner):
    """Synchronous runner for graph execution.

    Executes graphs synchronously without async support.
    Nodes are executed sequentially within each superstep.

    Features:
    - Supports cyclic graphs with max_iterations limit
    - Sequential execution (no concurrency)
    - Does not support async nodes (use AsyncRunner instead)

    Example:
        >>> from hypergraph import Graph, node, SyncRunner
        >>> @node(output_name="doubled")
        ... def double(x: int) -> int:
        ...     return x * 2
        >>> graph = Graph([double])
        >>> runner = SyncRunner()
        >>> result = runner.run(graph, {"x": 5})
        >>> result["doubled"]
        10
    """

    @property
    def capabilities(self) -> RunnerCapabilities:
        """SyncRunner capabilities."""
        return RunnerCapabilities(
            supports_cycles=True,
            supports_async_nodes=False,
            supports_streaming=False,
            returns_coroutine=False,
        )

    def run(
        self,
        graph: "Graph",
        values: dict[str, Any],
        *,
        select: list[str] | None = None,
        max_iterations: int | None = None,
    ) -> RunResult:
        """Execute a graph synchronously.

        Args:
            graph: The graph to execute
            values: Input values for graph parameters
            select: Optional list of output names to include in result
            max_iterations: Max supersteps for cyclic graphs (default: 1000)

        Returns:
            RunResult containing output values and execution status

        Raises:
            MissingInputError: If required inputs not provided
            IncompatibleRunnerError: If graph has async nodes
            InfiniteLoopError: If max_iterations exceeded
        """
        # Validate
        validate_runner_compatibility(graph, self.capabilities)
        validate_inputs(graph, values)

        max_iter = max_iterations or DEFAULT_MAX_ITERATIONS

        try:
            state = self._execute_graph(graph, values, max_iter)
            output_values = filter_outputs(state, graph, select)
            return RunResult(
                values=output_values,
                status=RunStatus.COMPLETED,
            )
        except Exception as e:
            return RunResult(
                values={},
                status=RunStatus.FAILED,
                error=e,
            )

    def _execute_graph(
        self,
        graph: "Graph",
        values: dict[str, Any],
        max_iterations: int,
    ) -> GraphState:
        """Execute graph until no more ready nodes or max_iterations reached."""
        state = initialize_state(graph, values)

        for iteration in range(max_iterations):
            ready_nodes = get_ready_nodes(graph, state)

            if not ready_nodes:
                break  # No more nodes to execute

            # Filter out GraphNodes - handle separately
            function_nodes = [n for n in ready_nodes if not self._is_graph_node(n)]
            graph_nodes = [n for n in ready_nodes if self._is_graph_node(n)]

            # Execute FunctionNodes
            if function_nodes:
                state = run_superstep_sync(graph, state, function_nodes, values)

            # Execute GraphNodes
            for gn in graph_nodes:
                state = self._execute_graph_node(gn, graph, state, values)

        else:
            # Loop completed without break = hit max_iterations
            if get_ready_nodes(graph, state):
                raise InfiniteLoopError(max_iterations)

        return state

    def _is_graph_node(self, node: "HyperNode") -> bool:
        """Check if node is a GraphNode."""
        from hypergraph.nodes.graph_node import GraphNode

        return isinstance(node, GraphNode)

    def _execute_graph_node(
        self,
        graph_node: "HyperNode",
        parent_graph: "Graph",
        state: GraphState,
        provided_values: dict[str, Any],
    ) -> GraphState:
        """Execute a GraphNode by delegating to its inner graph."""
        from hypergraph.nodes.graph_node import GraphNode

        gn = graph_node  # type: GraphNode

        # Collect inputs for the nested graph
        inputs = collect_inputs_for_node(gn, parent_graph, state, provided_values)

        # Record input versions before execution
        input_versions = {
            param: state.get_version(param) for param in gn.inputs
        }

        # Check if GraphNode has map_over configured
        map_over = getattr(gn, "_map_over", None)
        map_mode = getattr(gn, "_map_mode", "zip")

        if map_over:
            # Execute with map
            outputs = self._execute_graph_node_with_map(
                gn, inputs, map_over, map_mode
            )
        else:
            # Execute once
            result = self.run(gn.graph, inputs)
            if result.status == RunStatus.FAILED:
                raise result.error or RuntimeError("Nested graph execution failed")
            outputs = result.values

        # Update state with outputs
        new_state = state.copy()
        for name, value in outputs.items():
            new_state.update_value(name, value)

        # Record execution
        new_state.node_executions[gn.name] = NodeExecution(
            node_name=gn.name,
            input_versions=input_versions,
            outputs=outputs,
        )

        return new_state

    def _execute_graph_node_with_map(
        self,
        graph_node: "HyperNode",
        inputs: dict[str, Any],
        map_over: list[str],
        map_mode: str,
    ) -> dict[str, Any]:
        """Execute a GraphNode with map_over configuration."""
        from hypergraph.nodes.graph_node import GraphNode

        gn = graph_node  # type: GraphNode

        # Generate input variations
        input_variations = list(generate_map_inputs(inputs, map_over, map_mode))

        if not input_variations:
            return {output: [] for output in gn.outputs}

        # Execute each variation
        results = []
        for variation_inputs in input_variations:
            result = self.run(gn.graph, variation_inputs)
            if result.status == RunStatus.FAILED:
                raise result.error or RuntimeError("Nested graph execution failed")
            results.append(result)

        # Collect outputs as lists
        outputs: dict[str, list] = {output: [] for output in gn.outputs}
        for result in results:
            for output_name in gn.outputs:
                if output_name in result.values:
                    outputs[output_name].append(result.values[output_name])

        return outputs

    def map(
        self,
        graph: "Graph",
        values: dict[str, Any],
        *,
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        select: list[str] | None = None,
    ) -> list[RunResult]:
        """Execute graph multiple times with different inputs.

        Args:
            graph: The graph to execute
            values: Input values (map_over params should be lists)
            map_over: Parameter name(s) to iterate over
            map_mode: "zip" for parallel iteration, "product" for cartesian
            select: Optional list of outputs to return

        Returns:
            List of RunResult, one per iteration
        """
        # Validate
        validate_runner_compatibility(graph, self.capabilities)
        validate_map_compatible(graph)

        # Normalize map_over to list
        map_over_list = [map_over] if isinstance(map_over, str) else list(map_over)

        # Generate input variations
        input_variations = list(generate_map_inputs(values, map_over_list, map_mode))

        if not input_variations:
            return []

        # Execute each variation
        results = []
        for variation_inputs in input_variations:
            result = self.run(graph, variation_inputs, select=select)
            results.append(result)

        return results
