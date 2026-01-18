"""Sync executor for IfElseNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import map_inputs_to_func_params

if TYPE_CHECKING:
    from hypergraph.nodes.gate import IfElseNode
    from hypergraph.runners._shared.types import GraphState


class SyncIfElseNodeExecutor:
    """Executes IfElseNode synchronously.

    IfElseNodes make binary routing decisions based on a boolean return value.
    The executor calls the routing function and stores the decision
    in the graph state for downstream control flow.
    """

    def __call__(
        self,
        node: "IfElseNode",
        state: "GraphState",
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute an IfElseNode synchronously.

        Args:
            node: The IfElseNode to execute
            state: Current graph execution state
            inputs: Input values for the node

        Returns:
            Empty dict (gates produce no data outputs).
            The routing decision is stored in state.routing_decisions.

        Raises:
            TypeError: If the function returns non-bool value
        """
        # Map renamed inputs back to original function parameter names
        func_inputs = map_inputs_to_func_params(node, inputs)

        # Call the routing function
        result = node.func(**func_inputs)

        # Validate result is strictly bool
        if not isinstance(result, bool):
            raise TypeError(
                f"IfElseNode '{node.name}' must return bool, got {type(result).__name__}.\n\n"
                f"Returned value: {result!r}\n\n"
                f"How to fix: Ensure your function returns True or False, not truthy/falsy values"
            )

        # Normalize bool → target name for consistency with RouteNode
        decision = node.when_true if result else node.when_false

        # Store routing decision in state
        state.routing_decisions[node.name] = decision

        # Gates produce no data outputs
        return {}
