"""Sync executor for HyperTable materialization nodes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.materialization._types import MaterializationReceipt, RowReceipt

if TYPE_CHECKING:
    from hypergraph.materialization._node import MaterializationNode
    from hypergraph.runners._shared.state import ExecutionContext, GraphState


class SyncMaterializationNodeExecutor:
    def __call__(
        self,
        node: MaterializationNode,
        state: GraphState,
        inputs: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        del state
        operation = node.table._write_planner.insert([node.map_inputs_to_params(inputs)])
        receipt = node.table._drive_sync(
            operation,
            event_processors=ctx.event_processors,
            parent_span_id=ctx.parent_span_id,
            parent_run_id=ctx.workflow_id,
        )
        row_receipt = receipt.receipts[0]
        assert isinstance(row_receipt, RowReceipt)
        return {node.outputs[0]: MaterializationReceipt.from_row_receipt(row_receipt)}
