"""Asynchronous runner for graph execution."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.exceptions import InfiniteLoopError
from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.runners._shared.helpers import (
    filter_outputs,
    generate_map_inputs,
    get_ready_nodes,
    initialize_state,
)
from hypergraph.runners._shared.protocols import AsyncNodeExecutor
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
from hypergraph.runners.async_.executors import (
    AsyncFunctionNodeExecutor,
    AsyncGraphNodeExecutor,
)
from hypergraph.runners.async_.superstep import (
    get_concurrency_limiter,
    reset_concurrency_limiter,
    run_superstep_async,
    set_concurrency_limiter,
)
from hypergraph.runners.base import BaseRunner

if TYPE_CHECKING:
    from hypergraph.graph import Graph

# Default max iterations for cyclic graphs
DEFAULT_MAX_ITERATIONS = 1000


class AsyncRunner(BaseRunner):
    """Asynchronous runner for graph execution.

    Executes graphs asynchronously with support for:
    - Async nodes (coroutines, async generators)
    - Concurrent execution within supersteps
    - Concurrency limiting via max_concurrency

    Features:
    - Supports cyclic graphs with max_iterations limit
    - Concurrent execution of independent nodes
    - Configurable concurrency limit
    - Supports both sync and async nodes

    Example:
        >>> from hypergraph import Graph, node, AsyncRunner
        >>> @node(output_name="doubled")
        ... async def double(x: int) -> int:
        ...     return x * 2
        >>> graph = Graph([double])
        >>> runner = AsyncRunner()
        >>> result = await runner.run(graph, {"x": 5})
        >>> result["doubled"]
        10
    """

    def __init__(self):
        """Initialize AsyncRunner with its node executors."""
        self._executors: dict[type[HyperNode], AsyncNodeExecutor] = {
            FunctionNode: AsyncFunctionNodeExecutor(),
            GraphNode: AsyncGraphNodeExecutor(self),
        }

    @property
    def capabilities(self) -> RunnerCapabilities:
        """AsyncRunner capabilities."""
        return RunnerCapabilities(
            supports_cycles=True,
            supports_async_nodes=True,
            supports_streaming=False,  # Phase 2
            returns_coroutine=True,
        )

    @property
    def supported_node_types(self) -> set[type[HyperNode]]:
        """Node types this runner can execute."""
        return set(self._executors.keys())

    async def run(
        self,
        graph: "Graph",
        values: dict[str, Any],
        *,
        select: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrency: int | None = None,
    ) -> RunResult:
        """Execute a graph asynchronously.

        Args:
            graph: The graph to execute
            values: Input values for graph parameters
            select: Optional list of output names to include in result
            max_iterations: Max supersteps for cyclic graphs (default: 1000)
            max_concurrency: Max parallel node executions (None = unlimited)

        Returns:
            RunResult containing output values and execution status

        Raises:
            MissingInputError: If required inputs not provided
            InfiniteLoopError: If max_iterations exceeded
        """
        # Validate
        validate_runner_compatibility(graph, self.capabilities)
        validate_node_types(graph, self.supported_node_types)
        validate_inputs(graph, values)

        max_iter = max_iterations or DEFAULT_MAX_ITERATIONS

        try:
            state = await self._execute_graph(graph, values, max_iter, max_concurrency)
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

    async def _execute_graph(
        self,
        graph: "Graph",
        values: dict[str, Any],
        max_iterations: int,
        max_concurrency: int | None,
    ) -> GraphState:
        """Execute graph until no more ready nodes or max_iterations reached."""
        state = initialize_state(graph, values)

        # Set up concurrency limiter if specified
        if max_concurrency is not None:
            semaphore = asyncio.Semaphore(max_concurrency)
            token = set_concurrency_limiter(semaphore)
        else:
            token = None

        try:
            for iteration in range(max_iterations):
                ready_nodes = get_ready_nodes(graph, state)

                if not ready_nodes:
                    break  # No more nodes to execute

                # Execute all ready nodes concurrently
                state = await run_superstep_async(
                    graph,
                    state,
                    ready_nodes,
                    values,
                    self._execute_node,
                    max_concurrency,
                )

            else:
                # Loop completed without break = hit max_iterations
                if get_ready_nodes(graph, state):
                    raise InfiniteLoopError(max_iterations)

        finally:
            # Reset concurrency limiter
            if token is not None:
                reset_concurrency_limiter(token)

        return state

    async def _execute_node(
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

        return await executor(node, state, inputs)

    async def map(
        self,
        graph: "Graph",
        values: dict[str, Any],
        *,
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        select: list[str] | None = None,
        max_concurrency: int | None = None,
    ) -> list[RunResult]:
        """Execute graph multiple times with different inputs.

        Args:
            graph: The graph to execute
            values: Input values (map_over params should be lists)
            map_over: Parameter name(s) to iterate over
            map_mode: "zip" for parallel iteration, "product" for cartesian
            select: Optional list of outputs to return
            max_concurrency: Max parallel executions

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

        # Execute all variations concurrently (with optional limiting)
        semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None

        async def run_one(variation_inputs: dict[str, Any]) -> RunResult:
            if semaphore:
                async with semaphore:
                    return await self.run(
                        graph,
                        variation_inputs,
                        select=select,
                        max_concurrency=max_concurrency,
                    )
            return await self.run(
                graph,
                variation_inputs,
                select=select,
                max_concurrency=max_concurrency,
            )

        results = await asyncio.gather(*[run_one(v) for v in input_variations])
        return list(results)
