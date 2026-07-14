"""Superstep execution for async runner."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
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
    from hypergraph.runners._shared.protocols import AsyncNodeExecutor

# Context variable for concurrency limiting across nested graphs
_concurrency_limiter: ContextVar[asyncio.Semaphore | None] = ContextVar("_concurrency_limiter", default=None)


def get_concurrency_limiter() -> asyncio.Semaphore | None:
    """Get the current concurrency limiter."""
    return _concurrency_limiter.get()


def set_concurrency_limiter(semaphore: asyncio.Semaphore | None) -> Any:
    """Set the concurrency limiter and return a token for reset."""
    return _concurrency_limiter.set(semaphore)


def reset_concurrency_limiter(token: Any) -> None:
    """Reset the concurrency limiter using a token."""
    _concurrency_limiter.reset(token)


async def run_superstep_async(
    graph: Graph,
    state: GraphState,
    ready_nodes: list[HyperNode],
    provided_values: dict[str, Any],
    executors: dict[type[HyperNode], AsyncNodeExecutor[Any]],
    ctx_base: ExecutionContext,
    max_concurrency: int | None = None,
    *,
    cache: CacheBackend | None = None,
    dispatcher: EventDispatcher | None = None,
    run_id: str = "",
    run_span_id: str = "",
    superstep_idx: int | None = None,
) -> GraphState:
    """Execute one superstep with concurrent node execution.

    Note: Concurrency limiting is handled at the FunctionNode executor level,
    not here. This allows nested GraphNodes to share the same global semaphore
    without causing deadlock.

    Args:
        graph: The graph being executed
        state: Current state (will be copied, not mutated)
        ready_nodes: Nodes to execute in this superstep
        provided_values: Values provided to runner.run()
        executors: Runner executor registry
        ctx_base: Per-run execution context
        max_concurrency: Unused (kept for API compatibility)
        dispatcher: Optional event dispatcher for emitting node events
        run_id: Run ID for event correlation
        run_span_id: Span ID of the parent run

    Returns:
        New state with updated values and versions
    """
    new_state = state.copy()
    active = dispatcher is not None and dispatcher.active

    # Interrupt isolation happens in the runner (plan_interrupt_batch in
    # _shared/scheduling.py) BEFORE checkpoint metadata captures the batch —
    # ready_nodes arrives here already planned.

    async def execute_one(
        node: HyperNode,
    ) -> tuple[HyperNode, dict[str, Any], dict[str, int], dict[str, int], float, bool, str]:
        """Execute a single node with event emission."""
        inputs = collect_inputs_for_node(node, graph, state, provided_values)
        # Record input versions under the same parent-facing key the staleness
        # check reads.
        input_versions = {(addr := address_for_node_input(node, param)): state.versions.get(addr, 0) for param in node.inputs}
        wait_for_versions = {name: state.get_version(name) for name in node.wait_for}

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
            # Emit NodeStartEvent -> CacheHitEvent -> RouteDecision? -> NodeEndEvent(cached=True)
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
            if active:
                await dispatcher.emit_async(start_evt)
                await dispatcher.emit_async(
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
                    await dispatcher.emit_async(route_evt)
                await dispatcher.emit_async(
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
            return node, outputs, input_versions, wait_for_versions, 0.0, True, node_span_id

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
            await dispatcher.emit_async(start_evt)

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
            # can attribute itself to this exact span even under concurrency.
            span_token = set_current_node_span(NodeSpanRef(run_id=run_id, span_id=node_span_id, node_name=node.name, graph_name=graph.name))
            evidence_inputs = dict(inputs)
            parent_invocation_token = _get_failure_evidence_invocation()
            invocation_token = object() if isinstance(node, GraphNode) else None
            try:
                try:
                    # Pass new_state so routing decisions are stored in the
                    # updated state. This exact executor boundary is the only
                    # place that creates FailureEvidence.
                    with _bind_failure_evidence_invocation(invocation_token):
                        outputs = await executor(node, new_state, inputs, ctx)
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
                            await dispatcher.emit_async(
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
                            node_failures = tuple(
                                replace(
                                    failure,
                                    node_name=f"{node.name}/{failure.node_name}",
                                    item_index=(ctx_base.item_index if ctx_base.item_index is not None else failure.item_index),
                                )
                                for failure in inner_failures
                            )
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
                            record_failure = not (isinstance(node, GraphNode) and node.runner_override is None)
                            inspection_session.fail_node(
                                span_id=node_span_id,
                                failure=inspection_failure,
                                ended_at_ms=time.perf_counter() * 1000,
                                duration_ms=duration_ms,
                                record_failure=record_failure,
                            )
                        elif inspection_session is not None:
                            inspection_session.abort_node(
                                span_id=node_span_id,
                                error=executor_error,
                                ended_at_ms=time.perf_counter() * 1000,
                                duration_ms=duration_ms,
                            )
                        executor_failure = _NodeExecutionError(
                            executor_error,
                            new_state,
                            attempted_node_names=(node.name,),
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
                    await dispatcher.emit_async(route_evt)
                await dispatcher.emit_async(
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

            return node, outputs, input_versions, wait_for_versions, duration_ms, False, node_span_id
        except PauseExecution as exc:
            exc.span_id = node_span_id
            raise
        except Exception as exc:
            if inspection_session is not None and not isinstance(exc, _NodeExecutionError):
                inspection_session.abort_node(
                    span_id=node_span_id,
                    error=exc,
                    ended_at_ms=time.perf_counter() * 1000,
                    duration_ms=(time.time() - node_start) * 1000,
                )
            if active and not node_error_event_attempted and not isinstance(exc, _NodeExecutionError):
                await dispatcher.emit_async(
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
            raise

    # Execute all ready nodes concurrently
    # Concurrency is controlled at the FunctionNode level via the global semaphore
    tasks = [execute_one(node) for node in ready_nodes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Separate successes from failures, applying successful outputs first
    first_error: BaseException | None = None
    control_flow_error: BaseException | None = None
    node_errors: dict[str, BaseException] = {}
    node_failures: list[FailureEvidence] = []
    settled_inspection = current_inspection()
    inspection_session = settled_inspection[0] if settled_inspection is not None else None
    for result_index, (node, result) in enumerate(zip(ready_nodes, results, strict=True)):
        if isinstance(result, BaseException):
            if first_error is None:
                first_error = (result.__cause__ or result) if isinstance(result, _NodeExecutionError) else result
            if control_flow_error is None and not isinstance(result, Exception):
                control_flow_error = result
            if isinstance(result, _NodeExecutionError):
                node_errors[node.name] = result.__cause__ or result
                node_failures.extend(result.node_failures)
            else:
                node_errors[node.name] = result
            continue
        node, outputs, input_versions, wait_for_versions, duration_ms, cached, node_span_id = result
        try:
            apply_node_result(
                graph,
                new_state,
                node,
                outputs,
                input_versions,
                wait_for_versions,
                duration_ms=duration_ms,
                cached=cached,
            )
        except Exception as error:
            if inspection_session is not None:
                for pending in results[result_index:]:
                    if isinstance(pending, BaseException):
                        continue
                    _, _, _, _, pending_duration_ms, _, pending_span_id = pending
                    inspection_session.abort_node(
                        span_id=pending_span_id,
                        error=error,
                        ended_at_ms=time.perf_counter() * 1000,
                        duration_ms=pending_duration_ms,
                    )
            raise

        if inspection_session is not None:
            inspection_session.finish_node(
                span_id=node_span_id,
                outputs=outputs,
                ended_at_ms=time.perf_counter() * 1000,
                duration_ms=duration_ms,
                cached=cached,
            )

    if control_flow_error is not None:
        # Pause/cancellation/system-exit control flow must not be hidden by an
        # ordinary Exception that happened to appear earlier in graph order.
        if isinstance(control_flow_error, PauseExecution):
            control_flow_error.partial_state = new_state
        raise control_flow_error

    if first_error is not None:
        attempted = tuple(node.name for node in ready_nodes)
        if node_failures:
            error = _NodeExecutionError(
                first_error,
                new_state,
                attempted_node_names=attempted,
                node_errors=node_errors,
                node_failures=tuple(node_failures),
                invocation_token=_get_failure_evidence_invocation(),
            )
        else:
            error = ExecutionError(
                first_error,
                new_state,
                attempted_node_names=attempted,
                node_errors=node_errors,
            )
        raise error from first_error

    return new_state
