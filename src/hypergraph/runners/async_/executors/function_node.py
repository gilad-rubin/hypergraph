"""Async executor for FunctionNode."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.cache_observer import node_cache_observer
from hypergraph.runners._shared.helpers import (
    map_inputs_to_func_params,
    wrap_outputs,
)
from hypergraph.runners.async_.superstep import get_concurrency_limiter

if TYPE_CHECKING:
    from hypergraph.nodes.function import FunctionNode
    from hypergraph.runners._shared.types import ExecutionContext, GraphState


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
        node: FunctionNode,
        state: GraphState,
        inputs: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        """Execute a FunctionNode asynchronously.

        Acquires the global concurrency semaphore if set, ensuring that
        max_concurrency is shared across all levels of nested graphs.

        Args:
            node: The FunctionNode to execute
            state: Current graph execution state (unused for FunctionNode)
            inputs: Input values for the node
            ctx: ExecutionContext with emit_fn for NodeContext injection

        Returns:
            Dict mapping output names to their values
        """
        semaphore = get_concurrency_limiter()

        if semaphore:
            async with semaphore:
                return await self._execute(node, inputs, ctx)
        return await self._execute(node, inputs, ctx)

    async def _execute(
        self,
        node: FunctionNode,
        inputs: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        """Execute the function and handle result types."""
        # Map renamed inputs back to original function parameter names
        func_inputs = map_inputs_to_func_params(node, inputs)

        # Inject NodeContext if the node declares one
        if getattr(node, "_context_param", None) is not None:
            from hypergraph.runners._shared.node_context import build_node_context

            func_inputs[node._context_param] = build_node_context(node.name, ctx.emit_fn, run_id=ctx.run_id)

        # Call the function (with cache observer installed for hypercache telemetry)
        emit_fn = ctx.emit_fn if ctx.emit_fn is not None else lambda _: None
        with node_cache_observer(
            emit_fn,
            run_id=ctx.run_id,
            node_name=node.name,
            graph_name=ctx.graph_name,
            node_span_id=ctx.parent_span_id or "",
        ):
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
