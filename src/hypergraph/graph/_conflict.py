"""Output conflict validation for graphs.

Validates that multiple nodes producing the same output are either in
mutually exclusive branches (mutex) or in the same cycle.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
    from hypergraph.nodes.base import HyperNode

from hypergraph.graph.validation import GraphConfigError


def validate_output_conflicts(
    G: nx.DiGraph,
    nodes: list["HyperNode"],
    output_to_sources: dict[str, list[str]],
) -> None:
    """Validate that duplicate outputs are in mutually exclusive branches.

    Called after the graph structure is built (edges added) so we can
    use graph reachability to determine if nodes producing the same output
    are in mutex branches.

    Args:
        G: The NetworkX directed graph (with edges)
        nodes: List of all nodes in the graph
        output_to_sources: Mapping from output name to list of nodes producing it

    Raises:
        GraphConfigError: If multiple nodes produce the same output and they
            are not in mutually exclusive branches.
    """
    expanded_groups = _expand_mutex_groups(G, nodes)

    for output, sources in output_to_sources.items():
        if len(sources) <= 1:
            continue

        if _are_all_mutex(sources, expanded_groups):
            continue

        if _are_all_in_same_cycle(sources, G, nodes, output_to_sources):
            continue

        raise GraphConfigError(
            f"Multiple nodes produce '{output}'\n\n"
            f"  -> {sources[0]} creates '{output}'\n"
            f"  -> {sources[1]} creates '{output}'\n\n"
            f"How to fix: Rename one output to avoid conflict"
        )


def _are_all_mutex(
    node_names: list[str], mutex_groups: list[list[set[str]]]
) -> bool:
    """Check if all given nodes are mutually exclusive.

    Two nodes are mutex if they're in different branches of the same gate.
    All given nodes are mutex if each is in a different branch of the same gate.
    """
    if len(node_names) < 2:
        return True

    for branches in mutex_groups:
        nodes_by_branch = []
        for branch in branches:
            branch_nodes = [n for n in node_names if n in branch]
            if branch_nodes:
                nodes_by_branch.append(branch_nodes)

        if len(nodes_by_branch) == len(node_names):
            return True

    return False


def _are_all_in_same_cycle(
    node_names: list[str],
    G: nx.DiGraph,
    nodes: list["HyperNode"],
    output_to_sources: dict[str, list[str]],
) -> bool:
    """Check if all given nodes are in at least one common cycle.

    Builds a temporary graph with edges from ALL producers (not just the
    first) so that cycles involving multiple producers of the same output
    are detected correctly.
    """
    if len(node_names) < 2:
        return True

    temp = nx.DiGraph()
    temp.add_nodes_from(G.nodes())
    for u, v, data in G.edges(data=True):
        if data.get("edge_type") == "data":
            temp.add_edge(u, v)
    for node in nodes:
        for param in node.inputs:
            for source in output_to_sources.get(param, []):
                temp.add_edge(source, node.name)

    nodes_set = set(node_names)
    return any(nodes_set.issubset(set(cycle)) for cycle in nx.simple_cycles(temp))


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
    G: nx.DiGraph, nodes: list["HyperNode"]
) -> list[list[set[str]]]:
    """Expand mutex groups to include downstream exclusive nodes.

    For each gate with mutually exclusive targets (RouteNode with multi_target=False
    or IfElseNode), expands the mutex relationship to include all nodes
    that are exclusively reachable through each target.

    Returns:
        List of mutex group sets, where each element is a list of branch sets.
        Nodes are mutex only if they're in DIFFERENT branch sets of the same gate.
    """
    from hypergraph.nodes.gate import RouteNode, IfElseNode, END

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
