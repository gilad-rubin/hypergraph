"""Builder for the per-execution ``NodeContext`` the executors inject.

The type itself is public and lives in :mod:`hypergraph.runners.context`; only
this construction step — which reads the runner's stop signal out of a
contextvar — is runner-internal. ``NodeContext`` is re-exported here so the
existing executor import site keeps one import.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hypergraph.runners.context import NodeContext as NodeContext

if TYPE_CHECKING:
    from hypergraph.checkpointers.base import Checkpointer


def _noop_emit(event: Any) -> None:
    """Fallback emit function when no dispatcher is configured."""


def build_node_context(
    node_name: str,
    emit_fn: Callable[[Any], None] | None,
    run_id: str = "",
    *,
    graph_name: str = "",
    workflow_id: str | None = None,
    item_index: int | None = None,
    parent_span_id: str | None = None,
    checkpointer: Checkpointer | None = None,
) -> NodeContext:
    """Build a NodeContext for executor injection.

    Reads the StopSignal from the contextvar (set by the runner).
    Falls back to an unset signal if none is active. The correlation
    fields (graph_name, workflow_id, item_index, parent_span_id) are
    stamped onto every ``StreamingChunkEvent`` the node emits.

    ``checkpointer`` is ``ExecutionContext.checkpointer`` — the active
    persistence for THIS run, already ``None`` unless a checkpointer and a
    workflow_id are both present — and is what ``record`` writes through.
    """
    from hypergraph.runners._shared.stop import StopSignal, get_stop_signal

    signal = get_stop_signal() or StopSignal()
    return NodeContext(
        signal,
        emit_fn or _noop_emit,
        node_name,
        run_id=run_id,
        graph_name=graph_name,
        workflow_id=workflow_id,
        item_index=item_index,
        parent_span_id=parent_span_id,
        checkpointer=checkpointer,
    )
