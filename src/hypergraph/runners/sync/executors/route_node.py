"""Sync executor for RouteNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.gate_execution import execute_route

if TYPE_CHECKING:
    from hypergraph.nodes.gate import RouteNode
    from hypergraph.runners._shared.types import GraphState


class SyncRouteNodeExecutor:
    """Executes RouteNode synchronously."""

    def __call__(
        self,
        node: "RouteNode",
        state: "GraphState",
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        return execute_route(node, state, inputs)
