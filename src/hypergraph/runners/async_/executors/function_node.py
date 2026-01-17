"""Async executor for FunctionNode."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import (
    map_inputs_to_func_params,
    wrap_outputs,
)
from hypergraph.runners.async_.superstep import get_concurrency_limiter

if TYPE_CHECKING:
    from hypergraph.nodes.function import FunctionNode
    from hypergraph.runners._shared.types import GraphState


class AsyncFunctionNodeExecutor:
    """Executes FunctionNode asynchronously.

    Handles:
    - Sync functions (called directly)
    - Async functions (awaited)
    - Sync generators (accumulated to list)
    - Async generators (accumulated to list)

    Respects the global concurrency limiter for async operations.
    The semaphore is acquired here (at the leaf level) rather than at
    the superstep level, so that nested GraphNodes don't cause deadlock.
    """

    async def __call__(
        self,
        node: "FunctionNode",
        state: "GraphState",
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a FunctionNode asynchronously.

        Acquires the global concurrency semaphore if set, ensuring that
        max_concurrency is shared across all levels of nested graphs.

        Args:
            node: The FunctionNode to execute
            state: Current graph execution state (unused for FunctionNode)
            inputs: Input values for the node

        Returns:
            Dict mapping output names to their values
        """
        semaphore = get_concurrency_limiter()

        if semaphore:
            async with semaphore:
                return await self._execute(node, inputs)
        return await self._execute(node, inputs)

    async def _execute(
        self,
        node: "FunctionNode",
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute the function and handle result types."""
        # Map renamed inputs back to original function parameter names
        func_inputs = map_inputs_to_func_params(node, inputs)

        # Call the function
        result = node.func(**func_inputs)

        # Await if coroutine
        if inspect.iscoroutine(result):
            result = await result

        # Handle async generators
        if inspect.isasyncgen(result):
            result = [item async for item in result]
        # Handle sync generators
        elif inspect.isgenerator(result):
            result = list(result)

        return wrap_outputs(node, result)
