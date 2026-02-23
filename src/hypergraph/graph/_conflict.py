"""Output conflict validation for graphs.

Validates that multiple nodes producing the same output are either in
mutually exclusive branches (mutex) or provably ordered.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
    from hypergraph.nodes.base import HyperNode

from hypergraph.graph.validation import GraphConfigError


def validate_output_conflicts(
    G: nx.DiGraph,
    nodes: list[HyperNode],
    output_to_sources: dict[str, list[str]],
) -> None:
    """Validate that duplicate outputs are mutex or ordered.

    Two producers of the same output are allowed if they are:
    1. In mutually exclusive gate branches (mutex), OR
    2. Connected by a directed path after removing contested data edges (ordered)

    Args:
        G: The NetworkX directed graph (with edges from first producer only)
        nodes: List of all nodes in the graph
        output_to_sources: Mapping from output name to list of nodes producing it

    Raises:
        GraphConfigError: If multiple nodes produce the same output and they
            are neither mutex nor ordered.
    """
    expanded_groups = _expand_mutex_groups(G, nodes)

    # Collect outputs that have multiple producers
    contested_outputs = {
        output: sources
        for output, sources in output_to_sources.items()
        if len(sources) > 1
    }
    if not contested_outputs:
        return

    # Build complete edge map with edges from ALL producers
    node_names, edge_map = _build_full_edge_map(G, nodes, output_to_sources)

    for output, sources in contested_outputs.items():
        # Find all outputs contested by THIS set of producers
        producer_set = set(sources)
        all_contested = _contested_values_for(producer_set, output_to_sources)

        for a, b in combinations(sources, 2):
            if _is_pair_mutex(a, b, expanded_groups):
                continue
            if _is_pair_ordered(a, b, all_contested, node_names, edge_map):
                continue

            raise GraphConfigError(
                f"Multiple nodes produce '{output}'\n\n"
                f"  -> {a} creates '{output}'\n"
                f"  -> {b} creates '{output}'\n\n"
                f"How to fix:\n"
                f"  - Add ordering with emit/wait_for between the producers\n"
                f"  - Or place them in exclusive gate branches"
            )


def _contested_values_for(
    producer_set: set[str],
    output_to_sources: dict[str, list[str]],
) -> set[str]:
    """Find all output names that are contested by the given producer set."""
    return {
        output
        for output, sources in output_to_sources.items()
        if len(sources) > 1 and len(producer_set & set(sources)) > 1
    }


class _EdgeInfo:
    """Track multiple edge types between a (u, v) pair."""

    __slots__ = ("data_values", "has_control", "has_ordering")

    def __init__(self) -> None:
        self.data_values: set[str] = set()
        self.has_control: bool = False
        self.has_ordering: bool = False


def _build_full_edge_map(
    G: nx.DiGraph,
    nodes: list[HyperNode],
    output_to_sources: dict[str, list[str]],
) -> tuple[set[str], dict[tuple[str, str], _EdgeInfo]]:
    """Build a complete edge map with edges from ALL producers.

    Returns node names and a dict mapping (u, v) to EdgeInfo that tracks
    all edge types (data, control, ordering) between each pair. This avoids
    the DiGraph limitation of one edge per pair.
    """
    node_names = set(G.nodes())
    edges: dict[tuple[str, str], _EdgeInfo] = {}

    def get_info(u: str, v: str) -> _EdgeInfo:
        key = (u, v)
        if key not in edges:
            edges[key] = _EdgeInfo()
        return edges[key]

    # Copy existing edges from G
    for u, v, data in G.edges(data=True):
        info = get_info(u, v)
        edge_type = data.get("edge_type")
        if edge_type == "data":
            info.data_values.update(data.get("value_names", []))
        elif edge_type == "control":
            info.has_control = True
        elif edge_type == "ordering":
            info.has_ordering = True

    # Add data edges from non-first producers
    for node in nodes:
        for param in node.inputs:
            for source in output_to_sources.get(param, []):
                get_info(source, node.name).data_values.add(param)

    # Add ordering edges from wait_for (may have been suppressed in G)
    for node in nodes:
        for name in node.wait_for:
            producers = output_to_sources.get(name, [])
            for producer in producers:
                if producer != node.name:
                    get_info(producer, node.name).has_ordering = True

    return node_names, edges


def _is_pair_mutex(
    a: str, b: str, expanded_groups: list[list[set[str]]]
) -> bool:
    """Check if two nodes are in different branches of the same exclusive gate."""
    for branches in expanded_groups:
        a_branch = None
        b_branch = None
        for i, branch in enumerate(branches):
            if a in branch:
                a_branch = i
            if b in branch:
                b_branch = i
        if a_branch is not None and b_branch is not None and a_branch != b_branch:
            return True
    return False


def _is_pair_ordered(
    a: str,
    b: str,
    contested_values: set[str],
    node_names: set[str],
    edge_map: dict[tuple[str, str], _EdgeInfo],
) -> bool:
    """Check if a directed path exists between a and b after removing contested data edges.

    An edge (u, v) is kept if:
    - It has control or ordering type, OR
    - Its data values are not ALL contested
    """
    sub = nx.DiGraph()
    sub.add_nodes_from(node_names)

    for (u, v), info in edge_map.items():
        # Keep if any non-data edge type exists
        if info.has_control or info.has_ordering:
            sub.add_edge(u, v)
            continue
        # Keep data edge if it carries at least one non-contested value
        if info.data_values and not info.data_values.issubset(contested_values):
            sub.add_edge(u, v)

    return nx.has_path(sub, a, b) or nx.has_path(sub, b, a)



def _compute_exclusive_reachability(
    G: nx.DiGraph, targets: list[str]
) -> dict[str, set[str]]:
    """For each target, find nodes reachable ONLY through that target.

    A node is "exclusively reachable" from target T if:
    - It is reachable from T (via graph edges)
    - It is NOT reachable from any other target
    """
    reachable: dict[str, set[str]] = {
        t: set(nx.descendants(G, t)) | {t} for t in targets
    }

    all_reachable_nodes = [node for nodes in reachable.values() for node in nodes]
    node_counts = Counter(all_reachable_nodes)

    return {
        t: {node for node in reachable[t] if node_counts[node] == 1}
        for t in targets
    }


def _expand_mutex_groups(
    G: nx.DiGraph, nodes: list[HyperNode]
) -> list[list[set[str]]]:
    """Expand mutex groups to include downstream exclusive nodes.

    For each gate with mutually exclusive targets (RouteNode with multi_target=False
    or IfElseNode), expands the mutex relationship to include all nodes
    that are exclusively reachable through each target.

    Returns:
        List of mutex group sets, where each element is a list of branch sets.
        Nodes are mutex only if they're in DIFFERENT branch sets of the same gate.
    """
    from hypergraph.nodes.gate import END, IfElseNode, RouteNode

    expanded_groups: list[list[set[str]]] = []

    for node in nodes:
        if isinstance(node, RouteNode):
            if node.multi_target:
                continue
        elif not isinstance(node, IfElseNode):
            continue

        targets = [t for t in node.targets if t is not END and isinstance(t, str)]
        if len(targets) < 2:
            continue

        exclusive_sets = _compute_exclusive_reachability(G, targets)
        expanded_groups.append(list(exclusive_sets.values()))

    return expanded_groups
