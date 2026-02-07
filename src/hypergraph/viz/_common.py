"""Shared algorithms for the visualization system.

These functions are used by both renderer/ (React Flow format) and
instructions.py (explicit VizInstructions format). Keeping them in
one place prevents the two code paths from diverging.
"""

from __future__ import annotations

from itertools import product
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import networkx as nx


# =============================================================================
# Expansion State
# =============================================================================


def build_expansion_state(flat_graph: nx.DiGraph, depth: int) -> dict[str, bool]:
    """Build map of node_id -> is_expanded for all GRAPH nodes."""
    expansion_state = {}
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("node_type") == "GRAPH":
            parent_id = attrs.get("parent")
            expansion_state[node_id] = is_node_expanded(node_id, parent_id, depth, flat_graph)
    return expansion_state


def is_node_expanded(
    node_id: str,
    parent_id: str | None,
    depth: int,
    flat_graph: nx.DiGraph,
) -> bool | None:
    """Determine if a GRAPH node should be expanded based on depth."""
    attrs = flat_graph.nodes[node_id]
    if attrs.get("node_type") != "GRAPH":
        return None

    nesting_level = 0
    current_parent = parent_id
    while current_parent is not None:
        nesting_level += 1
        current_parent = flat_graph.nodes[current_parent].get("parent")

    return depth > nesting_level


# =============================================================================
# Node Visibility and Traversal
# =============================================================================


def is_node_visible(
    node_id: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> bool:
    """Check if a node is visible (not hidden and all ancestors are expanded)."""
    attrs = flat_graph.nodes[node_id]

    if attrs.get("hide", False):
        return False

    parent_id = attrs.get("parent")
    while parent_id is not None:
        if not expansion_state.get(parent_id, False):
            return False
        parent_attrs = flat_graph.nodes[parent_id]
        parent_id = parent_attrs.get("parent")

    return True


def get_nesting_depth(node_id: str, flat_graph: nx.DiGraph) -> int:
    """Get the nesting depth of a node (0 = root level)."""
    depth = 0
    attrs = flat_graph.nodes[node_id]
    parent_id = attrs.get("parent")

    while parent_id is not None:
        depth += 1
        parent_attrs = flat_graph.nodes[parent_id]
        parent_id = parent_attrs.get("parent")

    return depth


def get_parent(node_id: str, flat_graph: nx.DiGraph) -> str | None:
    """Get the parent of a node."""
    if node_id not in flat_graph.nodes:
        return None
    return flat_graph.nodes[node_id].get("parent")


def get_root_ancestor(node_id: str, flat_graph: nx.DiGraph) -> str:
    """Get the root-level ancestor of a node (or itself if root-level)."""
    attrs = flat_graph.nodes[node_id]
    parent_id = attrs.get("parent")

    if parent_id is None:
        return node_id

    while True:
        parent_attrs = flat_graph.nodes[parent_id]
        grandparent = parent_attrs.get("parent")
        if grandparent is None:
            return parent_id
        parent_id = grandparent


def is_descendant_of(node_id: str, ancestor_id: str, flat_graph: nx.DiGraph) -> bool:
    """Check if node_id is a descendant of ancestor_id."""
    current = node_id
    while current is not None:
        parent = get_parent(current, flat_graph)
        if parent == ancestor_id:
            return True
        current = parent
    return False


# =============================================================================
# Expansion State Enumeration
# =============================================================================


def get_expandable_nodes(flat_graph: nx.DiGraph) -> list[str]:
    """Get list of node IDs that can be expanded/collapsed (GRAPH nodes)."""
    return sorted([
        node_id
        for node_id, attrs in flat_graph.nodes(data=True)
        if attrs.get("node_type") == "GRAPH"
    ])


def expansion_state_to_key(expansion_state: dict[str, bool]) -> str:
    """Convert expansion state dict to a canonical string key.

    Format: "node1:0,node2:1" (sorted alphabetically, 0=collapsed, 1=expanded)
    """
    sorted_items = sorted(expansion_state.items())
    return ",".join(f"{node_id}:{int(expanded)}" for node_id, expanded in sorted_items)


def enumerate_valid_expansion_states(
    flat_graph: nx.DiGraph,
    expandable_nodes: list[str],
) -> list[dict[str, bool]]:
    """Enumerate all valid expansion state combinations.

    A state is valid if expanded children only appear when their parent is also expanded.
    This prunes unreachable states (e.g., inner expanded when outer collapsed).
    """
    if not expandable_nodes:
        return [{}]

    node_to_parent: dict[str, str] = {}
    for node_id in expandable_nodes:
        parent_id = flat_graph.nodes[node_id].get("parent")
        if parent_id in expandable_nodes:
            node_to_parent[node_id] = parent_id

    valid_states = []

    for bits in product([False, True], repeat=len(expandable_nodes)):
        state = dict(zip(expandable_nodes, bits, strict=True))

        is_valid = True
        for node_id, is_expanded in state.items():
            if is_expanded:
                parent = node_to_parent.get(node_id)
                while parent is not None:
                    if not state.get(parent, False):
                        is_valid = False
                        break
                    parent = node_to_parent.get(parent)
                if not is_valid:
                    break

        if is_valid:
            valid_states.append(state)

    return valid_states


# =============================================================================
# Parameter / Output Maps
# =============================================================================


def build_param_to_consumer_map(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    use_deepest: bool = False,
    mode: str = "all",
) -> dict[str, list[str]]:
    """Build map of param_name -> list of actual consumer node_ids.

    Returns:
        Dict mapping parameter names to list of consumer node IDs.
        Multiple consumers are supported (e.g., route graphs where multiple
        functions consume the same input parameter).
    """
    param_to_consumers: dict[str, list[str]] = {}

    if use_deepest:
        mode = "all"

    for node_id, attrs in flat_graph.nodes(data=True):
        for param in attrs.get("inputs", ()):
            if not use_deepest and not is_node_visible(node_id, flat_graph, expansion_state):
                continue

            if param not in param_to_consumers:
                param_to_consumers[param] = []
            param_to_consumers[param].append(node_id)

    # Filter out containers that have deeper visible consumers
    for param, consumers in param_to_consumers.items():
        if len(consumers) <= 1:
            continue

        filtered = []
        for consumer in consumers:
            has_deeper_descendant = False
            for other in consumers:
                if other == consumer:
                    continue
                if is_descendant_of(other, consumer, flat_graph):
                    has_deeper_descendant = True
                    break
            if not has_deeper_descendant:
                filtered.append(consumer)
        param_to_consumers[param] = filtered

    if mode == "primary":
        for param, consumers in param_to_consumers.items():
            if not consumers:
                continue
            selected = max(
                consumers,
                key=lambda node_id: (get_nesting_depth(node_id, flat_graph), node_id),
            )
            param_to_consumers[param] = [selected]

    return param_to_consumers


def build_output_to_producer_map(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    use_deepest: bool = False,
) -> dict[str, str]:
    """Build map of output_value_name -> actual_producer_node_id."""
    output_to_producer: dict[str, str] = {}

    for node_id, attrs in flat_graph.nodes(data=True):
        for output in attrs.get("outputs", ()):
            if not use_deepest and not is_node_visible(node_id, flat_graph, expansion_state):
                continue

            if output not in output_to_producer:
                output_to_producer[output] = node_id
            else:
                existing = output_to_producer[output]
                if get_nesting_depth(node_id, flat_graph) > get_nesting_depth(existing, flat_graph):
                    output_to_producer[output] = node_id

    return output_to_producer
