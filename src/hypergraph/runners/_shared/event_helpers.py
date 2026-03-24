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
    from hypergraph.runners._shared.types import GraphState


def create_dispatcher(
    processors: list[EventProcessor] | None,
) -> EventDispatcher:
    """Create an EventDispatcher from processor list."""
    from hypergraph.events.dispatcher import EventDispatcher

    return EventDispatcher(processors)


# ------------------------------------------------------------------
# Superstep-level helpers (node events)
# ------------------------------------------------------------------


def build_node_start_event(
    run_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: Graph,
    *,
    workflow_id: str | None = None,
    item_index: int | None = None,
    superstep: int | None = None,
) -> tuple[str, Any]:
    """Build a NodeStartEvent. Returns (span_id, event)."""
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
) -> Any:
    """Build a NodeEndEvent."""
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
    """Build a NodeErrorEvent from the current exception context."""
    from hypergraph.events.types import NodeErrorEvent

    exc_type, exc_val, _ = sys.exc_info()
    return NodeErrorEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        workflow_id=workflow_id,
        item_index=item_index,
        node_name=node.name,
        graph_name=graph.name,
        error=str(exc_val) if exc_val else "",
        error_type=f"{exc_type.__module__}.{exc_type.__qualname__}" if exc_type else "",
        superstep=superstep,
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
    from hypergraph.runners._shared.types import _generate_run_id

    run_id = _generate_run_id()
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
    """Build a RunEndEvent."""
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
        error=str(error) if error else None,
        duration_ms=duration_ms,
        batch_total_items=batch_summary.total_items if batch_summary is not None else None,
        batch_completed_items=batch_summary.completed_items if batch_summary is not None else None,
        batch_failed_items=batch_summary.failed_items if batch_summary is not None else None,
        batch_paused_items=batch_summary.paused_items if batch_summary is not None else None,
        batch_stopped_items=batch_summary.stopped_items if batch_summary is not None else None,
        batch_outcome=batch_summary.outcome if batch_summary is not None else None,
    )
