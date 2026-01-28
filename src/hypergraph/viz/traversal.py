"""Graph traversal utilities for visualization.

Provides functions for traversing hierarchical graph structures with
configurable expansion predicates.
"""

from __future__ import annotations

from typing import Callable

import networkx as nx


def get_children(G: nx.DiGraph, parent_id: str) -> list[str]:
    """Get direct children of a node from NetworkX graph.

    Children are nodes where the 'parent' attribute equals the given parent_id.

    Args:
        G: NetworkX DiGraph with 'parent' attribute on nodes
        parent_id: ID of the parent node

    Returns:
        List of node IDs that are direct children of parent_id

    Example:
        >>> G = nx.DiGraph()
        >>> G.add_node('a', parent=None)
        >>> G.add_node('b', parent='a')
        >>> G.add_node('c', parent='a')
        >>> get_children(G, 'a')
        ['b', 'c']
    """
    return [
        node_id
        for node_id, attrs in G.nodes(data=True)
        if attrs.get("parent") == parent_id
    ]


def traverse_to_leaves(
    G: nx.DiGraph,
    node_id: str,
    should_expand: Callable[[nx.DiGraph, str, int], bool],
) -> list[str]:
    """Recursively traverse graph, expanding nodes based on predicate.

    Visits nodes depth-first. At each node, calls should_expand to decide
    whether to recurse into children or treat as a leaf.

    Args:
        G: NetworkX DiGraph with 'parent' attribute on nodes
        node_id: Starting node ID
        should_expand: Predicate (graph, node_id, depth) -> bool
            Returns True if children should be expanded

    Returns:
        List of leaf node IDs (nodes where expansion stopped)
    """
    leaves: list[str] = []
    _traverse_recursive(G, node_id, should_expand, depth=0, leaves=leaves)
    return leaves


def _traverse_recursive(
    G: nx.DiGraph,
    node_id: str,
    should_expand: Callable[[nx.DiGraph, str, int], bool],
    depth: int,
    leaves: list[str],
) -> None:
    """Recursive helper for traverse_to_leaves."""
    if not should_expand(G, node_id, depth):
        leaves.append(node_id)
        return

    children = get_children(G, node_id)

    if not children:
        leaves.append(node_id)
        return

    for child_id in children:
        _traverse_recursive(G, child_id, should_expand, depth + 1, leaves)


def build_expansion_predicate(
    max_depth: int | None = None,
) -> Callable[[nx.DiGraph, str, int], bool]:
    """Factory for creating depth-based expansion predicates.

    Args:
        max_depth: Maximum depth to expand (None = unlimited)

    Returns:
        Predicate function (graph, node_id, depth) -> bool

    Example:
        >>> predicate = build_expansion_predicate(max_depth=2)
        >>> predicate(G, 'node1', 1)
        True
        >>> predicate(G, 'node2', 2)
        False
    """
    if max_depth is None:
        return lambda G, node_id, depth: True

    def predicate(G: nx.DiGraph, node_id: str, depth: int) -> bool:
        return depth < max_depth

    return predicate
