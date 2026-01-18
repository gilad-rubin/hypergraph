"""Validation logic for routing decisions.

Shared between sync and async RouteNode executors.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hypergraph.nodes.gate import RouteNode


def validate_routing_decision(node: "RouteNode", decision: Any) -> None:
    """Validate the routing decision matches expected type and values.

    Args:
        node: The RouteNode that made the decision
        decision: The decision value to validate

    Raises:
        TypeError: If decision type doesn't match multi_target setting
        ValueError: If decision is not in the valid targets list
    """
    from hypergraph.nodes.gate import END

    # Check for string "END" instead of END sentinel
    if decision == "END" and decision is not END:
        warnings.warn(
            f"Gate '{node.name}' returned string 'END' instead of END sentinel.\n"
            f"Use 'from hypergraph import END' and return END directly.",
            UserWarning,
            stacklevel=5,
        )
        return  # Let it proceed, might be intentional (target named "END")

    if node.multi_target:
        _validate_multi_target_decision(node, decision)
    else:
        _validate_single_target_decision(node, decision)


def _validate_multi_target_decision(node: "RouteNode", decision: Any) -> None:
    """Validate a multi_target routing decision."""
    if decision is None:
        return

    if not isinstance(decision, list):
        raise TypeError(
            f"Gate '{node.name}' has multi_target=True but returned {type(decision).__name__}.\n"
            f"Expected: list of target names (or empty list)\n"
            f"Got: {decision!r}"
        )

    for target in decision:
        _validate_single_target(node, target)


def _validate_single_target_decision(node: "RouteNode", decision: Any) -> None:
    """Validate a single-target routing decision."""
    if decision is None:
        return

    if isinstance(decision, list):
        raise TypeError(
            f"Gate '{node.name}' has multi_target=False but returned a list.\n"
            f"Expected: single target name (str), None, or END\n"
            f"Got: {decision!r}\n\n"
            f"Hint: Use multi_target=True if you want to route to multiple targets"
        )

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
