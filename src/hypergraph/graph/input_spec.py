"""Input specification calculation for graphs.

This module contains the InputSpec dataclass and logic for computing
which parameters are required, optional, or seed values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
    from hypergraph.nodes.base import HyperNode


@dataclass(frozen=True)
class InputSpec:
    """Specification of graph input parameters.

    Categories follow the "edge cancels default" rule:
    - required: No edge, no default, not bound -> must always provide
    - optional: No edge, has default OR bound -> can omit (fallback exists)
    - seeds: Has cycle edge -> must provide initial value for first iteration
    """

    required: tuple[str, ...]
    optional: tuple[str, ...]
    seeds: tuple[str, ...]
    bound: dict[str, Any]

    @property
    def all(self) -> tuple[str, ...]:
        """All input names (required + optional + seeds)."""
        return self.required + self.optional + self.seeds


def compute_input_spec(
    nodes: dict[str, "HyperNode"],
    nx_graph: nx.DiGraph,
    bound: dict[str, Any],
) -> InputSpec:
    """Compute input specification for a graph.

    Args:
        nodes: Map of node name -> HyperNode
        nx_graph: The NetworkX directed graph
        bound: Currently bound values

    Returns:
        InputSpec with categorized parameters
    """
    edge_produced = _get_edge_produced_values(nx_graph)
    cycle_params = _get_cycle_params(nodes, nx_graph, edge_produced)

    required, optional, seeds = [], [], []

    for param in _unique_params(nodes):
        category = _categorize_param(param, edge_produced, cycle_params, bound, nodes)
        if category == "required":
            required.append(param)
        elif category == "optional":
            optional.append(param)
        elif category == "seed":
            seeds.append(param)

    return InputSpec(
        required=tuple(required),
        optional=tuple(optional),
        seeds=tuple(seeds),
        bound=dict(bound),
    )


def _unique_params(nodes: dict[str, "HyperNode"]) -> Iterator[str]:
    """Yield each unique parameter name across all nodes."""
    seen: set[str] = set()
    for node in nodes.values():
        for param in node.inputs:
            if param not in seen:
                seen.add(param)
                yield param


def _get_edge_produced_values(nx_graph: nx.DiGraph) -> set[str]:
    """Get all value names that are produced by data edges.

    Control edges (from gates) don't have value_name - only data edges do.
    """
    return {
        data["value_name"]
        for _, _, data in nx_graph.edges(data=True)
        if "value_name" in data
    }


def _categorize_param(
    param: str,
    edge_produced: set[str],
    cycle_params: set[str],
    bound: dict[str, Any],
    nodes: dict[str, "HyperNode"],
) -> str | None:
    """Categorize a parameter: 'required', 'optional', 'seed', or None."""
    has_edge = param in edge_produced

    if has_edge:
        return "seed" if param in cycle_params else None

    if param in bound or _any_node_has_default(param, nodes):
        return "optional"

    return "required"


def _any_node_has_default(param: str, nodes: dict[str, "HyperNode"]) -> bool:
    """Check if any node consuming this param has a default value."""
    for node in nodes.values():
        if param in node.inputs and node.has_default_for(param):
            return True
    return False


def _get_cycle_params(
    nodes: dict[str, "HyperNode"],
    nx_graph: nx.DiGraph,
    edge_produced: set[str],
) -> set[str]:
    """Get parameter names that are part of cycles."""
    cycles = list(nx.simple_cycles(nx_graph))
    if not cycles:
        return set()

    return {
        param
        for cycle in cycles
        for param in _params_flowing_in_cycle(cycle, nodes, edge_produced)
    }


def _params_flowing_in_cycle(
    cycle: list[str],
    nodes: dict[str, "HyperNode"],
    edge_produced: set[str],
) -> Iterator[str]:
    """Yield params that flow within a cycle."""
    cycle_nodes = set(cycle)

    for node_name in cycle:
        for param in nodes[node_name].inputs:
            if param not in edge_produced:
                continue
            if any(p in cycle_nodes for p in _sources_of(param, nodes)):
                yield param


def _sources_of(output: str, nodes: dict[str, "HyperNode"]) -> list[str]:
    """Get all nodes that produce the given output."""
    return [node.name for node in nodes.values() if output in node.outputs]
