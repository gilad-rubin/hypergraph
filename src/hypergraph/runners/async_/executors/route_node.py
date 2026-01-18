"""Async executor for RouteNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import map_inputs_to_func_params
from hypergraph.runners._shared.routing_validation import validate_routing_decision

if TYPE_CHECKING:
    from hypergraph.nodes.gate import RouteNode
    from hypergraph.runners._shared.types import GraphState


class AsyncRouteNodeExecutor:
    """Executes RouteNode in async context.

    RouteNodes make routing decisions but don't produce data outputs.
    Even though the executor is async, the routing function must be sync.
    The executor calls the routing function and stores the decision
    in the graph state for downstream control flow.
    """

    async def __call__(
        self,
        node: "RouteNode",
        state: "GraphState",
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a RouteNode asynchronously.

        Note: The routing function is always sync (validated at decoration time).
        This async wrapper exists for consistency with other async executors.

        Args:
            node: The RouteNode to execute
            state: Current graph execution state
            inputs: Input values for the node

        Returns:
            Empty dict (gates produce no data outputs).
            The routing decision is stored in state.routing_decisions.
        """
        # Map renamed inputs back to original function parameter names
        func_inputs = map_inputs_to_func_params(node, inputs)

        # Call the routing function (always sync)
        decision = node.func(**func_inputs)

        # Apply fallback if decision is None
        if decision is None and node.fallback is not None:
            decision = node.fallback

        # Validate return type
        validate_routing_decision(node, decision)

        # Store routing decision in state
        state.routing_decisions[node.name] = decision

        # Gates produce no data outputs
        return {}
