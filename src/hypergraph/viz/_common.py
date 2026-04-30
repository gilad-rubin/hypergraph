"""Shared algorithms for the visualization system.

These functions are used by both renderer/ (React Flow format) and
instructions.py (explicit VizInstructions format). Keeping them in
one place prevents the two code paths from diverging.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations, product
from typing import TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
    pass


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
    return sorted([node_id for node_id, attrs in flat_graph.nodes(data=True) if attrs.get("node_type") == "GRAPH"])


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


# =============================================================================
# Mutex (exclusive-branch) Detection
# =============================================================================


def _gate_branch_targets(branch_data: dict) -> list[str]:
    """Return mutex-branch targets for a gate, or [] if not exclusive."""
    if branch_data.get("multi_target"):
        return []
    targets: list[str] = []
    if "when_true" in branch_data:
        for key in ("when_true", "when_false"):
            value = branch_data.get(key)
            if isinstance(value, str) and value and value != "END":
                targets.append(value)
    elif "targets" in branch_data:
        target_data = branch_data["targets"]
        values = target_data.values() if isinstance(target_data, dict) else target_data
        targets = [t for t in values if isinstance(t, str) and t and t != "END"]
    return targets


def _compute_mutex_groups(flat_graph: nx.DiGraph) -> list[list[set[str]]]:
    """Compute mutex groups from a flat graph using gate ``branch_data``.

    Each entry is a list of branch sets (one per gate target); two nodes are
    mutex iff they appear in different branch sets of the same entry.
    """
    groups: list[list[set[str]]] = []
    for _, attrs in flat_graph.nodes(data=True):
        branch_data = attrs.get("branch_data") or {}
        if not branch_data:
            continue
        targets = _gate_branch_targets(branch_data)
        targets = [t for t in targets if t in flat_graph]
        if len(targets) < 2:
            continue
        reachable = {t: set(nx.descendants(flat_graph, t)) | {t} for t in targets}
        counts = Counter(node for nodes in reachable.values() for node in nodes)
        groups.append([{n for n in reachable[t] if counts[n] == 1} for t in targets])
    return groups


def _is_pair_mutex(a: str, b: str, mutex_groups: list[list[set[str]]]) -> bool:
    """Return True if two nodes are in different branches of the same gate."""
    for branches in mutex_groups:
        a_branch: int | None = None
        b_branch: int | None = None
        for i, branch in enumerate(branches):
            if a in branch:
                a_branch = i
            if b in branch:
                b_branch = i
        if a_branch is not None and b_branch is not None and a_branch != b_branch:
            return True
    return False


def compute_exclusive_data_edges(flat_graph: nx.DiGraph) -> set[tuple[str, str, str]]:
    """Identify ``(source, target, value_name)`` data edges fed by mutex producers.

    An edge is exclusive when another producer of the same value name feeds the
    same consumer and the two producers live in different branches of the same
    exclusive gate. Returned tuples cover both producers in such pairs.
    """
    mutex_groups = _compute_mutex_groups(flat_graph)
    if not mutex_groups:
        return set()

    producers_by_input: dict[tuple[str, str], list[str]] = defaultdict(list)
    for source, target, attrs in flat_graph.edges(data=True):
        if attrs.get("edge_type") != "data":
            continue
        # Match renderer behaviour: edges without explicit value names still
        # emit a single edge keyed by "" — track them under that key too.
        value_names = attrs.get("value_names") or [""]
        for value_name in value_names:
            producers_by_input[(target, value_name)].append(source)

    exclusive: set[tuple[str, str, str]] = set()
    for (target, value_name), producers in producers_by_input.items():
        if len(producers) < 2:
            continue
        mutex_sources: set[str] = set()
        for a, b in combinations(producers, 2):
            if _is_pair_mutex(a, b, mutex_groups):
                mutex_sources.add(a)
                mutex_sources.add(b)
        for source in mutex_sources:
            exclusive.add((source, target, value_name))
    return exclusive
