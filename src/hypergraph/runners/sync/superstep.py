"""Superstep execution for sync runner."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from hypergraph.exceptions import ExecutionError
from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.caching import (
    check_cache,
    restore_routing_decision,
    store_in_cache,
)
from hypergraph.runners._shared.event_helpers import (
    build_cache_hit_event,
    build_node_end_event,
    build_node_error_event,
    build_node_start_event,
    build_route_decision_event,
)
from hypergraph.runners._shared.helpers import apply_node_result, collect_inputs_for_node
from hypergraph.runners._shared.inspect import FailureCase
from hypergraph.runners._shared.types import ExecutionContext, GraphState, PauseExecution

if TYPE_CHECKING:
    from hypergraph.cache import CacheBackend
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.graph import Graph
    from hypergraph.runners._shared.protocols import NodeExecutor


def run_superstep_sync(
    graph: Graph,
    state: GraphState,
    ready_nodes: list[HyperNode],
    provided_values: dict[str, Any],
    executors: dict[type[HyperNode], NodeExecutor[Any]],
    ctx_base: ExecutionContext,
    *,
    superstep_idx: int = 0,
    cache: CacheBackend | None = None,
    dispatcher: EventDispatcher | None = None,
    run_id: str = "",
    run_span_id: str = "",
    workflow_id: str | None = None,
    item_index: int | None = None,
) -> GraphState:
    """Execute one superstep: run all ready nodes and update state.

    In sync mode, nodes are executed sequentially.

    Args:
        graph: The graph being executed
        state: Current state (will be copied, not mutated)
        ready_nodes: Nodes to execute in this superstep
        provided_values: Values provided to runner.run()
        executors: Runner executor registry
        ctx_base: Per-run execution context
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
            cached_started_at_ms = (time.time() - ctx_base.run_started_at) * 1000 if ctx_base.run_started_at else None
            if ctx_base.on_node_snapshot is not None:
                ctx_base.on_node_snapshot(
                    node.name,
                    superstep_idx,
                    inputs,
                    outputs,
                    0.0,
                    cached_started_at_ms,
                    cached_started_at_ms,
                    True,
                )
            # Emit NodeStartEvent -> CacheHitEvent -> NodeEndEvent(cached=True)
            node_span_id, start_evt = build_node_start_event(
                run_id,
                run_span_id,
                node,
                graph,
                workflow_id=workflow_id,
                item_index=item_index,
                superstep=superstep_idx,
            )
            if active:
                dispatcher.emit(start_evt)
                dispatcher.emit(
                    build_cache_hit_event(
                        run_id,
                        node_span_id,
                        run_span_id,
                        node,
                        graph,
                        cache_key,
                        workflow_id=workflow_id,
                        item_index=item_index,
                        superstep=superstep_idx,
                    )
                )
                route_evt = build_route_decision_event(
                    run_id,
                    run_span_id,
                    node_span_id,
                    node,
                    graph,
                    new_state,
                    workflow_id=workflow_id,
                    item_index=item_index,
                    superstep=superstep_idx,
                )
                if route_evt is not None:
                    dispatcher.emit(route_evt)
                dispatcher.emit(
                    build_node_end_event(
                        run_id,
                        node_span_id,
                        run_span_id,
                        node,
                        graph,
                        duration_ms=0.0,
                        cached=True,
                        workflow_id=workflow_id,
                        item_index=item_index,
                        superstep=superstep_idx,
                    )
                )
        else:
            # Emit NodeStartEvent
            node_span_id, start_evt = build_node_start_event(
                run_id,
                run_span_id,
                node,
                graph,
                workflow_id=workflow_id,
                item_index=item_index,
                superstep=superstep_idx,
            )
            if active:
                dispatcher.emit(start_evt)

            node_start = time.time()
            started_at_ms = (node_start - ctx_base.run_started_at) * 1000 if ctx_base.run_started_at else None
            if ctx_base.on_node_start is not None:
                ctx_base.on_node_start(node.name, superstep_idx, inputs, started_at_ms)
            try:
                executor = executors.get(type(node))
                if executor is None:
                    raise TypeError(f"No executor registered for node type '{type(node).__name__}'")

                inner_logs: list = []
                ctx = replace(
                    ctx_base,
                    parent_span_id=node_span_id,
                    provided_values=provided_values,
                    on_inner_log=inner_logs.append,
                )

                # Execute node
                outputs = executor(node, new_state, inputs, ctx)

                node_end = time.time()
                duration_ms = (node_end - node_start) * 1000
                ended_at_ms = (node_end - ctx_base.run_started_at) * 1000 if ctx_base.run_started_at else None

                # Store result in cache
                if cache is not None and cache_key:
                    store_in_cache(node, outputs, new_state, cache, cache_key)

                if ctx_base.on_node_snapshot is not None:
                    ctx_base.on_node_snapshot(
                        node.name,
                        superstep_idx,
                        inputs,
                        outputs,
                        duration_ms,
                        started_at_ms,
                        ended_at_ms,
                        False,
                    )

                if active:
                    route_evt = build_route_decision_event(
                        run_id,
                        run_span_id,
                        node_span_id,
                        node,
                        graph,
                        new_state,
                        workflow_id=workflow_id,
                        item_index=item_index,
                        superstep=superstep_idx,
                    )
                    if route_evt is not None:
                        dispatcher.emit(route_evt)
                    dispatcher.emit(
                        build_node_end_event(
                            run_id,
                            node_span_id,
                            run_span_id,
                            node,
                            graph,
                            duration_ms,
                            inner_logs=tuple(inner_logs),
                            workflow_id=workflow_id,
                            item_index=item_index,
                            superstep=superstep_idx,
                        )
                    )

            except BaseException as e:
                node_end = time.time()
                duration_ms = (node_end - node_start) * 1000
                ended_at_ms = (node_end - ctx_base.run_started_at) * 1000 if ctx_base.run_started_at else None
                if active:
                    dispatcher.emit(
                        build_node_error_event(
                            run_id,
                            node_span_id,
                            run_span_id,
                            node,
                            graph,
                            workflow_id=workflow_id,
                            item_index=item_index,
                            superstep=superstep_idx,
                        )
                    )
                # Re-raise PauseExecution unwrapped (needed for InterruptNode)
                if isinstance(e, PauseExecution):
                    raise
                # Wrap only Exception subclasses
                if isinstance(e, Exception):
                    raise ExecutionError(
                        e,
                        new_state,
                        failure_case=FailureCase(
                            node_name=node.name,
                            error=e,
                            inputs=dict(inputs),
                            superstep=superstep_idx,
                            duration_ms=duration_ms,
                            started_at_ms=started_at_ms,
                            ended_at_ms=ended_at_ms,
                        ),
                    ) from e
                # Re-raise other BaseExceptions (KeyboardInterrupt, SystemExit, etc.)
                raise

        # Record wait_for versions
        wait_for_versions = {name: state.get_version(name) for name in node.wait_for}
        apply_node_result(
            graph,
            new_state,
            node,
            outputs,
            input_versions,
            wait_for_versions,
            duration_ms=0.0 if cached_outputs is not None else duration_ms,
            cached=cached_outputs is not None,
        )

    return new_state
