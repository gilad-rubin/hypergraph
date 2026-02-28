"""Superstep execution for async runner."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
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
from hypergraph.runners._shared.helpers import collect_inputs_for_node
from hypergraph.runners._shared.types import GraphState, NodeExecution

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
    execute_node: AsyncNodeExecutor,
    max_concurrency: int | None = None,
    *,
    cache: CacheBackend | None = None,
    dispatcher: EventDispatcher | None = None,
    run_id: str = "",
    run_span_id: str = "",
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
        execute_node: Async function to execute a single node
        max_concurrency: Unused (kept for API compatibility)
        dispatcher: Optional event dispatcher for emitting node events
        run_id: Run ID for event correlation
        run_span_id: Span ID of the parent run

    Returns:
        New state with updated values and versions
    """
    new_state = state.copy()
    active = dispatcher is not None and dispatcher.active

    # Execute interrupt nodes alone (not concurrently with other nodes).
    # PauseExecution extends BaseException, so if raised inside asyncio.gather
    # it cancels all sibling tasks. By isolating interrupt nodes, other ready
    # nodes are deferred to the next superstep where they'll still be ready.
    interrupts = [n for n in ready_nodes if n.is_interrupt]
    if interrupts:
        ready_nodes = [interrupts[0]]

    async def execute_one(
        node: HyperNode,
    ) -> tuple[HyperNode, dict[str, Any], dict[str, int], dict[str, int], float, bool]:
        """Execute a single node with event emission."""
        inputs = collect_inputs_for_node(node, graph, state, provided_values)
        input_versions = {param: state.get_version(param) for param in node.inputs}
        wait_for_versions = {name: state.get_version(name) for name in node.wait_for}

        # Check cache before execution
        cache_key, cached_outputs = ("", None)
        if cache is not None:
            cache_key, cached_outputs = check_cache(node, inputs, cache)

        if cached_outputs is not None:
            outputs = cached_outputs
            restore_routing_decision(node, outputs, new_state)
            # Emit NodeStartEvent -> CacheHitEvent -> RouteDecision? -> NodeEndEvent(cached=True)
            node_span_id, start_evt = build_node_start_event(run_id, run_span_id, node, graph)
            if active:
                await dispatcher.emit_async(start_evt)
                await dispatcher.emit_async(build_cache_hit_event(run_id, node_span_id, run_span_id, node, graph, cache_key))
                route_evt = build_route_decision_event(run_id, run_span_id, node, graph, new_state)
                if route_evt is not None:
                    await dispatcher.emit_async(route_evt)
                await dispatcher.emit_async(build_node_end_event(run_id, node_span_id, run_span_id, node, graph, duration_ms=0.0, cached=True))
            return node, outputs, input_versions, wait_for_versions, 0.0, True

        # Emit NodeStartEvent
        node_span_id, start_evt = build_node_start_event(run_id, run_span_id, node, graph)
        if active:
            await dispatcher.emit_async(start_evt)

        # Set node span_id on executor for nested graph propagation
        if hasattr(execute_node, "current_span_id"):
            execute_node.current_span_id[0] = node_span_id  # type: ignore[attr-defined]

        node_start = time.time()
        try:
            # Pass new_state so routing decisions are stored in the updated state
            outputs = await execute_node(node, new_state, inputs)

            duration_ms = (time.time() - node_start) * 1000

            # Store result in cache
            if cache is not None and cache_key:
                store_in_cache(node, outputs, new_state, cache, cache_key)

            if active:
                route_evt = build_route_decision_event(run_id, run_span_id, node, graph, new_state)
                if route_evt is not None:
                    await dispatcher.emit_async(route_evt)
                await dispatcher.emit_async(build_node_end_event(run_id, node_span_id, run_span_id, node, graph, duration_ms))

            return node, outputs, input_versions, wait_for_versions, duration_ms, False
        except Exception:
            if active:
                await dispatcher.emit_async(build_node_error_event(run_id, node_span_id, run_span_id, node, graph))
            raise

    # Execute all ready nodes concurrently
    # Concurrency is controlled at the FunctionNode level via the global semaphore
    tasks = [execute_one(node) for node in ready_nodes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Separate successes from failures, applying successful outputs first
    first_error: BaseException | None = None
    for result in results:
        if isinstance(result, BaseException):
            if first_error is None:
                first_error = result
            continue
        node, outputs, input_versions, wait_for_versions, duration_ms, cached = result
        for name, value in outputs.items():
            new_state.update_value(name, value)
        new_state.node_executions[node.name] = NodeExecution(
            node_name=node.name,
            input_versions=input_versions,
            outputs=outputs,
            wait_for_versions=wait_for_versions,
            duration_ms=duration_ms,
            cached=cached,
        )

    if first_error is not None:
        # PauseExecution (BaseException) must propagate unwrapped for the
        # runner's except PauseExecution handler to work
        if isinstance(first_error, ExecutionError):
            raise first_error
        if not isinstance(first_error, Exception):
            raise first_error
        raise ExecutionError(first_error, new_state) from first_error

    return new_state
