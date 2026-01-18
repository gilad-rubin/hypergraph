"""Synchronous runner for graph execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from hypergraph.exceptions import InfiniteLoopError
from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.gate import IfElseNode, RouteNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.runners._shared.helpers import (
    filter_outputs,
    generate_map_inputs,
    get_ready_nodes,
    initialize_state,
)
from hypergraph.runners._shared.protocols import NodeExecutor
from hypergraph.runners._shared.types import (
    GraphState,
    RunnerCapabilities,
    RunResult,
    RunStatus,
)
from hypergraph.runners._shared.validation import (
    validate_inputs,
    validate_map_compatible,
    validate_node_types,
    validate_runner_compatibility,
)
from hypergraph.runners.base import BaseRunner
from hypergraph.runners.sync.executors import (
    SyncFunctionNodeExecutor,
    SyncGraphNodeExecutor,
    SyncIfElseNodeExecutor,
    SyncRouteNodeExecutor,
)
from hypergraph.runners.sync.superstep import run_superstep_sync

if TYPE_CHECKING:
    from hypergraph.graph import Graph

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

    def __init__(self):
        """Initialize SyncRunner with its node executors."""
        self._executors: dict[type[HyperNode], NodeExecutor] = {
            FunctionNode: SyncFunctionNodeExecutor(),
            GraphNode: SyncGraphNodeExecutor(self),
            IfElseNode: SyncIfElseNodeExecutor(),
            RouteNode: SyncRouteNodeExecutor(),
        }

    @property
    def capabilities(self) -> RunnerCapabilities:
        """SyncRunner capabilities."""
        return RunnerCapabilities(
            supports_cycles=True,
            supports_async_nodes=False,
            supports_streaming=False,
            returns_coroutine=False,
        )

    @property
    def supported_node_types(self) -> set[type[HyperNode]]:
        """Node types this runner can execute."""
        return set(self._executors.keys())

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
        validate_node_types(graph, self.supported_node_types)
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

            # Execute all ready nodes
            state = run_superstep_sync(
                graph, state, ready_nodes, values, self._execute_node
            )

        else:
            # Loop completed without break = hit max_iterations
            if get_ready_nodes(graph, state):
                raise InfiniteLoopError(max_iterations)

        return state

    def _execute_node(
        self,
        node: HyperNode,
        state: GraphState,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a single node using its registered executor.

        Args:
            node: The node to execute
            state: Current graph execution state
            inputs: Input values for the node

        Returns:
            Dict mapping output names to their values

        Raises:
            TypeError: If node type has no registered executor
        """
        node_type = type(node)
        executor = self._executors.get(node_type)

        if executor is None:
            raise TypeError(
                f"No executor registered for node type '{node_type.__name__}'"
            )

        return executor(node, state, inputs)

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
        validate_node_types(graph, self.supported_node_types)
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
