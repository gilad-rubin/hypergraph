"""Sync executor for IfElseNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.gate_execution import execute_ifelse
from hypergraph.runners._shared.node_context import node_context_for, settle_node_records_sync

if TYPE_CHECKING:
    from hypergraph.nodes.gate import IfElseNode
    from hypergraph.runners._shared.state import ExecutionContext, GraphState


class SyncIfElseNodeExecutor:
    """Executes IfElseNode synchronously.

    No ``record_loop``: this family has no executor loop, so a ``ctx.record``
    in the routing function writes straight through and the settle below
    only answers for a write that failed.
    """

    def __call__(
        self,
        node: IfElseNode,
        state: GraphState,
        inputs: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        node_context = node_context_for(node, ctx)
        try:
            outputs = execute_ifelse(node, state, inputs, node_context)
        except BaseException:
            settle_node_records_sync(node_context, node_failed=True)
            raise
        settle_node_records_sync(node_context, node_failed=False)
        return outputs
