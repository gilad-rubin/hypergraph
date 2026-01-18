"""Async executor for RouteNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import map_inputs_to_func_params

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
        from hypergraph.nodes.gate import END

        # Map renamed inputs back to original function parameter names
        func_inputs = map_inputs_to_func_params(node, inputs)

        # Call the routing function (always sync)
        decision = node.func(**func_inputs)

        # Apply fallback if decision is None
        if decision is None and node.fallback is not None:
            decision = node.fallback

        # Validate return type
        _validate_routing_decision(node, decision)

        # Store routing decision in state
        state.routing_decisions[node.name] = decision

        # Gates produce no data outputs
        return {}


def _validate_routing_decision(node: "RouteNode", decision: Any) -> None:
    """Validate the routing decision matches expected type and values.

    Args:
        node: The RouteNode that made the decision
        decision: The decision value to validate

    Raises:
        TypeError: If decision type doesn't match multi_target setting
        ValueError: If decision is not in the valid targets list
    """
    from hypergraph.nodes.gate import END
    import warnings

    # Check for string "END" instead of END sentinel
    if decision == "END" and decision is not END:
        warnings.warn(
            f"Gate '{node.name}' returned string 'END' instead of END sentinel.\n"
            f"Use 'from hypergraph import END' and return END directly.",
            UserWarning,
            stacklevel=4,
        )
        return  # Let it proceed, might be intentional (target named "END")

    if node.multi_target:
        # multi_target=True expects list or None
        if decision is not None and not isinstance(decision, list):
            raise TypeError(
                f"Gate '{node.name}' has multi_target=True but returned {type(decision).__name__}.\n"
                f"Expected: list of target names (or empty list)\n"
                f"Got: {decision!r}"
            )
        if decision is not None:
            for target in decision:
                _validate_single_target(node, target)
    else:
        # multi_target=False expects single value or None
        if isinstance(decision, list):
            raise TypeError(
                f"Gate '{node.name}' has multi_target=False but returned a list.\n"
                f"Expected: single target name (str), None, or END\n"
                f"Got: {decision!r}\n\n"
                f"Hint: Use multi_target=True if you want to route to multiple targets"
            )
        if decision is not None:
            _validate_single_target(node, decision)


def _validate_single_target(node: "RouteNode", target: Any) -> None:
    """Validate a single target is in the valid targets list."""
    from hypergraph.nodes.gate import END

    valid_targets = set(node.targets)
    if target not in valid_targets:
        target_str = "END" if target is END else repr(target)
        valid_str = sorted(str(t) if t is END else repr(t) for t in node.targets)
        raise ValueError(
            f"Gate '{node.name}' returned invalid target {target_str}\n\n"
            f"  -> Valid targets: {valid_str}\n\n"
            f"How to fix: Return one of the targets listed in @route(targets=[...])"
        )
