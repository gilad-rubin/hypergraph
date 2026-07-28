"""Async executor for HyperTable materialization nodes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph._thread_settle import to_thread_settled
from hypergraph.materialization._types import MaterializationReceipt, RowReceipt

if TYPE_CHECKING:
    from hypergraph.materialization._node import MaterializationNode
    from hypergraph.runners._shared.state import ExecutionContext, GraphState


class AsyncMaterializationNodeExecutor:
    async def __call__(
        self,
        node: MaterializationNode,
        state: GraphState,
        inputs: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        del state
        operation = node.table._write_planner.insert([node.map_inputs_to_params(inputs)])
        options = {
            "event_processors": ctx.event_processors,
            "parent_span_id": ctx.parent_span_id,
            "parent_run_id": ctx.workflow_id,
        }
        if node.table._is_async_runner():
            receipt = await node.table._drive_async(operation, **options)
        else:
            receipt = await to_thread_settled(node.table._drive_sync, operation, **options)
        row_receipt = receipt.receipts[0]
        assert isinstance(row_receipt, RowReceipt)
        return {node.outputs[0]: MaterializationReceipt.from_row_receipt(row_receipt)}
