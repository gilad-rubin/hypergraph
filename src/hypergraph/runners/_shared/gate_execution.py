"""Shared gate execution logic for sync and async runners.

Gate routing functions are always synchronous (validated at decoration time),
so the core logic is identical between sync and async executors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import map_inputs_to_func_params
from hypergraph.runners._shared.routing_validation import validate_routing_decision

if TYPE_CHECKING:
    from hypergraph.nodes.gate import IfElseNode, RouteNode
    from hypergraph.runners._shared.types import GraphState


def execute_ifelse(
    node: "IfElseNode",
    state: "GraphState",
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Execute an IfElseNode's routing logic.

    Calls the routing function, validates the result is bool,
    normalizes to a target name, and stores the routing decision.

    Returns:
        Empty dict (gates produce no data outputs).
    """
    func_inputs = map_inputs_to_func_params(node, inputs)
    result = node.func(**func_inputs)

    if not isinstance(result, bool):
        raise TypeError(
            f"IfElseNode '{node.name}' must return bool, got {type(result).__name__}.\n\n"
            f"Returned value: {result!r}\n\n"
            f"How to fix: Ensure your function returns True or False, not truthy/falsy values"
        )

    decision = node.when_true if result else node.when_false
    state.routing_decisions[node.name] = decision
    return {}


def execute_route(
    node: "RouteNode",
    state: "GraphState",
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Execute a RouteNode's routing logic.

    Calls the routing function, applies fallback, validates the decision,
    and stores the routing decision.

    Returns:
        Empty dict (gates produce no data outputs).
    """
    func_inputs = map_inputs_to_func_params(node, inputs)
    decision = node.func(**func_inputs)

    if decision is None and node.fallback is not None:
        decision = node.fallback

    validate_routing_decision(node, decision)
    state.routing_decisions[node.name] = decision
    return {}
