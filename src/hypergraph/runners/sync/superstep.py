"""Superstep execution for sync runner."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.gate import IfElseNode, RouteNode
from hypergraph.runners._shared.helpers import collect_inputs_for_node
from hypergraph.runners._shared.types import GraphState, NodeExecution

if TYPE_CHECKING:
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.graph import Graph


def run_superstep_sync(
    graph: "Graph",
    state: GraphState,
    ready_nodes: list[HyperNode],
    provided_values: dict[str, Any],
    execute_node: Callable,
    *,
    dispatcher: "EventDispatcher | None" = None,
    run_id: str = "",
    run_span_id: str = "",
) -> GraphState:
    """Execute one superstep: run all ready nodes and update state.

    In sync mode, nodes are executed sequentially.

    Args:
        graph: The graph being executed
        state: Current state (will be copied, not mutated)
        ready_nodes: Nodes to execute in this superstep
        provided_values: Values provided to runner.run()
        execute_node: Function to execute a single node
        dispatcher: Optional event dispatcher for emitting node events
        run_id: Run ID for event correlation
        run_span_id: Span ID of the parent run

    Returns:
        New state with updated values and versions
    """
    new_state = state.copy()

    for node in ready_nodes:
        # Use original state snapshot for input collection to ensure all nodes
        # in this superstep see the same values (deterministic execution order)
        inputs = collect_inputs_for_node(node, graph, state, provided_values)

        # Record input versions from the snapshot we used
        input_versions = {param: state.get_version(param) for param in node.inputs}

        # Emit NodeStartEvent
        node_span_id = _emit_node_start(dispatcher, run_id, run_span_id, node, graph)

        node_start = time.time()
        try:
            # Snapshot routing decisions before execution to detect new decisions
            routing_before = set(new_state.routing_decisions.keys())

            # Set node span_id on executor for nested graph propagation
            if hasattr(execute_node, "current_span_id"):
                execute_node.current_span_id[0] = node_span_id  # type: ignore[attr-defined]

            # Execute node
            outputs = execute_node(node, new_state, inputs)

            duration_ms = (time.time() - node_start) * 1000

            # Emit RouteDecisionEvent if this was a routing node
            _emit_route_decision(
                dispatcher, run_id, run_span_id, node, graph, new_state, routing_before,
            )

            # Emit NodeEndEvent
            _emit_node_end(dispatcher, run_id, node_span_id, run_span_id, node, graph, duration_ms)

        except Exception:
            duration_ms = (time.time() - node_start) * 1000
            _emit_node_error(dispatcher, run_id, node_span_id, run_span_id, node, graph)
            raise

        # Update state with outputs
        for name, value in outputs.items():
            new_state.update_value(name, value)

        # Record execution
        new_state.node_executions[node.name] = NodeExecution(
            node_name=node.name,
            input_versions=input_versions,
            outputs=outputs,
        )

    return new_state


# ------------------------------------------------------------------
# Event emission helpers
# ------------------------------------------------------------------


def _emit_node_start(
    dispatcher: "EventDispatcher | None",
    run_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: "Graph",
) -> str:
    """Emit NodeStartEvent. Returns the node's span_id."""
    from hypergraph.events.types import _generate_span_id

    span_id = _generate_span_id()

    if dispatcher is None or not dispatcher.active:
        return span_id

    from hypergraph.events.types import NodeStartEvent

    dispatcher.emit(NodeStartEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
    ))
    return span_id


def _emit_node_end(
    dispatcher: "EventDispatcher | None",
    run_id: str,
    node_span_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: "Graph",
    duration_ms: float,
) -> None:
    """Emit NodeEndEvent."""
    if dispatcher is None or not dispatcher.active:
        return

    from hypergraph.events.types import NodeEndEvent

    dispatcher.emit(NodeEndEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
        duration_ms=duration_ms,
    ))


def _emit_node_error(
    dispatcher: "EventDispatcher | None",
    run_id: str,
    node_span_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: "Graph",
) -> None:
    """Emit NodeErrorEvent for the current exception."""
    if dispatcher is None or not dispatcher.active:
        return

    import sys

    from hypergraph.events.types import NodeErrorEvent

    exc_type, exc_val, _ = sys.exc_info()
    dispatcher.emit(NodeErrorEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
        error=str(exc_val) if exc_val else "",
        error_type=f"{exc_type.__module__}.{exc_type.__qualname__}" if exc_type else "",
    ))


def _emit_route_decision(
    dispatcher: "EventDispatcher | None",
    run_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: "Graph",
    state: GraphState,
    routing_before: set[str],
) -> None:
    """Emit RouteDecisionEvent if the node made a routing decision."""
    if dispatcher is None or not dispatcher.active:
        return
    if not isinstance(node, (RouteNode, IfElseNode)):
        return

    # Check if this node made a routing decision
    if node.name not in state.routing_decisions:
        return

    from hypergraph.events.types import RouteDecisionEvent

    decision = state.routing_decisions[node.name]
    dispatcher.emit(RouteDecisionEvent(
        run_id=run_id,
        span_id=run_span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
        decision=decision,
    ))
