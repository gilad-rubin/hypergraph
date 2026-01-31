"""Superstep execution for async runner."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.gate import IfElseNode, RouteNode
from hypergraph.runners._shared.helpers import collect_inputs_for_node
from hypergraph.runners._shared.types import GraphState, NodeExecution

if TYPE_CHECKING:
    from hypergraph.cache import CacheBackend
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.graph import Graph
    from hypergraph.runners._shared.protocols import AsyncNodeExecutor

# Context variable for concurrency limiting across nested graphs
_concurrency_limiter: ContextVar[asyncio.Semaphore | None] = ContextVar(
    "_concurrency_limiter", default=None
)


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
    graph: "Graph",
    state: GraphState,
    ready_nodes: list[HyperNode],
    provided_values: dict[str, Any],
    execute_node: "AsyncNodeExecutor",
    max_concurrency: int | None = None,
    *,
    cache: "CacheBackend | None" = None,
    dispatcher: "EventDispatcher | None" = None,
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

    # Execute InterruptNodes alone (not concurrently with other nodes).
    # PauseExecution extends BaseException, so if raised inside asyncio.gather
    # it cancels all sibling tasks. By isolating InterruptNodes, other ready
    # nodes are deferred to the next superstep where they'll still be ready.
    from hypergraph.nodes.interrupt import InterruptNode

    interrupt_nodes = [n for n in ready_nodes if isinstance(n, InterruptNode)]
    if interrupt_nodes:
        ready_nodes = [interrupt_nodes[0]]

    async def execute_one(
        node: HyperNode,
    ) -> tuple[HyperNode, dict[str, Any], dict[str, int]]:
        """Execute a single node with event emission."""
        inputs = collect_inputs_for_node(node, graph, state, provided_values)
        input_versions = {param: state.get_version(param) for param in node.inputs}

        # Check cache before execution
        cache_key = ""
        if cache is not None and getattr(node, "cache", False):
            from hypergraph.cache import compute_cache_key

            cache_key = compute_cache_key(node.definition_hash, inputs)
            if cache_key:
                cached_hit, cached_value = cache.get(cache_key)
                if cached_hit:
                    outputs = cached_value
                    # Restore routing decision for gate nodes
                    if isinstance(node, (RouteNode, IfElseNode)):
                        routing_decision = outputs.pop("__routing_decision__", None)
                        if routing_decision is not None:
                            new_state.routing_decisions[node.name] = routing_decision
                    # Emit NodeStartEvent → CacheHitEvent → RouteDecision? → NodeEndEvent(cached=True)
                    node_span_id = await _emit_node_start(
                        dispatcher, run_id, run_span_id, node, graph,
                    )
                    await _emit_cache_hit(
                        dispatcher, run_id, node_span_id, run_span_id,
                        node, graph, cache_key,
                    )
                    await _emit_route_decision(
                        dispatcher, run_id, run_span_id, node, graph, new_state,
                    )
                    await _emit_node_end(
                        dispatcher, run_id, node_span_id, run_span_id,
                        node, graph, duration_ms=0.0, cached=True,
                    )
                    return node, outputs, input_versions

        # Emit NodeStartEvent
        node_span_id = await _emit_node_start(dispatcher, run_id, run_span_id, node, graph)

        # Set node span_id on executor for nested graph propagation
        if hasattr(execute_node, "current_span_id"):
            execute_node.current_span_id[0] = node_span_id  # type: ignore[attr-defined]

        node_start = time.time()
        try:
            # Pass new_state so routing decisions are stored in the updated state
            outputs = await execute_node(node, new_state, inputs)

            duration_ms = (time.time() - node_start) * 1000

            # Store result in cache (include routing decision for gates)
            if cache is not None and cache_key:
                to_cache = dict(outputs)
                if isinstance(node, (RouteNode, IfElseNode)):
                    decision = new_state.routing_decisions.get(node.name)
                    if decision is not None:
                        to_cache["__routing_decision__"] = decision
                cache.set(cache_key, to_cache)

            # Emit RouteDecisionEvent if this was a routing node
            await _emit_route_decision(
                dispatcher, run_id, run_span_id, node, graph, new_state,
            )

            # Emit NodeEndEvent
            await _emit_node_end(
                dispatcher, run_id, node_span_id, run_span_id, node, graph, duration_ms,
            )

            return node, outputs, input_versions
        except Exception:
            await _emit_node_error(
                dispatcher, run_id, node_span_id, run_span_id, node, graph,
            )
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
        node, outputs, input_versions = result
        for name, value in outputs.items():
            new_state.update_value(name, value)
        new_state.node_executions[node.name] = NodeExecution(
            node_name=node.name,
            input_versions=input_versions,
            outputs=outputs,
        )

    if first_error is not None:
        first_error._partial_state = new_state  # type: ignore[attr-defined]
        raise first_error

    return new_state


# ------------------------------------------------------------------
# Event emission helpers
# ------------------------------------------------------------------


async def _emit_node_start(
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

    await dispatcher.emit_async(NodeStartEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
    ))
    return span_id


async def _emit_node_end(
    dispatcher: "EventDispatcher | None",
    run_id: str,
    node_span_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: "Graph",
    duration_ms: float,
    cached: bool = False,
) -> None:
    """Emit NodeEndEvent."""
    if dispatcher is None or not dispatcher.active:
        return

    from hypergraph.events.types import NodeEndEvent

    await dispatcher.emit_async(NodeEndEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
        duration_ms=duration_ms,
        cached=cached,
    ))


async def _emit_cache_hit(
    dispatcher: "EventDispatcher | None",
    run_id: str,
    node_span_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: "Graph",
    cache_key: str,
) -> None:
    """Emit CacheHitEvent."""
    if dispatcher is None or not dispatcher.active:
        return

    from hypergraph.events.types import CacheHitEvent

    await dispatcher.emit_async(CacheHitEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
        cache_key=cache_key,
    ))


async def _emit_node_error(
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
    await dispatcher.emit_async(NodeErrorEvent(
        run_id=run_id,
        span_id=node_span_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
        error=str(exc_val) if exc_val else "",
        error_type=f"{exc_type.__module__}.{exc_type.__qualname__}" if exc_type else "",
    ))


async def _emit_route_decision(
    dispatcher: "EventDispatcher | None",
    run_id: str,
    run_span_id: str,
    node: HyperNode,
    graph: "Graph",
    state: GraphState,
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
    await dispatcher.emit_async(RouteDecisionEvent(
        run_id=run_id,
        parent_span_id=run_span_id,
        node_name=node.name,
        graph_name=graph.name,
        decision=decision,
    ))
