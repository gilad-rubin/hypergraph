"""Asynchronous runner for graph execution."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.exceptions import InfiniteLoopError
from hypergraph.runners._execution import (
    _concurrency_limiter,
    collect_inputs_for_node,
    filter_outputs,
    generate_map_inputs,
    get_ready_nodes,
    initialize_state,
    run_superstep_async,
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

    @property
    def capabilities(self) -> RunnerCapabilities:
        """AsyncRunner capabilities."""
        return RunnerCapabilities(
            supports_cycles=True,
            supports_async_nodes=True,
            supports_streaming=False,  # Phase 2
            returns_coroutine=True,
        )

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
            token = _concurrency_limiter.set(semaphore)
        else:
            token = None

        try:
            for iteration in range(max_iterations):
                ready_nodes = get_ready_nodes(graph, state)

                if not ready_nodes:
                    break  # No more nodes to execute

                # Filter out GraphNodes - handle separately
                function_nodes = [n for n in ready_nodes if not self._is_graph_node(n)]
                graph_nodes = [n for n in ready_nodes if self._is_graph_node(n)]

                # Execute FunctionNodes concurrently
                if function_nodes:
                    state = await run_superstep_async(
                        graph, state, function_nodes, values, max_concurrency
                    )

                # Execute GraphNodes (possibly concurrently)
                if graph_nodes:
                    state = await self._execute_graph_nodes(
                        graph_nodes, graph, state, values, max_concurrency
                    )

            else:
                # Loop completed without break = hit max_iterations
                if get_ready_nodes(graph, state):
                    raise InfiniteLoopError(max_iterations)

        finally:
            # Reset concurrency limiter
            if token is not None:
                _concurrency_limiter.reset(token)

        return state

    def _is_graph_node(self, node: "HyperNode") -> bool:
        """Check if node is a GraphNode."""
        from hypergraph.nodes.graph_node import GraphNode

        return isinstance(node, GraphNode)

    async def _execute_graph_nodes(
        self,
        graph_nodes: list["HyperNode"],
        parent_graph: "Graph",
        state: GraphState,
        provided_values: dict[str, Any],
        max_concurrency: int | None,
    ) -> GraphState:
        """Execute multiple GraphNodes, potentially concurrently."""
        if not graph_nodes:
            return state

        # Execute all graph nodes concurrently
        semaphore = _concurrency_limiter.get()

        async def execute_one(
            gn: "HyperNode",
        ) -> tuple["HyperNode", dict[str, Any], dict[str, int]]:
            inputs = collect_inputs_for_node(gn, parent_graph, state, provided_values)
            input_versions = {
                param: state.get_version(param) for param in gn.inputs
            }

            if semaphore:
                async with semaphore:
                    outputs = await self._execute_single_graph_node(
                        gn, inputs, max_concurrency
                    )
            else:
                outputs = await self._execute_single_graph_node(
                    gn, inputs, max_concurrency
                )

            return gn, outputs, input_versions

        tasks = [execute_one(gn) for gn in graph_nodes]
        results = await asyncio.gather(*tasks)

        # Update state with all results
        new_state = state.copy()
        for gn, outputs, input_versions in results:
            for name, value in outputs.items():
                new_state.update_value(name, value)

            new_state.node_executions[gn.name] = NodeExecution(
                node_name=gn.name,
                input_versions=input_versions,
                outputs=outputs,
            )

        return new_state

    async def _execute_single_graph_node(
        self,
        graph_node: "HyperNode",
        inputs: dict[str, Any],
        max_concurrency: int | None,
    ) -> dict[str, Any]:
        """Execute a single GraphNode."""
        from hypergraph.nodes.graph_node import GraphNode

        gn = graph_node  # type: GraphNode

        # Check if GraphNode has map_over configured
        map_over = getattr(gn, "_map_over", None)
        map_mode = getattr(gn, "_map_mode", "zip")

        if map_over:
            return await self._execute_graph_node_with_map(
                gn, inputs, map_over, map_mode, max_concurrency
            )

        # Execute once
        result = await self.run(gn.graph, inputs, max_concurrency=max_concurrency)
        if result.status == RunStatus.FAILED:
            raise result.error or RuntimeError("Nested graph execution failed")
        return result.values

    async def _execute_graph_node_with_map(
        self,
        graph_node: "HyperNode",
        inputs: dict[str, Any],
        map_over: list[str],
        map_mode: str,
        max_concurrency: int | None,
    ) -> dict[str, Any]:
        """Execute a GraphNode with map_over configuration."""
        from hypergraph.nodes.graph_node import GraphNode

        gn = graph_node  # type: GraphNode

        # Generate input variations
        input_variations = list(generate_map_inputs(inputs, map_over, map_mode))

        if not input_variations:
            return {output: [] for output in gn.outputs}

        # Execute all variations concurrently
        async def run_one(variation_inputs: dict[str, Any]) -> RunResult:
            return await self.run(
                gn.graph, variation_inputs, max_concurrency=max_concurrency
            )

        results = await asyncio.gather(*[run_one(v) for v in input_variations])

        # Check for failures
        for result in results:
            if result.status == RunStatus.FAILED:
                raise result.error or RuntimeError("Nested graph execution failed")

        # Collect outputs as lists
        outputs: dict[str, list] = {output: [] for output in gn.outputs}
        for result in results:
            for output_name in gn.outputs:
                if output_name in result.values:
                    outputs[output_name].append(result.values[output_name])

        return outputs

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
