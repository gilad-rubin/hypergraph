"""Sync executor for FunctionNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import (
    map_inputs_to_func_params,
    wrap_outputs,
)

if TYPE_CHECKING:
    from hypergraph.nodes.function import FunctionNode
    from hypergraph.runners._shared.types import ExecutionContext, GraphState


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
        func_inputs = map_inputs_to_func_params(node, inputs)

        # Inject NodeContext if the node declares one
        if getattr(node, "_context_param", None) is not None:
            from hypergraph.runners._shared.node_context import build_node_context

            func_inputs[node._context_param] = build_node_context(node.name, ctx.emit_fn, run_id=ctx.run_id)

        # Call the function
        result = node.func(**func_inputs)

        # Handle generators - accumulate to list
        if node.is_generator:
            result = list(result)

        return wrap_outputs(node, result)
