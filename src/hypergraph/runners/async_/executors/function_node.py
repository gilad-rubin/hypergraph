"""Async executor for FunctionNode."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.cache_observer import node_cache_observer
from hypergraph.runners._shared.outputs import wrap_outputs
from hypergraph.runners.async_.superstep import get_concurrency_limiter

if TYPE_CHECKING:
    from hypergraph.nodes.function import FunctionNode
    from hypergraph.runners._shared.state import ExecutionContext, GraphState


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
        func_inputs = node.map_inputs_to_params(inputs)

        # Inject NodeContext if the node declares one
        if getattr(node, "_context_param", None) is not None:
            from hypergraph.runners._shared.node_context import build_node_context

            func_inputs[node._context_param] = build_node_context(
                node.name,
                ctx.emit_fn,
                run_id=ctx.run_id,
                graph_name=ctx.graph_name,
                workflow_id=ctx.workflow_id,
                item_index=ctx.item_index,
                parent_span_id=ctx.parent_span_id,
            )

        # Call the function (with cache observer installed for hypercache telemetry)
        emit_fn = ctx.emit_fn if ctx.emit_fn is not None else lambda _: None
        with node_cache_observer(
            emit_fn,
            run_id=ctx.run_id,
            node_name=node.name,
            graph_name=ctx.graph_name,
            node_span_id=ctx.parent_span_id or "",
        ):

            async def invoke() -> Any:
                result = node.func(**func_inputs)

                # Await if coroutine
                if inspect.iscoroutine(result):
                    result = await result

                # Generators are consumed inside the observer scope (and
                # inside the attempt, so a failing generator body counts as a
                # failed attempt) to preserve inner-cache telemetry.
                if inspect.isasyncgen(result):
                    return [item async for item in result]
                if inspect.isgenerator(result):
                    return list(result)
                return result

            if node.retry is None:
                result = await invoke()
            else:
                # The attempt coordinator sits here: below the superstep's
                # cache lookup, above state application. The ledger keys off
                # the workflow_id (StepRecords use it as run_id).
                from hypergraph.runners._shared.attempts import run_attempts_async

                result = await run_attempts_async(
                    invoke,
                    node_name=node.name,
                    policy=node.retry,
                    checkpointer=ctx.checkpointer,
                    run_id=ctx.workflow_id,
                    scheduled_superstep=ctx.superstep_offset + ctx.superstep,
                )

        return wrap_outputs(node, result)
