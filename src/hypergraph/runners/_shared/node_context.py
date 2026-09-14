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
    records_on_loop: bool = False,
) -> NodeContext:
    """Build a NodeContext for executor injection.

    Reads the StopSignal from the contextvar (set by the runner).
    Falls back to an unset signal if none is active. The correlation
    fields (graph_name, workflow_id, item_index, parent_span_id) are
    stamped onto every ``StreamingChunkEvent`` the node emits.

    ``checkpointer`` is ``ExecutionContext.checkpointer`` — the active
    persistence for THIS run, already ``None`` unless a checkpointer and a
    workflow_id are both present — and is what ``record`` writes through.
    ``records_on_loop`` is the async executor's promise to call
    ``flush_node_records`` on this context; only it may defer a write.
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
        records_on_loop=records_on_loop,
    )


async def flush_node_records(context: Any, *, node_failed: bool) -> None:
    """Await every fact a coroutine node recorded. No-op for anything else.

    Called by the async executor after the node body settles and BEFORE the
    step record is written, so a fact is durable by the time the step that
    produced it is, and the log reads ``fact… step`` the way the sync family
    writes it.

    A failed append is the NODE's failure when the node itself succeeded: a
    node that believes its fact is durable must not report success over a
    write that never landed. When the node is already failing, its own
    exception is the one worth seeing, so the append error is logged instead
    of replacing it.
    """
    tasks = getattr(context, "_record_tasks", None)
    if tasks is None:
        return
    import asyncio

    # Writes that finished while the node ran are already gone from the set,
    # and any failure they left is on the context. So this awaits only what is
    # still in flight and then reads the earliest failure from either place —
    # the context first, since those writes settled first.
    pending = list(tasks)
    tasks.clear()
    results = await asyncio.gather(*pending, return_exceptions=True) if pending else []
    failure = context._record_failure or next((outcome for outcome in results if isinstance(outcome, BaseException)), None)
    context._record_failure = None
    if failure is None:
        return
    if node_failed:
        import logging

        logging.getLogger("hypergraph.runners").warning(
            "node %r failed and at least one of its recorded facts could not be written: %r",
            getattr(context, "_node_name", "?"),
            failure,
        )
        return
    raise failure
