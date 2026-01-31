"""Superstep execution for async runner."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.helpers import collect_inputs_for_node
from hypergraph.runners._shared.types import GraphState, NodeExecution

if TYPE_CHECKING:
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

    Returns:
        New state with updated values and versions
    """
    new_state = state.copy()

    async def execute_one(
        node: HyperNode,
    ) -> tuple[HyperNode, dict[str, Any], dict[str, int]]:
        """Execute a single node."""
        inputs = collect_inputs_for_node(node, graph, state, provided_values)
        input_versions = {param: state.get_version(param) for param in node.inputs}

        # Pass new_state so routing decisions are stored in the updated state
        outputs = await execute_node(node, new_state, inputs)

        return node, outputs, input_versions

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
