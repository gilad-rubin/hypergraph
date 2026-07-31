"""Shared event construction and emission helpers for runners.

Event objects are identical between sync and async paths. These helpers
build the events; callers choose emit() vs emit_async().
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.event_metadata import (
    DEFAULT_RUN_CONTEXT,
    DEFAULT_RUN_LINEAGE,
    BatchSummary,
    RunContext,
    RunLineage,
)

if TYPE_CHECKING:
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph
    from hypergraph.nodes.base import HyperNode
    from hypergraph.runners._shared.state import GraphState


def create_dispatcher(
    processors: list[EventProcessor] | None,
) -> EventDispatcher:
    """Create an EventDispatcher from processor list."""
    from hypergraph.events.dispatcher import EventDispatcher

    return EventDispatcher(processors)


# ------------------------------------------------------------------
# Superstep-level helpers (node events)
# ------------------------------------------------------------------


def trace_io_enabled(node: HyperNode, graph: Graph) -> bool:
    """Resolve node/graph ``trace_io``: an explicit node value wins.

    Precedence is node explicit -> graph default -> off. An exporter can
    still refuse the payloads it receives (the processor-side kill switch),
    which is deliberately NOT consulted here: the runner does not know which
    processors are attached, and the capture itself is a shallow dict copy.
    """
    declared = getattr(node, "trace_io", None)
    if declared is not None:
        return bool(declared)
    return bool(getattr(graph, "trace_io", False))


def build_node_start_event(
    run_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: Graph,
    *,
    workflow_id: str | None = None,
    item_index: int | None = None,
    superstep: int | None = None,
    inputs: dict[str, Any] | None = None,
) -> tuple[str, Any]:
    """Build a NodeStartEvent. Returns (span_id, event).

    ``inputs`` is carried on the event only when the node resolves
    ``trace_io`` on; it feeds span export and never a durable record.
    """
    from hypergraph.events.types import NodeStartEvent, _generate_span_id

    span_id = _generate_span_id()
    event = NodeStartEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=run_span_id,
        workflow_id=workflow_id,
        item_index=item_index,
        node_name=node.name,
        graph_name=graph.name,
        superstep=superstep,
        trace_inputs=dict(inputs) if inputs is not None and trace_io_enabled(node, graph) else None,
    )
    return span_id, event


def build_node_end_event(
    run_id: str,
    node_span_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: Graph,
    duration_ms: float,
    cached: bool = False,
    inner_logs: tuple = (),
    *,
    workflow_id: str | None = None,
    item_index: int | None = None,
    superstep: int | None = None,
    outputs: dict[str, Any] | None = None,
) -> Any:
    """Build a NodeEndEvent.

    ``outputs`` is carried on the event only when the node resolves
    ``trace_io`` on; it feeds span export and never a durable record.
    """
    from hypergraph.events.types import NodeEndEvent

    return NodeEndEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        workflow_id=workflow_id,
        item_index=item_index,
        node_name=node.name,
        graph_name=graph.name,
        superstep=superstep,
        duration_ms=duration_ms,
        cached=cached,
        inner_logs=inner_logs,
        trace_outputs=dict(outputs) if outputs is not None and trace_io_enabled(node, graph) else None,
    )


def build_cache_hit_event(
    run_id: str,
    node_span_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: Graph,
    cache_key: str,
    *,
    workflow_id: str | None = None,
    item_index: int | None = None,
    superstep: int | None = None,
) -> Any:
    """Build a CacheHitEvent."""
    from hypergraph.events.types import CacheHitEvent

    return CacheHitEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        workflow_id=workflow_id,
        item_index=item_index,
        node_name=node.name,
        graph_name=graph.name,
        cache_key=cache_key,
        superstep=superstep,
    )


def build_node_error_event(
    run_id: str,
    node_span_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: Graph,
    *,
    workflow_id: str | None = None,
    item_index: int | None = None,
    superstep: int | None = None,
) -> Any:
    """Build a NodeErrorEvent from the current exception context.

    ``error`` carries the privacy-safe projection — never ``str(exception)`` —
    and is what durable surfaces (RunLog, StepRecord, checkpoints, the attempt
    ledger) store. ``error_detail`` carries the unredacted message, type and
    traceback for live consumers such as the OTel export; it is never
    persisted. The exact exception object stays on local surfaces only.
    """
    from hypergraph.diagnostics import derive_diagnostic, full_error_detail, safe_error_text
    from hypergraph.events.types import NodeErrorEvent

    exc_type, exc_val, _ = sys.exc_info()
    diagnostic = None
    if exc_val is not None:
        diagnostic = derive_diagnostic(
            exc_val,
            node_name=node.name,
            graph_name=graph.name or None,
            superstep=superstep,
            item_index=item_index,
            workflow_id=workflow_id,
        )
    return NodeErrorEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        workflow_id=workflow_id,
        item_index=item_index,
        node_name=node.name,
        graph_name=graph.name,
        error=safe_error_text(exc_val, node_name=node.name) if exc_val else "",
        error_type=f"{exc_type.__module__}.{exc_type.__qualname__}" if exc_type else "",
        superstep=superstep,
        diagnostic=diagnostic,
        error_detail=full_error_detail(exc_val) if exc_val is not None else None,
    )


def build_route_decision_event(
    run_id: str,
    run_span_id: str,
    node_span_id: str,
    node: HyperNode,
    graph: Graph,
    state: GraphState,
    *,
    workflow_id: str | None = None,
    item_index: int | None = None,
    superstep: int | None = None,
) -> Any | None:
    """Build a RouteDecisionEvent if the node made a routing decision.

    Returns None if the node is not a gate or hasn't made a decision.
    """
    from hypergraph.nodes.gate import IfElseNode, RouteNode

    if not isinstance(node, (RouteNode, IfElseNode)):
        return None
    if node.name not in state.routing_decisions:
        return None

    from hypergraph.events.types import RouteDecisionEvent

    return RouteDecisionEvent(
        run_id=run_id,
        parent_span_id=run_span_id,
        workflow_id=workflow_id,
        item_index=item_index,
        node_name=node.name,
        graph_name=graph.name,
        decision=state.routing_decisions[node.name],
        node_span_id=node_span_id,
        superstep=superstep,
    )


# ------------------------------------------------------------------
# Runner-level helpers (run events)
# ------------------------------------------------------------------


def build_run_start_event(
    graph: Graph,
    parent_span_id: str | None,
    *,
    context: RunContext = DEFAULT_RUN_CONTEXT,
    is_map: bool = False,
    map_size: int | None = None,
    lineage: RunLineage = DEFAULT_RUN_LINEAGE,
) -> tuple[str, str, Any]:
    """Build a RunStartEvent. Returns (run_id, span_id, event)."""
    from hypergraph.events.types import RunStartEvent, _generate_span_id
    from hypergraph.runners._shared.results import generate_run_id

    run_id = generate_run_id()
    span_id = _generate_span_id()
    event = RunStartEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        workflow_id=context.workflow_id,
        item_index=context.item_index,
        graph_name=graph.name,
        is_map=is_map,
        map_size=map_size,
        parent_workflow_id=lineage.parent_workflow_id,
        forked_from=lineage.forked_from,
        fork_superstep=lineage.fork_superstep,
        retry_of=lineage.retry_of,
        retry_index=lineage.retry_index,
        is_resume=lineage.is_resume,
    )
    return run_id, span_id, event


def build_run_end_event(
    run_id: str,
    span_id: str,
    graph: Graph,
    start_time: float,
    parent_span_id: str | None,
    *,
    context: RunContext = DEFAULT_RUN_CONTEXT,
    status: str | None = None,
    error: BaseException | None = None,
    batch_summary: BatchSummary | None = None,
) -> Any:
    """Build a RunEndEvent.

    ``error`` carries the privacy-safe projection — never ``str(exception)`` —
    and is what durable surfaces store. ``error_detail`` carries the
    unredacted message, type and traceback for live consumers; it is never
    persisted.
    """
    from hypergraph.diagnostics import full_error_detail, safe_error_text
    from hypergraph.events.types import RunEndEvent, RunStatus

    duration_ms = (time.time() - start_time) * 1000
    return RunEndEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        workflow_id=context.workflow_id,
        item_index=context.item_index,
        graph_name=graph.name,
        status=RunStatus(status) if status is not None else (RunStatus.FAILED if error else RunStatus.COMPLETED),
        error=safe_error_text(error) if error else None,
        error_detail=full_error_detail(error) if error else None,
        duration_ms=duration_ms,
        batch_total_items=batch_summary.total_items if batch_summary is not None else None,
        batch_completed_items=batch_summary.completed_items if batch_summary is not None else None,
        batch_failed_items=batch_summary.failed_items if batch_summary is not None else None,
        batch_paused_items=batch_summary.paused_items if batch_summary is not None else None,
        batch_stopped_items=batch_summary.stopped_items if batch_summary is not None else None,
        batch_restored_items=batch_summary.restored_items if batch_summary is not None else None,
        batch_outcome=batch_summary.outcome if batch_summary is not None else None,
    )
