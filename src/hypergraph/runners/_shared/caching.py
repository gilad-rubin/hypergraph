"""Shared caching logic for runners.

Cache check/store logic is identical between sync and async supersteps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.nodes.gate import IfElseNode, RouteNode

if TYPE_CHECKING:
    from hypergraph.cache import CacheBackend
    from hypergraph.nodes.base import HyperNode
    from hypergraph.runners._shared.types import GraphState

# Internal key used to store routing decisions alongside cached gate outputs.
# Never exposed in RunResult.values.
_ROUTING_DECISION_KEY = "__routing_decision__"


def check_cache(
    node: "HyperNode",
    inputs: dict[str, Any],
    cache: "CacheBackend",
) -> tuple[str, dict[str, Any] | None]:
    """Check cache for a node's result.

    Returns:
        (cache_key, outputs) — outputs is None on miss.
    """
    if not getattr(node, "cache", False):
        return "", None

    from hypergraph.cache import compute_cache_key

    cache_key = compute_cache_key(node.definition_hash, inputs)
    if not cache_key:
        return "", None

    hit, cached_value = cache.get(cache_key)
    if not hit:
        return cache_key, None

    return cache_key, dict(cached_value)


def restore_routing_decision(
    node: "HyperNode",
    outputs: dict[str, Any],
    state: "GraphState",
) -> None:
    """Restore a cached routing decision for gate nodes.

    Pops the internal routing key from outputs (so it doesn't leak)
    and writes it to state.routing_decisions.
    """
    if not isinstance(node, (RouteNode, IfElseNode)):
        return
    routing_decision = outputs.pop(_ROUTING_DECISION_KEY, None)
    if routing_decision is not None:
        state.routing_decisions[node.name] = routing_decision


def store_in_cache(
    node: "HyperNode",
    outputs: dict[str, Any],
    state: "GraphState",
    cache: "CacheBackend",
    cache_key: str,
) -> None:
    """Store a node's outputs in cache, including routing decisions for gates."""
    to_cache = dict(outputs)
    if isinstance(node, (RouteNode, IfElseNode)):
        decision = state.routing_decisions.get(node.name)
        if decision is not None:
            to_cache[_ROUTING_DECISION_KEY] = decision
    cache.set(cache_key, to_cache)
