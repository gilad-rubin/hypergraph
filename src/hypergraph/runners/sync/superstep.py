"""Superstep execution for sync runner."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from hypergraph.exceptions import ExecutionError
from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.caching import (
    check_cache,
    restore_routing_decision,
    store_in_cache,
)
from hypergraph.runners._shared.helpers import collect_inputs_for_node
from hypergraph.runners._shared.event_helpers import (
    build_cache_hit_event,
    build_node_end_event,
    build_node_error_event,
    build_node_start_event,
    build_route_decision_event,
)
from hypergraph.runners._shared.types import GraphState, NodeExecution

if TYPE_CHECKING:
    from hypergraph.cache import CacheBackend
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.graph import Graph


def run_superstep_sync(
    graph: "Graph",
    state: GraphState,
    ready_nodes: list[HyperNode],
    provided_values: dict[str, Any],
    execute_node: Callable,
    *,
    cache: "CacheBackend | None" = None,
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
    active = dispatcher is not None and dispatcher.active

    for node in ready_nodes:
        # Use original state snapshot for input collection to ensure all nodes
        # in this superstep see the same values (deterministic execution order)
        inputs = collect_inputs_for_node(node, graph, state, provided_values)

        # Record input versions from the snapshot we used
        input_versions = {param: state.get_version(param) for param in node.inputs}

        # Check cache before execution
        cache_key, cached_outputs = ("", None)
        if cache is not None:
            cache_key, cached_outputs = check_cache(node, inputs, cache)

        if cached_outputs is not None:
            outputs = cached_outputs
            restore_routing_decision(node, outputs, new_state)
            # Emit NodeStartEvent -> CacheHitEvent -> NodeEndEvent(cached=True)
            node_span_id, start_evt = build_node_start_event(run_id, run_span_id, node, graph)
            if active:
                dispatcher.emit(start_evt)
                dispatcher.emit(build_cache_hit_event(run_id, node_span_id, run_span_id, node, graph, cache_key))
                route_evt = build_route_decision_event(run_id, run_span_id, node, graph, new_state)
                if route_evt is not None:
                    dispatcher.emit(route_evt)
                dispatcher.emit(build_node_end_event(run_id, node_span_id, run_span_id, node, graph, duration_ms=0.0, cached=True))
        else:
            # Emit NodeStartEvent
            node_span_id, start_evt = build_node_start_event(run_id, run_span_id, node, graph)
            if active:
                dispatcher.emit(start_evt)

            node_start = time.time()
            try:
                # Set node span_id on executor for nested graph propagation
                if hasattr(execute_node, "current_span_id"):
                    execute_node.current_span_id[0] = node_span_id  # type: ignore[attr-defined]

                # Execute node
                outputs = execute_node(node, new_state, inputs)

                duration_ms = (time.time() - node_start) * 1000

                # Store result in cache
                if cache is not None and cache_key:
                    store_in_cache(node, outputs, new_state, cache, cache_key)

                if active:
                    route_evt = build_route_decision_event(run_id, run_span_id, node, graph, new_state)
                    if route_evt is not None:
                        dispatcher.emit(route_evt)
                    dispatcher.emit(build_node_end_event(run_id, node_span_id, run_span_id, node, graph, duration_ms))

            except Exception as e:
                duration_ms = (time.time() - node_start) * 1000
                if active:
                    dispatcher.emit(build_node_error_event(run_id, node_span_id, run_span_id, node, graph))
                raise ExecutionError(e, new_state) from e

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
