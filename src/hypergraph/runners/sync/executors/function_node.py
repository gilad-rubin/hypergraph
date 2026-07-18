"""Sync executor for FunctionNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.cache_observer import node_cache_observer
from hypergraph.runners._shared.outputs import wrap_outputs

if TYPE_CHECKING:
    from hypergraph.nodes.function import FunctionNode
    from hypergraph.runners._shared.state import ExecutionContext, GraphState


class SyncFunctionNodeExecutor:
    """Executes FunctionNode synchronously.

    Handles:
    - Regular function calls
    - Sync generators (accumulated to list)
    """

    def __call__(
        self,
        node: FunctionNode,
        state: GraphState,
        inputs: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        """Execute a FunctionNode synchronously.

        Args:
            node: The FunctionNode to execute
            state: Current graph execution state (unused for FunctionNode)
            inputs: Input values for the node
            ctx: ExecutionContext with emit_fn for NodeContext injection

        Returns:
            Dict mapping output names to their values
        """
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

            def invoke() -> Any:
                result = node.func(**func_inputs)
                # Sync generator bodies execute lazily during iteration, so
                # consume them inside the observer scope (and inside the
                # attempt, so a failing generator body counts as a failed
                # attempt) to preserve inner-cache telemetry.
                if node.is_generator:
                    return list(result)
                return result

            if node.retry is None:
                result = invoke()
            else:
                # The attempt coordinator sits here: below the superstep's
                # cache lookup, above state application. The ledger keys off
                # the workflow_id (StepRecords use it as run_id).
                from hypergraph.runners._shared.attempts import AttemptEventSink, run_attempts_sync

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
                result = run_attempts_sync(
                    invoke,
                    node_name=node.name,
                    policy=node.retry,
                    checkpointer=ctx.checkpointer,
                    run_id=ctx.workflow_id,
                    scheduled_superstep=ctx.superstep_offset + ctx.superstep,
                    events=events,
                )

        return wrap_outputs(node, result)
