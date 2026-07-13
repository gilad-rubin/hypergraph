"""Superstep execution for sync runner."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from hypergraph.exceptions import (
    ExecutionError,
    _bind_failure_evidence_invocation,
    _get_failure_evidence_from_context,
    _get_failure_evidence_invocation,
    _NodeExecutionError,
)
from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.runners._shared._inspect import current_inspection
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
from hypergraph.runners._shared.observability import (
    NodeSpanRef,
    reset_current_node_span,
    set_current_node_span,
)
from hypergraph.runners._shared.readiness import apply_node_result
from hypergraph.runners._shared.results import FailureEvidence
from hypergraph.runners._shared.state import ExecutionContext, GraphState, PauseExecution
from hypergraph.runners._shared.value_resolution import address_for_node_input, collect_inputs_for_node

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
    cache: CacheBackend | None = None,
    dispatcher: EventDispatcher | None = None,
    run_id: str = "",
    run_span_id: str = "",
    superstep_idx: int | None = None,
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
    attempted_node_names: list[str] = []

    for node in ready_nodes:
        attempted_node_names.append(node.name)
        # Use original state snapshot for input collection to ensure all nodes
        # in this superstep see the same values (deterministic execution order)
        try:
            inputs = collect_inputs_for_node(node, graph, state, provided_values)
        except Exception as e:
            error = ExecutionError(
                e,
                new_state,
                attempted_node_names=tuple(attempted_node_names),
                node_errors={node.name: e},
            )
            raise error from e

        # Record input versions under the same parent-facing key the staleness
        # check reads.
        input_versions = {(addr := address_for_node_input(node, param)): state.versions.get(addr, 0) for param in node.inputs}

        # Check cache before execution
        cache_key, cached_outputs = ("", None)
        if cache is not None:
            cache_key, cached_outputs = check_cache(node, inputs, cache)

        inspection_context = current_inspection()
        inspection_session = inspection_context[0] if inspection_context is not None else None
        inspection_path = inspection_context[1] if inspection_context is not None else ()
        qualified_name = "/".join((*inspection_path, node.name))

        if cached_outputs is not None:
            outputs = cached_outputs
            restore_routing_decision(node, outputs, new_state)
            # Emit NodeStartEvent -> CacheHitEvent -> NodeEndEvent(cached=True)
            node_span_id, start_evt = build_node_start_event(
                run_id,
                run_span_id,
                node,
                graph,
                workflow_id=ctx_base.workflow_id,
                item_index=ctx_base.item_index,
                superstep=superstep_idx,
            )
            inspection_time_ms = time.perf_counter() * 1000
            if inspection_session is not None:
                inspection_session.start_node(
                    run_id=run_id,
                    span_id=node_span_id,
                    node_name=node.name,
                    qualified_name=qualified_name,
                    graph_name=graph.name or "",
                    item_index=ctx_base.item_index,
                    superstep=superstep_idx if superstep_idx is not None else 0,
                    inputs=inputs,
                    started_at_ms=inspection_time_ms,
                )
                inspection_session.finish_node(
                    span_id=node_span_id,
                    outputs=outputs,
                    ended_at_ms=inspection_time_ms,
                    duration_ms=0.0,
                    cached=True,
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
                        workflow_id=ctx_base.workflow_id,
                        item_index=ctx_base.item_index,
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
                    workflow_id=ctx_base.workflow_id,
                    item_index=ctx_base.item_index,
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
                        workflow_id=ctx_base.workflow_id,
                        item_index=ctx_base.item_index,
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
                workflow_id=ctx_base.workflow_id,
                item_index=ctx_base.item_index,
                superstep=superstep_idx,
            )
            inspection_started_at_ms = time.perf_counter() * 1000
            if inspection_session is not None:
                inspection_session.start_node(
                    run_id=run_id,
                    span_id=node_span_id,
                    node_name=node.name,
                    qualified_name=qualified_name,
                    graph_name=graph.name or "",
                    item_index=ctx_base.item_index,
                    superstep=superstep_idx if superstep_idx is not None else 0,
                    inputs=inputs,
                    started_at_ms=inspection_started_at_ms,
                )
            if active:
                dispatcher.emit(start_evt)

            node_start = time.time()
            node_error_event_attempted = False
            try:
                executor = executors.get(type(node))
                if executor is None:
                    raise TypeError(f"No executor registered for node type '{type(node).__name__}'")

                inner_logs: list = []
                ctx = replace(
                    ctx_base,
                    parent_span_id=node_span_id,
                    graph_name=graph.name,
                    provided_values=provided_values,
                    on_inner_log=inner_logs.append,
                )

                # Publish the node span so in-node telemetry (LLM clients, stores)
                # can attribute itself to this exact span.
                span_token = set_current_node_span(NodeSpanRef(run_id=run_id, span_id=node_span_id, node_name=node.name, graph_name=graph.name))
                evidence_inputs = dict(inputs)
                parent_invocation_token = _get_failure_evidence_invocation()
                invocation_token = object() if isinstance(node, GraphNode) else None
                try:
                    try:
                        # Execute node. This is the only boundary that creates
                        # FailureEvidence; surrounding work is infrastructure.
                        with _bind_failure_evidence_invocation(invocation_token):
                            outputs = executor(node, new_state, inputs, ctx)
                    except BaseException as executor_error:
                        if isinstance(executor_error, PauseExecution):
                            if inspection_session is not None:
                                inspection_session.pause_node(
                                    span_id=node_span_id,
                                    ended_at_ms=time.perf_counter() * 1000,
                                    duration_ms=(time.time() - node_start) * 1000,
                                )
                            raise
                        if isinstance(executor_error, Exception):
                            duration_ms = (time.time() - node_start) * 1000
                            if active:
                                node_error_event_attempted = True
                                dispatcher.emit(
                                    build_node_error_event(
                                        run_id,
                                        node_span_id,
                                        run_span_id,
                                        node,
                                        graph,
                                        workflow_id=ctx_base.workflow_id,
                                        item_index=ctx_base.item_index,
                                        superstep=superstep_idx,
                                    )
                                )
                            if isinstance(node, GraphNode):
                                assert invocation_token is not None
                                inner_failures = (
                                    _get_failure_evidence_from_context(
                                        executor_error,
                                        invocation_token=invocation_token,
                                    )
                                    or ()
                                )
                                node_failures = tuple(replace(failure, node_name=f"{node.name}/{failure.node_name}") for failure in inner_failures)
                            else:
                                node_failures = (
                                    FailureEvidence(
                                        node_name=node.name,
                                        error=executor_error,
                                        inputs=evidence_inputs,
                                        superstep=superstep_idx if superstep_idx is not None else 0,
                                        duration_ms=duration_ms,
                                        graph_name=graph.name or "",
                                        workflow_id=ctx_base.workflow_id,
                                        item_index=ctx_base.item_index,
                                    ),
                                )
                            if inspection_session is not None and node_failures:
                                inspection_failure = replace(
                                    node_failures[0],
                                    node_name="/".join((*inspection_path, node_failures[0].node_name)),
                                )
                                record_failure = not (isinstance(node, GraphNode) and node.runner_override is None and node.map_config is None)
                                inspection_session.fail_node(
                                    span_id=node_span_id,
                                    failure=inspection_failure,
                                    ended_at_ms=time.perf_counter() * 1000,
                                    record_failure=record_failure,
                                )
                            executor_failure = _NodeExecutionError(
                                executor_error,
                                new_state,
                                attempted_node_names=tuple(attempted_node_names),
                                node_errors={node.name: executor_error},
                                node_failures=node_failures,
                                invocation_token=parent_invocation_token,
                            )
                            raise executor_failure from executor_error
                        raise
                finally:
                    reset_current_node_span(span_token)

                duration_ms = (time.time() - node_start) * 1000

                # Store result in cache
                if cache is not None and cache_key:
                    store_in_cache(node, outputs, new_state, cache, cache_key)

                if active:
                    route_evt = build_route_decision_event(
                        run_id,
                        run_span_id,
                        node_span_id,
                        node,
                        graph,
                        new_state,
                        workflow_id=ctx_base.workflow_id,
                        item_index=ctx_base.item_index,
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
                            workflow_id=ctx_base.workflow_id,
                            item_index=ctx_base.item_index,
                            superstep=superstep_idx,
                        )
                    )

                if inspection_session is not None:
                    inspection_session.finish_node(
                        span_id=node_span_id,
                        outputs=outputs,
                        ended_at_ms=time.perf_counter() * 1000,
                        duration_ms=duration_ms,
                        cached=False,
                    )

            except BaseException as e:
                duration_ms = (time.time() - node_start) * 1000
                if inspection_session is not None and isinstance(e, Exception) and not isinstance(e, _NodeExecutionError):
                    inspection_session.abort_node(
                        span_id=node_span_id,
                        ended_at_ms=time.perf_counter() * 1000,
                        duration_ms=duration_ms,
                    )
                if active and not node_error_event_attempted and not isinstance(e, _NodeExecutionError):
                    dispatcher.emit(
                        build_node_error_event(
                            run_id,
                            node_span_id,
                            run_span_id,
                            node,
                            graph,
                            workflow_id=ctx_base.workflow_id,
                            item_index=ctx_base.item_index,
                            superstep=superstep_idx,
                        )
                    )
                # Re-raise PauseExecution unwrapped (needed for InterruptNode)
                if isinstance(e, PauseExecution):
                    e.span_id = node_span_id
                    raise
                if isinstance(e, _NodeExecutionError):
                    raise
                # Wrap only Exception subclasses
                if isinstance(e, Exception):
                    if isinstance(e, ExecutionError):
                        error = ExecutionError(
                            e,
                            e.partial_state,
                            attempted_node_names=e.attempted_node_names,
                            node_errors=e.node_errors,
                        )
                        raise error from e
                    error = ExecutionError(
                        e,
                        new_state,
                        attempted_node_names=tuple(attempted_node_names),
                        node_errors={node.name: e},
                    )
                    raise error from e
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
