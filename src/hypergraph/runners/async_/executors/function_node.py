"""Async executor for FunctionNode."""

from __future__ import annotations

import inspect
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.cache_observer import node_cache_observer
from hypergraph.runners._shared.outputs import wrap_outputs
from hypergraph.runners.async_.superstep import get_concurrency_limiter

if TYPE_CHECKING:
    from hypergraph.nodes.function import FunctionNode
    from hypergraph.runners._shared.state import ExecutionContext, GraphState


def _attempt_concurrency_scope() -> AbstractAsyncContextManager[Any]:
    """One in-flight-invocation permit (#218); no limiter means no gate."""
    semaphore = get_concurrency_limiter()
    return semaphore if semaphore is not None else nullcontext()


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
        if node.retry is not None or node.timeout is not None:
            # Per-attempt permits (#218): the concurrency budget covers
            # in-flight callable invocation through timeout settlement, while
            # backoff sleeps hold no permit. The attempt coordinator acquires
            # the limiter around each attempt via _attempt_concurrency_scope.
            return await self._execute(node, inputs, ctx)

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

            func_inputs[node._context_param] = build_node_context(  # type: ignore[index]
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

            if node.retry is None and node.timeout is None:
                result = await invoke()
            else:
                # The attempt coordinator sits here: below the superstep's
                # cache lookup, above state application. Timeout wraps only
                # the callable invocation inside an attempt. The ledger keys
                # off workflow_id (StepRecords use it as run_id).
                from hypergraph.runners._shared.attempts import AttemptEventSink, run_attempts_async

                events = None
                if ctx.emit_fn is not None:
                    events = AttemptEventSink(
                        emit=ctx.emit_fn,
                        run_id=ctx.run_id,
                        node_span_id=ctx.parent_span_id,
                        workflow_id=ctx.workflow_id,
                        item_index=ctx.item_index,
                        node_name=node.name,
                        graph_name=ctx.graph_name,
                        superstep=ctx.superstep,
                    )
                result = await run_attempts_async(
                    invoke,
                    node_name=node.name,
                    policy=node.retry,
                    timeout=node.timeout,
                    checkpointer=ctx.checkpointer,
                    run_id=ctx.workflow_id,
                    scheduled_superstep=ctx.superstep_offset + ctx.superstep,
                    attempt_scope=_attempt_concurrency_scope,
                    events=events,
                )

        return wrap_outputs(node, result)
