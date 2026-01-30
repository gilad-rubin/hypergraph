"""Async executor for GraphNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import collect_as_lists, map_inputs_to_func_params
from hypergraph.runners._shared.types import RunStatus

if TYPE_CHECKING:
    from hypergraph.nodes.graph_node import GraphNode
    from hypergraph.runners._shared.types import GraphState
    from hypergraph.runners.async_.runner import AsyncRunner


class AsyncGraphNodeExecutor:
    """Executes GraphNode asynchronously by delegating to runner.

    Handles:
    - Simple nested graph execution
    - Map-over execution (delegates to runner.map())

    Nested graphs inherit the parent's concurrency limiter via ContextVar,
    so max_concurrency is shared across all levels of execution.
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

        Inherits the parent's concurrency limiter via ContextVar.
        All nested operations share the same global concurrency budget.

        Args:
            node: The GraphNode to execute
            state: Current graph execution state (unused directly)
            inputs: Input values for the nested graph

        Returns:
            Dict mapping output names to their values
        """
        # Translate renamed input keys back to original inner graph names
        inner_inputs = map_inputs_to_func_params(node, inputs)

        map_config = node.map_config

        if map_config:
            _, mode, error_handling = map_config
            # Use original param names for map_over (inner graph expects these)
            original_params = node._original_map_params()
            results = await self.runner.map(
                node.graph, inner_inputs, map_over=original_params, map_mode=mode,
                error_handling=error_handling,
            )
            return collect_as_lists(results, node, error_handling)

        result = await self.runner.run(node.graph, inner_inputs)
        if result.status == RunStatus.FAILED:
            raise result.error  # type: ignore[misc]
        return node.map_outputs_from_original(result.values)
