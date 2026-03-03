"""Shared event construction and emission helpers for runners.

Event objects are identical between sync and async paths. These helpers
build the events; callers choose emit() vs emit_async().
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Any

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
) -> tuple[str, Any]:
    """Build a NodeStartEvent. Returns (span_id, event)."""
    from hypergraph.events.types import NodeStartEvent, _generate_span_id

    span_id = _generate_span_id()
    event = NodeStartEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
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
) -> Any:
    """Build a NodeEndEvent."""
    from hypergraph.events.types import NodeEndEvent

    return NodeEndEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
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
) -> Any:
    """Build a CacheHitEvent."""
    from hypergraph.events.types import CacheHitEvent

    return CacheHitEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
        cache_key=cache_key,
    )


def build_node_error_event(
    run_id: str,
    node_span_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: Graph,
) -> Any:
    """Build a NodeErrorEvent from the current exception context."""
    from hypergraph.events.types import NodeErrorEvent

    exc_type, exc_val, _ = sys.exc_info()
    return NodeErrorEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
        error=str(exc_val) if exc_val else "",
        error_type=f"{exc_type.__module__}.{exc_type.__qualname__}" if exc_type else "",
    )


def build_route_decision_event(
    run_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: Graph,
    state: GraphState,
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
        node_name=node.name,
        graph_name=graph.name,
        decision=state.routing_decisions[node.name],
    )


# ------------------------------------------------------------------
# Runner-level helpers (run events)
# ------------------------------------------------------------------


def build_run_start_event(
    graph: Graph,
    parent_span_id: str | None,
    *,
    is_map: bool = False,
    map_size: int | None = None,
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
        graph_name=graph.name,
        is_map=is_map,
        map_size=map_size,
    )
    return run_id, span_id, event


def build_run_end_event(
    run_id: str,
    span_id: str,
    graph: Graph,
    start_time: float,
    parent_span_id: str | None,
    *,
    error: BaseException | None = None,
) -> Any:
    """Build a RunEndEvent."""
    from hypergraph.events.types import RunEndEvent, RunStatus

    duration_ms = (time.time() - start_time) * 1000
    return RunEndEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        graph_name=graph.name,
        status=RunStatus.FAILED if error else RunStatus.COMPLETED,
        error=str(error) if error else None,
        duration_ms=duration_ms,
    )
