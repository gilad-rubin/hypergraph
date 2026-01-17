"""Async executor for GraphNode."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import generate_map_inputs
from hypergraph.runners._shared.types import RunStatus

if TYPE_CHECKING:
    from hypergraph.nodes.graph_node import GraphNode
    from hypergraph.runners._shared.types import GraphState
    from hypergraph.runners.async_.runner import AsyncRunner


class AsyncGraphNodeExecutor:
    """Executes GraphNode asynchronously by delegating to runner.

    Handles:
    - Simple nested graph execution
    - Map-over execution (iterating over inputs concurrently)
    """

    def __init__(self, runner: "AsyncRunner"):
        """Initialize with reference to parent runner.

        Args:
            runner: The AsyncRunner that owns this executor
        """
        self.runner = runner

    async def __call__(
        self,
        node: "GraphNode",
        state: "GraphState",
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a GraphNode by running its inner graph.

        Args:
            node: The GraphNode to execute
            state: Current graph execution state (unused directly)
            inputs: Input values for the nested graph

        Returns:
            Dict mapping output names to their values
        """
        # Check if GraphNode has map_over configured
        map_over = getattr(node, "_map_over", None)
        map_mode = getattr(node, "_map_mode", "zip")

        if map_over:
            return await self._execute_with_map(node, inputs, map_over, map_mode)

        # Execute once
        result = await self.runner.run(node.graph, inputs)
        if result.status == RunStatus.FAILED:
            raise result.error or RuntimeError("Nested graph execution failed")
        return result.values

    async def _execute_with_map(
        self,
        node: "GraphNode",
        inputs: dict[str, Any],
        map_over: list[str],
        map_mode: str,
    ) -> dict[str, Any]:
        """Execute a GraphNode with map_over configuration.

        Args:
            node: The GraphNode to execute
            inputs: Input values (some are lists to iterate over)
            map_over: Parameter names to iterate over
            map_mode: "zip" or "product"

        Returns:
            Dict mapping output names to lists of values
        """
        # Generate input variations
        input_variations = list(generate_map_inputs(inputs, map_over, map_mode))

        if not input_variations:
            return {output: [] for output in node.outputs}

        # Execute all variations concurrently
        async def run_one(variation_inputs: dict[str, Any]) -> Any:
            return await self.runner.run(node.graph, variation_inputs)

        results = await asyncio.gather(*[run_one(v) for v in input_variations])

        # Check for failures
        for result in results:
            if result.status == RunStatus.FAILED:
                raise result.error or RuntimeError("Nested graph execution failed")

        # Collect outputs as lists
        outputs: dict[str, list] = {output: [] for output in node.outputs}
        for result in results:
            for output_name in node.outputs:
                if output_name in result.values:
                    outputs[output_name].append(result.values[output_name])

        return outputs
