"""Executor protocols for node execution strategies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, TypeVar

if TYPE_CHECKING:
    from hypergraph.nodes.base import HyperNode
    from hypergraph.runners._shared.types import GraphState

N = TypeVar("N", bound="HyperNode", contravariant=True)


class NodeExecutor(Protocol[N]):
    """Protocol for synchronous node execution.

    Executors are responsible for executing a specific node type.
    Each runner owns a registry of executors for the node types it supports.

    The executor is called with:
    - node: The node instance to execute
    - state: Current graph execution state
    - inputs: Dict of input values for this node

    Returns:
        Dict mapping output names to their values
    """

    def __call__(
        self,
        node: N,
        state: "GraphState",
        inputs: dict[str, Any],
    ) -> dict[str, Any]: ...


class AsyncNodeExecutor(Protocol[N]):
    """Protocol for asynchronous node execution.

    Async version of NodeExecutor for runners that support async execution.

    The executor is called with:
    - node: The node instance to execute
    - state: Current graph execution state
    - inputs: Dict of input values for this node

    Returns:
        Dict mapping output names to their values
    """

    async def __call__(
        self,
        node: N,
        state: "GraphState",
        inputs: dict[str, Any],
    ) -> dict[str, Any]: ...
