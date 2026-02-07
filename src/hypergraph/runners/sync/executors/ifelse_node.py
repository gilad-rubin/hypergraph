"""Sync executor for IfElseNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.gate_execution import execute_ifelse

if TYPE_CHECKING:
    from hypergraph.nodes.gate import IfElseNode
    from hypergraph.runners._shared.types import GraphState


class SyncIfElseNodeExecutor:
    """Executes IfElseNode synchronously."""

    def __call__(
        self,
        node: "IfElseNode",
        state: "GraphState",
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        return execute_ifelse(node, state, inputs)
