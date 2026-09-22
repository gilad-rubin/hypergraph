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
    from asyncio import AbstractEventLoop

    from hypergraph.checkpointers.base import Checkpointer
    from hypergraph.runners._shared.state import ExecutionContext


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
    record_loop: AbstractEventLoop | None = None,
) -> NodeContext:
    """Build a NodeContext for executor injection.

    Reads the StopSignal from the contextvar (set by the runner).
    Falls back to an unset signal if none is active. The correlation
    fields (graph_name, workflow_id, item_index, parent_span_id) are
    stamped onto every ``StreamingChunkEvent`` the node emits.

    ``checkpointer`` is ``ExecutionContext.checkpointer`` — the active
    persistence for THIS run, already ``None`` unless a checkpointer and a
    workflow_id are both present — and is what ``record`` writes through.
    ``record_loop`` is the executor's OWN event loop — the async executor
    hands over the loop it is running on, and it is the one that settles the
    tasks planted there. ``record`` defers a write to that loop and to no
    other; a body anywhere else writes straight through on its own thread.
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
        record_loop=record_loop,
    )


def node_context_for(
    node: Any,
    ctx: ExecutionContext,
    *,
    record_loop: AbstractEventLoop | None = None,
) -> NodeContext | None:
    """The context a node declares, or ``None`` when it declares none.

    One reader of ``_context_param`` for every executor that injects, so a
    new node kind cannot strip ``ctx`` at build time and forget to hand it
    back at run time. Every id comes from ``ctx``, the node's OWN
    per-node ``ExecutionContext`` (its span, its graph, its run), so a gate,
    a handler and a function node inside one run stamp the same identity.

    ``record_loop`` is the executor's own loop when the body may run ON it:
    the async executors pass ``asyncio.get_running_loop()`` so a body there
    defers its ``ctx.record`` to a loop task instead of blocking the loop
    inside a store write. The sync executors pass nothing — that family has
    no executor loop, so every write goes straight through.
    """
    if getattr(node, "_context_param", None) is None:
        return None
    return build_node_context(
        node.name,
        ctx.emit_fn,
        run_id=ctx.run_id,
        graph_name=ctx.graph_name,
        workflow_id=ctx.workflow_id,
        item_index=ctx.item_index,
        parent_span_id=ctx.parent_span_id,
        checkpointer=ctx.checkpointer,
        record_loop=record_loop,
    )


async def settle_node_records(context: Any, *, node_failed: bool) -> None:
    """Await every fact recorded ON THIS LOOP, then apply the policy.

    Called by the async executor after the node body settles and BEFORE the
    step record is written, so a fact is durable by the time the step that
    produced it is, and the log reads ``fact… step`` the way the thread path
    writes it. A body this executor dispatched to a thread plants no tasks,
    so for it this is the captured failure and nothing else.
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
    _resolve_record_failure(context, failure, node_failed=node_failed)


def settle_node_records_sync(context: Any, *, node_failed: bool) -> None:
    """Sync mirror of :func:`settle_node_records`.

    This family plants no tasks (``record_loop`` is ``None``, so every write
    went straight through on this thread), leaving nothing to await — only
    the captured failure to answer for.
    """
    if getattr(context, "_record_tasks", None) is None:
        return
    _resolve_record_failure(context, context._record_failure, node_failed=node_failed)


def _resolve_record_failure(context: Any, failure: BaseException | None, *, node_failed: bool) -> None:
    """What a lost fact costs — the ONE policy, reached by both mirrors.

    A failed append is the NODE's failure when the node itself succeeded: a
    node that believes its fact is durable must not report success over a
    write that never landed. A ``try/except`` in the body therefore changes
    the body's own control flow and never the node's outcome. When the node
    is already failing, its own exception is the one worth seeing, so the
    append error is logged instead of replacing it — unless the body was
    handed that very exception at the ``record`` call, which is the whole
    reason a thread-path body stopped, and does not need saying twice.
    """
    context._record_failure = None
    told_at_the_call = context._record_failure_raised
    context._record_failure_raised = False
    if failure is None:
        return
    if not node_failed:
        raise failure
    if told_at_the_call:
        return
    import logging

    logging.getLogger("hypergraph.runners").warning(
        "node %r failed and at least one of its recorded facts could not be written: %r",
        getattr(context, "_node_name", "?"),
        failure,
    )
