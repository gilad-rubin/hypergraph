"""Durable pause slots (PRD 0010).

A pause used to die with the process: ``PauseInfo`` (question value + answer
port) lived only on the in-memory ``RunResult``, and the persisted paused
StepRecord carried neither. A new process could not discover what was asked,
which port answers it, or which occurrence is current.

This module turns one ``PauseExecution`` into a durable
:class:`~hypergraph.checkpointers.types.PauseSlot` and hands it to the
checkpointer as ONE atomic commit together with the paused step's records and
the run's transition to ``PAUSED``. Sync and async templates share every
decision here — the slot shape, the capability probe, and the write point — so
the two paths cannot drift.

Three rules the slot obeys:

- **The question is a JSON-safe projection.** The live handler payload never
  enters the journal; evidence that cannot be serialized is replaced by a
  type marker rather than a ``repr`` (raw values can embed secrets, #233).
- **The answer contract is data, not a callable.** ``answer_type`` is read
  from the graph's ``InterruptNode`` and rendered as JSON Schema
  (``checkpointers/_answer_schema.py``).
- **The address is parent-facing.** For a nested interrupt the slot names the
  delegating ``GraphNode`` in THIS run — exactly the address its paused
  StepRecord carries — while the child run records its own slot under the
  child workflow id. ``node_path`` keeps the full ``nested/approval`` display
  path.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hypergraph.checkpointers._answer_schema import render_answer_schema
from hypergraph.checkpointers.protocols import PauseSlotProtocol, SyncPauseSlotProtocol
from hypergraph.checkpointers.types import PauseSlot, WorkflowStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hypergraph.checkpointers.types import StepRecord
    from hypergraph.graph import Graph
    from hypergraph.runners._shared.results import PauseInfo
    from hypergraph.runners._shared.state import PauseExecution

#: Every method of each seam, not just the one the write path calls — see the
#: same reasoning in ``pending_boundaries``: ``runtime_checkable`` matches on
#: attribute PRESENCE only.
_ASYNC_PAUSE_METHODS = ("record_pause", "get_pause_slot", "settle_pause")
_SYNC_PAUSE_METHODS = ("record_pause_sync", "get_pause_slot_sync", "settle_pause_sync")

#: Marker for question evidence that cannot be JSON-serialized. The type name
#: is diagnostic; the value itself never reaches the journal.
_UNSERIALIZABLE_KEY = "__unserializable__"


def supports_pause_slots(checkpointer: object | None, *, sync: bool) -> bool:
    """Whether this checkpointer can persist and settle durable pause slots.

    ``None`` (no checkpointer) answers False, so callers need no null guard.
    A checkpointer without the seam keeps working through the plain
    save-steps-then-set-status path — it simply has no durable question.
    """
    if checkpointer is None:
        return False
    protocol, methods = (SyncPauseSlotProtocol, _SYNC_PAUSE_METHODS) if sync else (PauseSlotProtocol, _ASYNC_PAUSE_METHODS)
    if not isinstance(checkpointer, protocol):
        return False
    return all(callable(getattr(checkpointer, name, None)) for name in methods)


def build_pause_slot(graph: Graph, workflow_id: str, superstep: int, pause_info: PauseInfo) -> PauseSlot:
    """Project one interrupt occurrence into its durable slot.

    ``superstep`` must be the resume-offset index the paused StepRecord
    carries, so the slot's ``pause_id`` IS that step's address.
    """
    node_path = pause_info.node_name
    # The parent-facing node: for "nested/inner/approval" the parent's paused
    # StepRecord names "nested", and the slot must agree with it.
    node_name = node_path.split("/", 1)[0]
    question_value = pause_info.value
    options = getattr(question_value, "options", None)
    answer_type = _resolve_answer_type(graph, node_path, question_value)
    return PauseSlot(
        run_id=workflow_id,
        superstep=superstep,
        node_name=node_name,
        node_path=node_path,
        response_key=pause_info.response_key,
        question=project_question(question_value),
        answer_schema=render_answer_schema(answer_type, options),
        options=tuple(options) if options is not None else None,
    )


def project_question(question_value: Any) -> dict[str, Any]:
    """JSON-safe projection of the ask payload — never the live object."""
    options = getattr(question_value, "options", None)
    evidence = getattr(question_value, "evidence", ())
    return {
        "prompt": str(getattr(question_value, "prompt", "")),
        "options": None if options is None else [str(option) for option in options],
        "evidence": [_json_safe(item) for item in evidence],
        "answer_type": _stable_type_name(getattr(question_value, "answer_type", None)),
    }


async def commit_pause_async(
    checkpointer: Any,
    graph: Graph,
    workflow_id: str,
    pause: PauseExecution,
    step_records: Sequence[StepRecord],
    *,
    duration_ms: float | None = None,
    node_count: int | None = None,
    error_count: int | None = None,
) -> None:
    """Persist the pause: slot + buffered step records + PAUSED, atomically.

    Falls back to the plain two-write path when the backend has no pause-slot
    seam, or when the occurrence has no superstep to be addressed by (a
    checkpointer-free nested delegation can raise a pause the runner never
    scheduled). The fallback is the pre-slot behavior exactly — never a
    half-written slot.
    """
    slot = _slot_for(graph, workflow_id, pause)
    if slot is not None and supports_pause_slots(checkpointer, sync=False):
        await checkpointer.record_pause(
            slot,
            step_records=tuple(step_records),
            duration_ms=duration_ms,
            node_count=node_count,
            error_count=error_count,
        )
        return
    for record in step_records:
        await checkpointer.save_step(record)
    await checkpointer.update_run_status(
        workflow_id,
        WorkflowStatus.PAUSED,
        duration_ms=duration_ms,
        node_count=node_count,
        error_count=error_count,
    )


def commit_pause_sync(
    checkpointer: Any,
    graph: Graph,
    workflow_id: str,
    pause: PauseExecution,
    step_records: Sequence[StepRecord],
    *,
    duration_ms: float | None = None,
    node_count: int | None = None,
    error_count: int | None = None,
) -> None:
    """Sync mirror of :func:`commit_pause_async`."""
    slot = _slot_for(graph, workflow_id, pause)
    if slot is not None and supports_pause_slots(checkpointer, sync=True):
        checkpointer.record_pause_sync(
            slot,
            step_records=tuple(step_records),
            duration_ms=duration_ms,
            node_count=node_count,
            error_count=error_count,
        )
        return
    for record in step_records:
        checkpointer.save_step_sync(record)
    checkpointer.update_run_status_sync(
        workflow_id,
        WorkflowStatus.PAUSED,
        duration_ms=duration_ms,
        node_count=node_count,
        error_count=error_count,
    )


def _slot_for(graph: Graph, workflow_id: str, pause: PauseExecution) -> PauseSlot | None:
    superstep = pause.superstep
    if superstep is None:
        return None
    return build_pause_slot(graph, workflow_id, superstep, pause.pause_info)


def _resolve_answer_type(graph: Graph, node_path: str, question_value: Any) -> Any:
    """The graph-declared answer type for this occurrence's answer port.

    Graph first (that is what "graph-derived" means, and it survives a handler
    that returned a foreign payload); the payload's own ``answer_type`` is the
    fallback when the path cannot be walked.
    """
    node = _find_interrupt_node(graph, node_path)
    if node is not None:
        declared = node.get_output_type(node.answer_name)
        if declared is not None:
            return declared
    return getattr(question_value, "answer_type", None)


def _find_interrupt_node(graph: Graph, node_path: str) -> Any:
    """Walk a ``graphnode/.../interrupt`` path down to the InterruptNode."""
    from hypergraph.nodes.graph_node import GraphNode
    from hypergraph.nodes.interrupt import InterruptNode

    current: Any = graph
    segments = node_path.split("/")
    for index, segment in enumerate(segments):
        node = getattr(current, "_nodes", {}).get(segment)
        if node is None:
            return None
        if index == len(segments) - 1:
            return node if isinstance(node, InterruptNode) else None
        if not isinstance(node, GraphNode):
            return None
        current = node.graph
    return None


def _stable_type_name(value: Any) -> str | None:
    """Display-stable name for a declared type (mirrors the HyperTable seam)."""
    if value is None:
        return None
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    return repr(value)


def _json_safe(value: Any) -> Any:
    """Coerce one evidence item into something the journal can hold."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        pass
    else:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return {_UNSERIALIZABLE_KEY: type(value).__name__}
