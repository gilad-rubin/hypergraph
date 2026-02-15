"""Input specification calculation for graphs.

This module contains the InputSpec dataclass and logic for computing
which parameters are required, optional, or cycle entry points.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, TYPE_CHECKING

import networkx as nx

from hypergraph.graph._helpers import get_edge_produced_values, sources_of

if TYPE_CHECKING:
    from hypergraph.nodes.base import HyperNode


@dataclass(frozen=True)
class InputSpec:
    """Specification of graph input parameters.

    Categories follow the "edge cancels default" rule:
    - required: No edge, no default, not bound -> must always provide
    - optional: No edge, has default OR bound -> can omit (fallback exists)
    - entry_points: dict mapping cycle node name -> tuple of params needed
      to enter the cycle at that node. Pick ONE entry point per cycle.
    """

    required: tuple[str, ...]
    optional: tuple[str, ...]
    entry_points: dict[str, tuple[str, ...]]
    bound: dict[str, Any]

    @property
    def all(self) -> tuple[str, ...]:
        """All input names (required + optional + entry point params)."""
        seen = set(self.required + self.optional)
        entry_params: list[str] = []
        for params in self.entry_points.values():
            for p in params:
                if p not in seen:
                    seen.add(p)
                    entry_params.append(p)
        return self.required + self.optional + tuple(entry_params)


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
    edge_produced = get_edge_produced_values(nx_graph)
    entry_points = _compute_entry_points(nodes, nx_graph, edge_produced, bound)
    # Flat set of all entry point params (for categorization)
    all_entry_params = {p for params in entry_points.values() for p in params}

    required, optional = [], []

    for param in _unique_params(nodes):
        if param in all_entry_params:
            continue  # Handled by entry_points
        category = _categorize_param(param, edge_produced, bound, nodes)
        if category == "required":
            required.append(param)
        elif category == "optional":
            optional.append(param)

    # Merge bound values from nested GraphNodes
    all_bound = _collect_bound_values(nodes, bound)

    return InputSpec(
        required=tuple(required),
        optional=tuple(optional),
        entry_points=entry_points,
        bound=all_bound,
    )


def _unique_params(nodes: dict[str, "HyperNode"]) -> Iterator[str]:
    """Yield each unique parameter name across all nodes."""
    seen: set[str] = set()
    for node in nodes.values():
        for param in node.inputs:
            if param not in seen:
                seen.add(param)
                yield param


def _categorize_param(
    param: str,
    edge_produced: set[str],
    bound: dict[str, Any],
    nodes: dict[str, "HyperNode"],
) -> str | None:
    """Categorize a non-cycle parameter: 'required', 'optional', or None (edge-produced)."""
    if param in edge_produced:
        return None  # Produced by an edge, not a user input

    if param in bound or _any_node_has_default(param, nodes):
        return "optional"

    return "required"


def _is_interrupt_produced(param: str, nodes: dict[str, "HyperNode"]) -> bool:
    """Check if param is produced by an InterruptNode."""
    from hypergraph.nodes.interrupt import InterruptNode

    return any(
        isinstance(n, InterruptNode) and param in n.outputs
        for n in nodes.values()
    )


def _any_node_has_default(param: str, nodes: dict[str, "HyperNode"]) -> bool:
    """Check if any node consuming this param has a default value."""
    for node in nodes.values():
        if param in node.inputs and node.has_default_for(param):
            return True
    return False


def _compute_entry_points(
    nodes: dict[str, "HyperNode"],
    nx_graph: nx.DiGraph,
    edge_produced: set[str],
    bound: dict[str, Any],
) -> dict[str, tuple[str, ...]]:
    """Compute entry points for each cycle in the graph.

    For each SCC (strongly connected component) with >1 node or a self-loop,
    find non-gate nodes and compute which cycle-params they need as inputs.
    Each such node is an entry point — the user picks one per cycle.

    Returns:
        Dict mapping node name -> tuple of cycle params needed to enter there.
        Empty dict for DAGs (no cycles).
    """
    from hypergraph.nodes.gate import GateNode

    data_graph = _data_only_subgraph(nx_graph)
    cycle_params = _get_all_cycle_params(nodes, data_graph, edge_produced)

    if not cycle_params:
        return {}

    sccs = list(nx.strongly_connected_components(data_graph))
    entry_points: dict[str, tuple[str, ...]] = {}

    for scc in sccs:
        if not _is_cyclic_scc(scc, data_graph):
            continue

        for node_name in sorted(scc):  # sorted for deterministic order
            node = nodes[node_name]
            if isinstance(node, GateNode):
                continue  # Gates control cycles, not start them

            # Params this node needs that are cycle-produced
            # (minus bound, minus interrupt-produced, minus defaulted)
            needed = tuple(
                p for p in node.inputs
                if p in cycle_params
                and p not in bound
                and not _is_interrupt_produced(p, nodes)
                and not node.has_default_for(p)
            )
            if needed:  # Only include if node needs user-provided cycle params
                entry_points[node_name] = needed

    return entry_points


def _is_cyclic_scc(scc: set[str], graph: nx.DiGraph) -> bool:
    """Check if an SCC represents a cycle (>1 node, or self-loop)."""
    if len(scc) > 1:
        return True
    node = next(iter(scc))
    return graph.has_edge(node, node)


def _get_all_cycle_params(
    nodes: dict[str, "HyperNode"],
    data_graph: nx.DiGraph,
    edge_produced: set[str],
) -> set[str]:
    """Get all parameter names that flow within any cycle."""
    cycles = list(nx.simple_cycles(data_graph))
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
            if any(p in cycle_nodes for p in sources_of(param, nodes)):
                yield param



def _data_only_subgraph(nx_graph: nx.DiGraph) -> nx.DiGraph:
    """Return subgraph containing only data edges (no control edges)."""
    data_edges = [
        (u, v) for u, v, data in nx_graph.edges(data=True)
        if data.get("edge_type") == "data"
    ]
    subgraph = nx.DiGraph()
    subgraph.add_nodes_from(nx_graph.nodes())
    subgraph.add_edges_from(data_edges)
    return subgraph


def _collect_bound_values(
    nodes: dict[str, "HyperNode"],
    bound: dict[str, Any],
) -> dict[str, Any]:
    """Collect all bound values from graph and nested GraphNodes.

    When a graph contains GraphNodes with bound values, those values need to be
    accessible at runtime for parameter resolution. This function merges:
    1. The graph's own bound values
    2. Bound values from all nested GraphNodes (recursively)

    Args:
        nodes: Map of node name -> HyperNode
        bound: Bound values from the current graph

    Returns:
        Merged dict of all bound values (current graph + nested graphs)
    """
    from hypergraph.nodes.graph_node import GraphNode

    # Start with current graph's bound values
    all_bound = dict(bound)

    # Merge bound values from nested GraphNodes
    for node in nodes.values():
        if isinstance(node, GraphNode):
            # Get bound values from the inner graph
            inner_bound = node.graph.inputs.bound
            # Merge into all_bound (current graph's values take precedence)
            for key, value in inner_bound.items():
                if key not in all_bound:
                    all_bound[key] = value

    return all_bound
