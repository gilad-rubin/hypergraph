"""Async executor for RouteNode."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.gate_execution import execute_route
from hypergraph.runners._shared.node_context import node_context_for, settle_node_records

if TYPE_CHECKING:
    from hypergraph.nodes.gate import RouteNode
    from hypergraph.runners._shared.state import ExecutionContext, GraphState


class AsyncRouteNodeExecutor:
    """Executes RouteNode in async context.

    The routing function is always sync (validated at decoration time) and is
    called directly on the runner's loop, not dispatched to a thread. So a
    ``ctx`` it declares is built with THIS loop as its ``record_loop``: a
    ``ctx.record`` in the body defers to a loop task, which the settle below
    awaits before the gate's step record is written.
    """

    async def __call__(
        self,
        node: RouteNode,
        state: GraphState,
        inputs: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        node_context = node_context_for(node, ctx, record_loop=asyncio.get_running_loop())
        try:
            outputs = execute_route(node, state, inputs, node_context)
        except BaseException:
            await settle_node_records(node_context, node_failed=True)
            raise
        await settle_node_records(node_context, node_failed=False)
        return outputs
