"""Render NetworkX graph to React Flow JSON format.

This module transforms a flattened NetworkX DiGraph into the React Flow
node/edge format expected by the visualization.
"""

from __future__ import annotations

from itertools import product
from typing import Any

import networkx as nx


def _format_type(t: type | None) -> str | None:
    """Format a type annotation for display.

    Converts type objects to clean string representations:
    - list[float] → "list[float]"
    - Dict[str, int] → "Dict[str, int]"
    - mymodule.MyClass → "MyClass"
    """
    if t is None:
        return None

    # Always use str() to preserve generic parameters (list[float], Dict[str, int], etc.)
    # Then clean up common prefixes
    type_str = str(t)
    type_str = type_str.replace("typing.", "")
    type_str = type_str.replace("<class '", "").replace("'>", "")

    # Simplify fully-qualified names: foo.bar.ClassName → ClassName
    return _simplify_type_string(type_str)


def _simplify_type_string(type_str: str) -> str:
    """Simplify type strings by extracting only the final part of dotted names.

    Examples:
        "list[mymodule.Document]" → "list[Document]"
        "Dict[str, foo.bar.Baz]" → "Dict[str, Baz]"
    """
    import re

    # Pattern to match dotted names (e.g., a.b.c.ClassName)
    pattern = r"([a-zA-Z_][a-zA-Z0-9_]*\.)+([a-zA-Z_][a-zA-Z0-9_]*)"

    def replace_with_final(match: re.Match) -> str:
        full_match = match.group(0)
        parts = full_match.split(".")
        return parts[-1]

    return re.sub(pattern, replace_with_final, type_str)


def render_graph(
    flat_graph: nx.DiGraph,
    *,
    depth: int = 1,
    theme: str = "auto",
    show_types: bool = False,
    separate_outputs: bool = False,
    layout_profile: str | None = None,
    debug_overlays: bool = False,
) -> dict[str, Any]:
    """Convert a flattened NetworkX graph to React Flow JSON format.

    Args:
        flat_graph: NetworkX DiGraph from Graph.to_flat_graph()
        depth: How many levels of nested graphs to expand (0 = collapsed)
        theme: "dark", "light", or "auto" (detect from environment)
        show_types: Whether to show type annotations
        separate_outputs: Whether to render outputs as separate DATA nodes
        layout_profile: Optional layout profile override (e.g. "classic")
        debug_overlays: Whether to enable debug overlays (internal use)

    Returns:
        Dict with "nodes", "edges", and "meta" keys ready for React Flow
    """
    # Get input_spec from graph attributes
    input_spec = flat_graph.graph.get("input_spec", {})
    bound_params = set(input_spec.get("bound", {}).keys())
    profile = layout_profile or "modern"
    render_consumer_mode = "all"
    layout_consumer_mode = "primary" if profile == "classic" else "all"

    # Build maps for routing edges to actual internal nodes when expanded
    expansion_state = _build_expansion_state(flat_graph, depth)
    # For static edges: use visibility-based targets
    param_to_consumer = _build_param_to_consumer_map(
        flat_graph,
        expansion_state,
        mode=render_consumer_mode,
    )
    input_groups = (
        _build_classic_input_groups(input_spec, bound_params)
        if profile == "classic"
        else _build_input_groups(input_spec, param_to_consumer, bound_params)
    )
    graph_output_visibility = _build_graph_output_visibility(flat_graph)
    # For JS meta data: use deepest targets (for interactive expand routing)
    param_to_consumer_deepest = _build_param_to_consumer_map(flat_graph, expansion_state, use_deepest=True)
    output_to_producer_deepest = _build_output_to_producer_map(flat_graph, expansion_state, use_deepest=True)
    # For JS meta data: node-to-parent map for routing
    node_to_parent = _build_node_to_parent_map(flat_graph)

    # Pre-compute edges for ALL valid expansion state combinations
    # JavaScript can select the correct edge set based on current expansion state
    edges_by_state, expandable_nodes = _precompute_all_edges(
        flat_graph,
        input_spec,
        show_types,
        theme,
        input_groups,
        graph_output_visibility,
        input_consumer_mode=render_consumer_mode,
    )

    layout_edges_by_state = None
    if profile == "classic":
        layout_edges_by_state, _ = _precompute_all_edges(
            flat_graph,
            input_spec,
            show_types,
            theme,
            input_groups,
            graph_output_visibility,
            input_consumer_mode=layout_consumer_mode,
        )

    # Pre-compute nodes for ALL valid expansion state combinations
    nodes_by_state, _ = _precompute_all_nodes(
        flat_graph,
        input_spec,
        show_types,
        theme,
        graph_output_visibility=graph_output_visibility,
        input_groups=input_groups,
        input_consumer_mode=render_consumer_mode,
    )

    # Use pre-computed edges for the initial state
    # This ensures initial render matches what JS would select from edgesByState
    initial_state_key = _expansion_state_to_key(expansion_state)
    sep_key = "sep:1" if separate_outputs else "sep:0"
    full_initial_key = f"{initial_state_key}|{sep_key}" if initial_state_key else sep_key
    initial_edges = edges_by_state.get(full_initial_key, [])
    initial_nodes = nodes_by_state.get(full_initial_key, [])

    # Sort nodes and edges by ID for deterministic ordering (prevents layout flickering)
    initial_nodes.sort(key=lambda n: n["id"])
    initial_edges.sort(key=lambda e: e["id"])

    return {
        "nodes": initial_nodes,
        "edges": initial_edges,
        "meta": {
            "theme_preference": theme,
            "initial_depth": depth,
            "separate_outputs": separate_outputs,
            "show_types": show_types,
            "debug_overlays": debug_overlays,
            "layout_profile": profile,
            # Routing data for JS to re-route edges to actual internal nodes
            # Use deepest targets so interactive expand can route correctly
            "output_to_producer": output_to_producer_deepest,
            "param_to_consumer": param_to_consumer_deepest,
            "node_to_parent": node_to_parent,
            # Pre-computed edges for all expansion states (collapse/expand consistency)
            "edgesByState": edges_by_state,
            "layoutEdgesByState": layout_edges_by_state,
            # Pre-computed nodes for all expansion states (Python-driven visibility)
            "nodesByState": nodes_by_state,
            "expandableNodes": expandable_nodes,
        },
    }


def _build_expansion_state(flat_graph: nx.DiGraph, depth: int) -> dict[str, bool]:
    """Build map of node_id -> is_expanded for all GRAPH nodes."""
    expansion_state = {}
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("node_type") == "GRAPH":
            parent_id = attrs.get("parent")
            expansion_state[node_id] = _is_node_expanded(node_id, parent_id, depth, flat_graph)
    return expansion_state


def _build_node_to_parent_map(flat_graph: nx.DiGraph) -> dict[str, str]:
    """Build mapping from node name to parent name for routing."""
    return {
        node_id: attrs.get("parent")
        for node_id, attrs in flat_graph.nodes(data=True)
        if attrs.get("parent") is not None
    }


def _build_param_to_consumer_map(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    use_deepest: bool = False,
    mode: str = "all",
) -> dict[str, list[str]]:
    """Build map of param_name -> list of actual consumer node_ids.

    Args:
        use_deepest: If True, include all consumers (for JS interactive routing).
                     If False, only include visible consumers (for static edges).

    Returns:
        Dict mapping parameter names to list of consumer node IDs.
        Multiple consumers are supported (e.g., route graphs where multiple
        functions consume the same input parameter).

        For nested graphs, when a container is expanded, we route to the internal
        consumer (not the container). When multiple independent consumers exist
        at the same level (like route targets), all get edges.
    """
    param_to_consumers: dict[str, list[str]] = {}

    if use_deepest:
        mode = "all"

    for node_id, attrs in flat_graph.nodes(data=True):
        for param in attrs.get("inputs", ()):
            # Check visibility unless we want deepest
            if not use_deepest and not _is_node_visible(node_id, flat_graph, expansion_state):
                continue

            if param not in param_to_consumers:
                param_to_consumers[param] = []
            param_to_consumers[param].append(node_id)

    # For each parameter, filter out containers that have deeper visible consumers
    # This ensures INPUT edges route to internal nodes, not containers
    for param, consumers in param_to_consumers.items():
        if len(consumers) <= 1:
            continue

        # Keep only consumers that don't have a deeper descendant in the list
        filtered = []
        for consumer in consumers:
            has_deeper_descendant = False
            for other in consumers:
                if other == consumer:
                    continue
                # Check if 'other' is a descendant of 'consumer'
                if _is_descendant_of(other, consumer, flat_graph):
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
                key=lambda node_id: (_get_nesting_depth(node_id, flat_graph), node_id),
            )
            param_to_consumers[param] = [selected]

    return param_to_consumers


def _is_descendant_of(node_id: str, ancestor_id: str, flat_graph: nx.DiGraph) -> bool:
    """Check if node_id is a descendant of ancestor_id."""
    current = node_id
    while current is not None:
        parent = _get_parent(current, flat_graph)
        if parent == ancestor_id:
            return True
        current = parent
    return False


def _find_container_entry_points(
    container_id: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> list[str]:
    """Find entry point nodes inside a container for control edge routing.

    Entry points are nodes inside the container that:
    1. Are direct children of the container (not nested deeper)
    2. Have no data predecessors from within the container
    3. Are visible in the current expansion state

    These are the nodes that would "receive" control flow from outside.
    """
    # Find direct children of the container
    direct_children = [
        node_id
        for node_id, attrs in flat_graph.nodes(data=True)
        if attrs.get("parent") == container_id
    ]

    # Find internal producers (nodes inside container that produce outputs)
    internal_outputs = set()
    for node_id in direct_children:
        attrs = flat_graph.nodes.get(node_id, {})
        for output in attrs.get("outputs", ()):
            internal_outputs.add(output)

    # Entry points are children that don't consume internal outputs
    entry_points = []
    for node_id in direct_children:
        attrs = flat_graph.nodes.get(node_id, {})
        inputs = set(attrs.get("inputs", ()))

        # Check if this node consumes any internal outputs
        consumes_internal = bool(inputs & internal_outputs)

        # Include if doesn't consume internal outputs and is visible
        if not consumes_internal:
            if _is_node_visible(node_id, flat_graph, expansion_state):
                entry_points.append(node_id)

    return entry_points


def _find_container_exit_points(
    container_id: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> list[str]:
    """Find exit point nodes inside a container for data edge routing.

    Exit points are nodes inside the container that:
    1. Are direct children of the container (not nested deeper)
    2. Produce outputs (have non-empty outputs attribute)
    3. Are visible in the current expansion state

    These are the nodes that would "send" data flow to outside.
    For containers with a single child (like mapped graphs), this is that child.
    """
    # Find direct children of the container that produce outputs
    exit_points = []
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("parent") != container_id:
            continue
        if not attrs.get("outputs", ()):
            continue
        if _is_node_visible(node_id, flat_graph, expansion_state):
            exit_points.append(node_id)

    return exit_points


def _find_internal_producer_for_output(
    container_id: str,
    output_name: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> str | None:
    """Find the internal node that produces the data that becomes `output_name`.

    This handles the `with_outputs` rename case: when a container exposes
    `retrieval_eval_results` but internally `compute_recall` produces
    `retrieval_eval_result`, we need to find `compute_recall`.

    Strategy: Find the internal node that:
    1. Is a direct child of the container (or deeper, via recursion)
    2. Produces an output that is NOT consumed by any other node inside the container
       (i.e., it's a "terminal" output that flows outside)
    3. That terminal output becomes `output_name` after container-level renaming

    For simplicity, we look for internal nodes whose outputs match any "tail" of
    `output_name` (e.g., "retrieval_eval_result" ends with most of "retrieval_eval_results").
    """
    # Get all outputs produced by internal nodes (direct children)
    internal_producers: dict[str, str] = {}  # output_name -> producer_id
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("parent") != container_id:
            continue
        for output in attrs.get("outputs", ()):
            internal_producers[output] = node_id

    # Find all outputs consumed internally
    internal_consumed: set[str] = set()
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("parent") != container_id:
            continue
        for inp in attrs.get("inputs", ()):
            if inp in internal_producers:
                internal_consumed.add(inp)

    # Terminal outputs are those NOT consumed internally
    terminal_outputs = {
        out: prod
        for out, prod in internal_producers.items()
        if out not in internal_consumed
    }

    # Try exact match first (output_name exists internally)
    if output_name in terminal_outputs:
        producer = terminal_outputs[output_name]
        if _is_node_visible(producer, flat_graph, expansion_state):
            return producer

    # Try fuzzy match for renamed outputs (e.g., retrieval_eval_result -> retrieval_eval_results)
    # Check if any terminal output is a prefix/suffix match
    for internal_out, producer in terminal_outputs.items():
        # Check if one is substring of the other (handles singular/plural, etc.)
        if internal_out in output_name or output_name in internal_out:
            if _is_node_visible(producer, flat_graph, expansion_state):
                return producer

    # Fall back to any visible terminal producer
    for internal_out, producer in terminal_outputs.items():
        if _is_node_visible(producer, flat_graph, expansion_state):
            return producer

    return None


def _build_output_to_producer_map(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    use_deepest: bool = False,
) -> dict[str, str]:
    """Build map of output_value_name -> actual_producer_node_id.

    Args:
        use_deepest: If True, include all producers (for JS interactive routing).
                     If False, only include visible producers (for static edges).
    """
    output_to_producer: dict[str, str] = {}

    for node_id, attrs in flat_graph.nodes(data=True):
        for output in attrs.get("outputs", ()):
            # Check visibility unless we want deepest
            if not use_deepest and not _is_node_visible(node_id, flat_graph, expansion_state):
                continue

            if output not in output_to_producer:
                output_to_producer[output] = node_id
            else:
                # Prefer the deeper (more specific) producer
                existing = output_to_producer[output]
                if _get_nesting_depth(node_id, flat_graph) > _get_nesting_depth(existing, flat_graph):
                    output_to_producer[output] = node_id

    return output_to_producer


def _is_node_visible(node_id: str, flat_graph: nx.DiGraph, expansion_state: dict[str, bool]) -> bool:
    """Check if a node is visible (all ancestors are expanded)."""
    attrs = flat_graph.nodes[node_id]
    parent_id = attrs.get("parent")

    while parent_id is not None:
        if not expansion_state.get(parent_id, False):
            return False
        parent_attrs = flat_graph.nodes[parent_id]
        parent_id = parent_attrs.get("parent")

    return True


def _is_data_node_visible(
    source_id: str,
    output_name: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> bool:
    """Check if a DATA node should be visible for the current expansion state."""
    if not _is_node_visible(source_id, flat_graph, expansion_state):
        return False

    source_attrs = flat_graph.nodes.get(source_id, {})
    if source_attrs.get("node_type") == "GRAPH" and expansion_state.get(source_id, False):
        return False

    return True


def _apply_node_visibility(
    nodes: list[dict[str, Any]],
    expansion_state: dict[str, bool],
    separate_outputs: bool,
) -> None:
    """Apply visibility rules to nodes in-place, setting `hidden` flags."""
    parent_map: dict[str, str] = {
        n["id"]: n["parentNode"]
        for n in nodes
        if n.get("parentNode")
    }
    pipeline_ids = {
        n["id"]
        for n in nodes
        if n.get("data", {}).get("nodeType") == "PIPELINE"
    }

    def _hidden_by_ancestor(node_id: str) -> bool:
        current = node_id
        while current:
            parent = parent_map.get(current)
            if not parent:
                return False
            if expansion_state.get(parent) is False:
                return True
            current = parent
        return False

    for node in nodes:
        data = node.get("data", {})
        node_type = data.get("nodeType")

        hidden = _hidden_by_ancestor(node["id"])

        if node_type == "DATA" and data.get("internalOnly"):
            parent = node.get("parentNode")
            if parent and not expansion_state.get(parent, False):
                hidden = True

        if node_type in ("INPUT", "INPUT_GROUP"):
            owner = data.get("deepestOwnerContainer") or data.get("ownerContainer")
            if owner:
                if expansion_state.get(owner) is not True:
                    hidden = True
                else:
                    current = parent_map.get(owner)
                    while current:
                        if expansion_state.get(current) is False:
                            hidden = True
                            break
                        current = parent_map.get(current)

        if separate_outputs:
            if node_type == "DATA":
                source_id = data.get("sourceId")
                if source_id in pipeline_ids and expansion_state.get(source_id, False):
                    hidden = True
        else:
            if node_type == "DATA":
                hidden = True

        node["hidden"] = hidden


def _get_nesting_depth(node_id: str, flat_graph: nx.DiGraph) -> int:
    """Get the nesting depth of a node (0 = root level)."""
    depth = 0
    attrs = flat_graph.nodes[node_id]
    parent_id = attrs.get("parent")

    while parent_id is not None:
        depth += 1
        parent_attrs = flat_graph.nodes[parent_id]
        parent_id = parent_attrs.get("parent")

    return depth


def _is_node_expanded(
    node_id: str,
    parent_id: str | None,
    depth: int,
    flat_graph: nx.DiGraph,
) -> bool | None:
    """Determine if a GRAPH node should be expanded based on depth."""
    attrs = flat_graph.nodes[node_id]
    if attrs.get("node_type") != "GRAPH":
        return None

    # Calculate nesting level by counting ancestors
    nesting_level = 0
    current_parent = parent_id
    while current_parent is not None:
        nesting_level += 1
        current_parent = flat_graph.nodes[current_parent].get("parent")

    return depth > nesting_level


# =============================================================================
# Scope Analysis for INPUT/OUTPUT Visibility
# =============================================================================


def _get_deepest_consumers(param: str, flat_graph: nx.DiGraph) -> list[str]:
    """Get the deepest (non-container) consumers of a parameter.

    When a container (GRAPH node) and its internal nodes both list a parameter
    as an input, we return only the internal nodes - they are the "actual"
    consumers. This filters out containers that merely pass through inputs.

    Args:
        param: Parameter name to find consumers for
        flat_graph: The flattened graph

    Returns:
        List of node IDs that actually consume this parameter
    """
    all_consumers = []
    for node_id, attrs in flat_graph.nodes(data=True):
        if param in attrs.get("inputs", ()):
            all_consumers.append(node_id)

    if len(all_consumers) <= 1:
        return all_consumers

    # Filter out containers that have deeper consumers
    # A container is "superseded" if any of its descendants also consume this param
    filtered = []
    for consumer in all_consumers:
        is_superseded = False
        for other in all_consumers:
            if other == consumer:
                continue
            # Check if 'other' is inside 'consumer' (consumer is an ancestor of other)
            if _is_descendant_of(other, consumer, flat_graph):
                is_superseded = True
                break
        if not is_superseded:
            filtered.append(consumer)

    return filtered


def _get_ancestor_chain(node_id: str, flat_graph: nx.DiGraph) -> list[str]:
    """Get the chain of container ancestors for a node, from immediate to root.

    Args:
        node_id: The node to get ancestors for
        flat_graph: The flattened graph

    Returns:
        List of container IDs from immediate parent to root.
        Empty list if node is at root level.
    """
    ancestors = []
    current = node_id
    while current is not None:
        parent = _get_parent(current, flat_graph)
        if parent is not None:
            ancestors.append(parent)
        current = parent
    return ancestors


def _find_deepest_common_container(ancestor_chains: list[list[str]]) -> str | None:
    """Find the deepest common container across all ancestor chains.

    Args:
        ancestor_chains: List of ancestor chains (each from immediate to root)

    Returns:
        The deepest container that appears in ALL chains, or None if no common
        container (i.e., some nodes are at root level or in different subtrees).
    """
    if not ancestor_chains:
        return None

    # If any chain is empty, at least one consumer is at root level
    if any(not chain for chain in ancestor_chains):
        return None

    # Find the deepest common ancestor
    # Start with the first chain's containers (from deepest to root)
    first_chain = ancestor_chains[0]

    for container in first_chain:
        # Check if this container is in all other chains
        if all(container in chain for chain in ancestor_chains[1:]):
            return container

    return None


def _compute_input_scope(
    param: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> str | None:
    """Determine which container (if any) should own this INPUT node.

    An INPUT should be placed inside a container if ALL its actual consumers
    are inside that container.

    The function finds the deepest common container of all consumers, then
    walks UP the ancestor chain to find the deepest container that is EXPANDED.
    This handles nested containers correctly:
    - If `retrieval` (inner) is expanded: INPUT goes inside `retrieval`
    - If `retrieval` is collapsed but `batch_recall` (outer) is expanded:
      INPUT goes inside `batch_recall`
    - If both are collapsed: INPUT goes at root

    Args:
        param: Parameter name
        flat_graph: The flattened graph
        expansion_state: Map of container_id -> is_expanded

    Returns:
        None - INPUT should be at root (consumers at root or in multiple containers)
        container_id - INPUT should be inside this container
    """
    # Get actual consumers (filtering out containers that pass through inputs)
    consumers = _get_deepest_consumers(param, flat_graph)

    if not consumers:
        return None

    # Get ancestor chains for all consumers
    ancestor_chains = [_get_ancestor_chain(c, flat_graph) for c in consumers]

    # Find the deepest common container
    deepest_owner = _find_deepest_common_container(ancestor_chains)

    if deepest_owner is None:
        return None

    # Walk up from the deepest owner to find the deepest EXPANDED container
    # The ancestor chain goes from deepest to root, so we check from start
    owner_chain = _get_ancestor_chain(deepest_owner, flat_graph)
    # Prepend the deepest owner itself (it might be expanded)
    candidates = [deepest_owner] + list(owner_chain)

    for container in candidates:
        if expansion_state.get(container, False):
            return container

    # No container in the chain is expanded, INPUT stays at root
    return None


def _compute_deepest_input_scope(
    param: str,
    flat_graph: nx.DiGraph,
) -> str | None:
    """Find the deepest common container of all consumers (ignoring expansion state).

    This is used for JavaScript to walk up at runtime when expansion state changes.

    Args:
        param: Parameter name
        flat_graph: The flattened graph

    Returns:
        The deepest container containing all consumers, or None if at root level.
    """
    consumers = _get_deepest_consumers(param, flat_graph)

    if not consumers:
        return None

    ancestor_chains = [_get_ancestor_chain(c, flat_graph) for c in consumers]
    return _find_deepest_common_container(ancestor_chains)


def _is_output_externally_consumed(
    output_param: str,
    source_node: str,
    flat_graph: nx.DiGraph,
) -> bool:
    """Check if an output is consumed by any node outside its source's container.

    An output is "externally consumed" if:
    1. Its source is at root level (always externally visible)
    2. Any consumer is at root level or in a different container

    Args:
        output_param: The output parameter name
        source_node: The node that produces this output
        flat_graph: The flattened graph

    Returns:
        True if output has consumers outside its container, False if internal-only
    """
    source_parent = _get_parent(source_node, flat_graph)
    source_attrs = flat_graph.nodes.get(source_node, {})

    # If source is at root level, output is always externally visible
    # for non-container nodes (final outputs should remain visible).
    if source_parent is None and source_attrs.get("node_type") != "GRAPH":
        return True

    # For containers, compare against the container's subtree.
    # For functions, compare against the parent container's subtree.
    source_container = source_node if source_attrs.get("node_type") == "GRAPH" else source_parent

    # Find all consumers of this output
    for node_id, attrs in flat_graph.nodes(data=True):
        if output_param in attrs.get("inputs", ()):
            # Any consumer outside the container's subtree is external
            if not _is_descendant_of(node_id, source_container, flat_graph):
                return True

    # All consumers are inside the same container subtree as source
    return False


def _build_graph_output_visibility(flat_graph: nx.DiGraph) -> dict[str, set[str]]:
    """Build mapping of GRAPH node -> externally consumed outputs."""
    visibility: dict[str, set[str]] = {}
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("node_type") != "GRAPH":
            continue
        visible_outputs = {
            output_name
            for output_name in attrs.get("outputs", ())
            if _is_output_externally_consumed(output_name, node_id, flat_graph)
        }
        visibility[node_id] = visible_outputs
    return visibility


def _group_inputs_by_consumers_and_bound(
    external_inputs: set[str],
    param_to_consumers: dict[str, list[str]],
    bound_params: set[str],
) -> dict[tuple[frozenset[str], bool], list[str]]:
    """Group input parameters by their consumers and bound status.

    Args:
        external_inputs: Set of external input parameter names
        param_to_consumers: Map of param -> list of consumer node IDs
        bound_params: Set of bound parameter names

    Returns:
        Dict mapping (frozenset of consumers, is_bound) -> list of params
    """
    groups: dict[tuple[frozenset[str], bool], list[str]] = {}
    for param in external_inputs:
        consumers = frozenset(param_to_consumers.get(param, []))
        is_bound = param in bound_params
        key = (consumers, is_bound)
        groups.setdefault(key, []).append(param)
    return groups


def _build_input_groups(
    input_spec: dict[str, Any],
    param_to_consumers: dict[str, list[str]],
    bound_params: set[str],
) -> list[dict[str, Any]]:
    """Build stable input groups for rendering and edge routing."""
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = set(required) | set(optional)

    groups = _group_inputs_by_consumers_and_bound(external_inputs, param_to_consumers, bound_params)

    group_specs: list[dict[str, Any]] = []
    for (_, is_bound), params in groups.items():
        group_specs.append({
            "params": sorted(params),
            "is_bound": is_bound,
        })

    # Deterministic ordering based on group id
    group_specs.sort(key=lambda g: "_".join(g["params"]))
    return group_specs


def _build_classic_input_groups(
    input_spec: dict[str, Any],
    bound_params: set[str],
) -> list[dict[str, Any]]:
    """Build single-parameter input groups (classic layout behavior)."""
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    params = sorted(set(required) | set(optional))
    return [
        {"params": [param], "is_bound": param in bound_params}
        for param in params
    ]


def _get_param_type(param: str, flat_graph: nx.DiGraph) -> type | None:
    """Find the type annotation for a parameter from the graph."""
    for node_id, attrs in flat_graph.nodes(data=True):
        if param in attrs.get("inputs", ()):
            param_type = attrs.get("input_types", {}).get(param)
            if param_type is not None:
                return param_type
    return None


def _get_param_targets(
    param: str,
    flat_graph: nx.DiGraph,
    param_to_consumers: dict[str, list[str]],
) -> list[str]:
    """Get the actual target nodes for a parameter."""
    actual_targets = param_to_consumers.get(param, [])
    if not actual_targets:
        # Fall back to root-level consumer
        for node_id, attrs in flat_graph.nodes(data=True):
            if param in attrs.get("inputs", ()):
                return [_get_root_ancestor(node_id, flat_graph)]
    return actual_targets


def _get_group_targets(
    params: list[str],
    flat_graph: nx.DiGraph,
    param_to_consumers: dict[str, list[str]],
) -> list[str]:
    """Get unique target nodes for a group of parameters."""
    targets: list[str] = []
    seen: set[str] = set()
    for param in params:
        for target in _get_param_targets(param, flat_graph, param_to_consumers):
            if target not in seen:
                seen.add(target)
                targets.append(target)
    return targets


def _create_input_nodes(
    nodes: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    input_spec: dict,
    bound_params: set[str],
    theme: str,
    show_types: bool,
    param_to_consumers: dict[str, list[str]],
    expansion_state: dict[str, bool],
    input_groups: list[dict[str, Any]] | None = None,
) -> None:
    """Create INPUT nodes for external input parameters, grouping where possible.

    INPUT nodes are grouped when they have:
    1. Exact same set of consumer nodes (destinations)
    2. Same bound status (both bound or both unbound)

    For single-param groups: creates individual INPUT node
    For multi-param groups: creates INPUT_GROUP node

    INPUT nodes are placed based on scope analysis:
    - If ALL consumers are inside a single expanded container, the INPUT is
      placed inside that container (with parentNode set)
    - Otherwise, the INPUT stays at root level
    """
    if input_groups is None:
        input_groups = _build_input_groups(input_spec, param_to_consumers, bound_params)

    # Create nodes for each group
    for group in input_groups:
        params = group["params"]
        is_bound = group["is_bound"]
        # Sort params for deterministic ordering
        if len(params) == 1:
            # Single param: create individual INPUT node (existing logic)
            param = params[0]
            input_node_id = f"input_{param}"
            param_type = _get_param_type(param, flat_graph)
            actual_targets = _get_group_targets([param], flat_graph, param_to_consumers)
            owner_container = _compute_input_scope(param, flat_graph, expansion_state)
            deepest_owner = _compute_deepest_input_scope(param, flat_graph)

            input_node: dict[str, Any] = {
                "id": input_node_id,
                "type": "custom",
                "position": {"x": 0, "y": 0},
                "data": {
                    "nodeType": "INPUT",
                    "label": param,
                    "typeHint": _format_type(param_type),
                    "isBound": is_bound,
                    "actualTargets": actual_targets,
                    "theme": theme,
                    "showTypes": show_types,
                    "ownerContainer": owner_container,
                    "deepestOwnerContainer": deepest_owner,
                },
                "sourcePosition": "bottom",
                "targetPosition": "top",
            }
            nodes.append(input_node)
        else:
            # Multiple params: create INPUT_GROUP node
            group_id = f"input_group_{'_'.join(params)}"
            param_types = [_format_type(_get_param_type(p, flat_graph)) for p in params]
            actual_targets = _get_group_targets(params, flat_graph, param_to_consumers)

            # Compute owner container - use the first param (all have same consumers)
            # All params in the group share the same consumers, so they share the same owner
            owner_container = _compute_input_scope(params[0], flat_graph, expansion_state)
            deepest_owner = _compute_deepest_input_scope(params[0], flat_graph)

            group_node: dict[str, Any] = {
                "id": group_id,
                "type": "custom",
                "position": {"x": 0, "y": 0},
                "data": {
                    "nodeType": "INPUT_GROUP",
                    "params": params,
                    "paramTypes": param_types,
                    "isBound": is_bound,
                    "actualTargets": actual_targets,
                    "ownerContainer": owner_container,
                    "deepestOwnerContainer": deepest_owner,
                    "theme": theme,
                    "showTypes": show_types,
                },
                "sourcePosition": "bottom",
                "targetPosition": "top",
            }
            nodes.append(group_node)
            # Edge routing uses param_to_consumer mappings, not node IDs here.


def _get_root_ancestor(node_id: str, flat_graph: nx.DiGraph) -> str:
    """Get the root-level ancestor of a node (or itself if root-level)."""
    attrs = flat_graph.nodes[node_id]
    parent_id = attrs.get("parent")

    if parent_id is None:
        return node_id

    # Walk up to find root
    while True:
        parent_attrs = flat_graph.nodes[parent_id]
        grandparent = parent_attrs.get("parent")
        if grandparent is None:
            return parent_id
        parent_id = grandparent


def _get_parent(node_id: str, flat_graph: nx.DiGraph) -> str | None:
    """Get the parent of a node."""
    if node_id not in flat_graph.nodes:
        return None
    return flat_graph.nodes[node_id].get("parent")


def _create_rf_node(
    node_id: str,
    attrs: dict[str, Any],
    node_type: str,
    is_expanded: bool | None,
    parent_id: str | None,
    bound_params: set[str],
    theme: str,
    show_types: bool,
    separate_outputs: bool,
) -> dict[str, Any]:
    """Create a React Flow node from graph attributes."""
    rf_node: dict[str, Any] = {
        "id": node_id,
        "type": "pipelineGroup" if node_type == "PIPELINE" and is_expanded else "custom",
        "position": {"x": 0, "y": 0},
        "data": {
            "nodeType": node_type,
            "label": attrs.get("label", node_id),
            "theme": theme,
            "showTypes": show_types,
            "separateOutputs": separate_outputs,
        },
        "sourcePosition": "bottom",
        "targetPosition": "top",
    }

    # Add parent reference for nested nodes
    if parent_id is not None:
        rf_node["parentNode"] = parent_id
        rf_node["extent"] = "parent"

    # Add expansion state for PIPELINE nodes
    if node_type == "PIPELINE":
        rf_node["data"]["isExpanded"] = is_expanded
        if is_expanded:
            rf_node["style"] = {"width": 600, "height": 400}

    # Add outputs (when not separate_outputs mode)
    if not separate_outputs and node_type in ("FUNCTION", "PIPELINE"):
        output_types = attrs.get("output_types", {})
        rf_node["data"]["outputs"] = [
            {"name": out, "type": _format_type(output_types.get(out))}
            for out in attrs.get("outputs", ())
        ]

    # Add inputs info
    input_types = attrs.get("input_types", {})
    has_defaults = attrs.get("has_defaults", {})
    rf_node["data"]["inputs"] = [
        {
            "name": param,
            "type": _format_type(input_types.get(param)),
            "has_default": has_defaults.get(param, False),
            "is_bound": param in bound_params,
        }
        for param in attrs.get("inputs", ())
    ]

    # Add branch-specific data
    branch_data = attrs.get("branch_data")
    if branch_data:
        if "when_true" in branch_data:
            rf_node["data"]["whenTrueTarget"] = branch_data["when_true"]
            rf_node["data"]["whenFalseTarget"] = branch_data["when_false"]
        if "targets" in branch_data:
            rf_node["data"]["targets"] = branch_data["targets"]

    return rf_node


def _create_data_nodes(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    theme: str,
    show_types: bool,
    graph_output_visibility: dict[str, set[str]] | None = None,
) -> None:
    """Create DATA nodes for all outputs.

    DATA nodes are marked with `internalOnly: true` if their output is not
    consumed by any nodes outside their container. This allows JavaScript
    to hide them when the container is collapsed.
    """
    for node_id, attrs in flat_graph.nodes(data=True):
        output_types = attrs.get("output_types", {})
        parent_id = attrs.get("parent")
        allowed_outputs = None
        if graph_output_visibility is not None and attrs.get("node_type") == "GRAPH":
            allowed_outputs = graph_output_visibility.get(node_id, set())

        for output_name in attrs.get("outputs", ()):
            if allowed_outputs is not None and output_name not in allowed_outputs:
                continue
            data_node_id = f"data_{node_id}_{output_name}"

            # Check if this output is consumed externally
            is_external = _is_output_externally_consumed(output_name, node_id, flat_graph)

            data_node = {
                "id": data_node_id,
                "type": "custom",
                "position": {"x": 0, "y": 0},
                "data": {
                    "nodeType": "DATA",
                    "label": output_name,
                    "typeHint": _format_type(output_types.get(output_name)),
                    "sourceId": node_id,
                    "theme": theme,
                    "showTypes": show_types,
                    "internalOnly": not is_external,  # True if no external consumers
                },
                "sourcePosition": "bottom",
                "targetPosition": "top",
            }

            # Add parent reference for nested nodes
            if parent_id is not None:
                data_node["parentNode"] = parent_id
                data_node["extent"] = "parent"

            nodes.append(data_node)

            # NOTE: Output edges (function → DATA) are now handled by pre-computed
            # edges in _add_separate_output_edges() when separate_outputs=True


# =============================================================================
# Pre-computed Edges for All Expansion States
# =============================================================================


def _get_expandable_nodes(flat_graph: nx.DiGraph) -> list[str]:
    """Get list of node IDs that can be expanded/collapsed (GRAPH nodes)."""
    return sorted([
        node_id
        for node_id, attrs in flat_graph.nodes(data=True)
        if attrs.get("node_type") == "GRAPH"
    ])


def _expansion_state_to_key(expansion_state: dict[str, bool]) -> str:
    """Convert expansion state dict to a canonical string key.

    Format: "node1:0,node2:1" (sorted alphabetically, 0=collapsed, 1=expanded)
    """
    sorted_items = sorted(expansion_state.items())
    return ",".join(f"{node_id}:{int(expanded)}" for node_id, expanded in sorted_items)


def _enumerate_valid_expansion_states(
    flat_graph: nx.DiGraph,
    expandable_nodes: list[str],
) -> list[dict[str, bool]]:
    """Enumerate all valid expansion state combinations.

    A state is valid if expanded children only appear when their parent is also expanded.
    This prunes unreachable states (e.g., inner expanded when outer collapsed).

    Returns:
        List of expansion state dicts, each mapping node_id -> is_expanded
    """
    if not expandable_nodes:
        return [{}]

    # Build parent-child relationships among expandable nodes
    node_to_parent: dict[str, str] = {}
    for node_id in expandable_nodes:
        parent_id = flat_graph.nodes[node_id].get("parent")
        if parent_id in expandable_nodes:
            node_to_parent[node_id] = parent_id

    valid_states = []

    # Generate all 2^n combinations
    for bits in product([False, True], repeat=len(expandable_nodes)):
        state = dict(zip(expandable_nodes, bits))

        # Check validity: if a node is expanded, all its expandable ancestors must be expanded
        is_valid = True
        for node_id, is_expanded in state.items():
            if is_expanded:
                # Check ancestor chain
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


def _compute_edges_for_state(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    input_spec: dict[str, Any],
    show_types: bool,
    theme: str,
    separate_outputs: bool = False,
    input_groups: list[dict[str, Any]] | None = None,
    graph_output_visibility: dict[str, set[str]] | None = None,
    input_consumer_mode: str = "all",
) -> list[dict[str, Any]]:
    """Compute edges for a specific expansion state.

    This is the core edge routing logic - determines which nodes edges connect to
    based on which containers are expanded/collapsed.

    Args:
        separate_outputs: If False (default), produces edges in "merged" format:
            - No edges TO DATA nodes
            - Edges go directly from source function to target function
            If True, produces edges that route through DATA nodes:
            - Edges from function TO DATA node (function → DATA)
            - Edges from DATA node TO consumers (DATA → consumer)
    """
    edges: list[dict[str, Any]] = []

    # Build param_to_consumers map for this expansion state
    param_to_consumers = _build_param_to_consumer_map(
        flat_graph,
        expansion_state,
        mode=input_consumer_mode,
    )

    bound_params = set(input_spec.get("bound", {}).keys())
    if input_groups is None:
        input_groups = _build_input_groups(input_spec, param_to_consumers, bound_params)

    # 1. Add edges from INPUT/INPUT_GROUP nodes to their consumers
    for group in input_groups:
        params = group["params"]
        actual_targets = _get_group_targets(params, flat_graph, param_to_consumers)
        if not actual_targets:
            continue

        if len(params) == 1:
            # Single param: use individual INPUT node
            param = params[0]
            input_node_id = f"input_{param}"
            for actual_target in actual_targets:
                edges.append({
                    "id": f"e_{input_node_id}_to_{actual_target}",
                    "source": input_node_id,
                    "target": actual_target,
                    "animated": False,
                    "style": {"stroke": "#64748b", "strokeWidth": 2},
                    "data": {"edgeType": "input"},
                })
        else:
            # Multiple params: use INPUT_GROUP node
            group_id = f"input_group_{'_'.join(params)}"
            for actual_target in actual_targets:
                edges.append({
                    "id": f"e_{group_id}_{actual_target}",
                    "source": group_id,
                    "target": actual_target,
                    "animated": False,
                    "style": {"stroke": "#64748b", "strokeWidth": 2},
                    "data": {"edgeType": "input"},
                })

    # 2. Add edges between function nodes (based on separate_outputs mode)
    if separate_outputs:
        _add_separate_output_edges(edges, flat_graph, expansion_state, graph_output_visibility)
    else:
        _add_merged_output_edges(edges, flat_graph, expansion_state)

    return edges


def _compute_nodes_for_state(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    input_spec: dict[str, Any],
    show_types: bool,
    theme: str,
    separate_outputs: bool = False,
    input_groups: list[dict[str, Any]] | None = None,
    input_consumer_mode: str = "all",
    graph_output_visibility: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    """Compute nodes for a specific expansion state."""
    nodes: list[dict[str, Any]] = []

    bound_params = set(input_spec.get("bound", {}).keys())
    param_to_consumer = _build_param_to_consumer_map(
        flat_graph,
        expansion_state,
        mode=input_consumer_mode,
    )
    if input_groups is None:
        input_groups = _build_input_groups(input_spec, param_to_consumer, bound_params)

    _create_input_nodes(
        nodes,
        flat_graph,
        input_spec,
        bound_params,
        theme,
        show_types,
        param_to_consumer,
        expansion_state,
        input_groups,
    )

    for node_id, attrs in flat_graph.nodes(data=True):
        parent_id = attrs.get("parent")
        node_type = attrs.get("node_type", "FUNCTION")
        rf_node_type = "PIPELINE" if node_type == "GRAPH" else node_type
        is_expanded = expansion_state.get(node_id, False)

        rf_node = _create_rf_node(
            node_id,
            attrs,
            rf_node_type,
            is_expanded,
            parent_id,
            bound_params,
            theme,
            show_types,
            separate_outputs,
        )

        if node_type == "GRAPH" and not separate_outputs:
            allowed_outputs = graph_output_visibility.get(node_id) if graph_output_visibility else None
            if allowed_outputs is not None and "outputs" in rf_node["data"]:
                rf_node["data"]["outputs"] = [
                    out for out in rf_node["data"]["outputs"]
                    if out["name"] in allowed_outputs
                ]

        nodes.append(rf_node)

    _create_data_nodes(nodes, [], flat_graph, theme, show_types, graph_output_visibility)

    for node in nodes:
        node.setdefault("data", {})["separateOutputs"] = separate_outputs

    _apply_node_visibility(nodes, expansion_state, separate_outputs)

    nodes.sort(key=lambda n: n["id"])
    return nodes


def _add_merged_output_edges(
    edges: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> None:
    """Add edges in merged output mode (separateOutputs=false).

    Edges go directly from source function to target function,
    skipping DATA nodes entirely.

    When a target is an expanded container, we re-route to the actual
    internal consumer node instead of the container boundary.
    """
    # Build param_to_consumer map to find actual internal consumers
    param_to_consumers = _build_param_to_consumer_map(flat_graph, expansion_state)

    # Build output_to_producer map to find actual internal producers
    output_to_producer = _build_output_to_producer_map(flat_graph, expansion_state, use_deepest=True)

    for source, target, edge_data in flat_graph.edges(data=True):
        # Skip if source is not visible in this expansion state
        if not _is_node_visible(source, flat_graph, expansion_state):
            continue

        edge_type = edge_data.get("edge_type", "data")
        value_name = edge_data.get("value_name", "")

        # Determine actual source - re-route if source is expanded container
        actual_source = source
        source_attrs = flat_graph.nodes.get(source, {})
        is_source_container = source_attrs.get("node_type") == "GRAPH"
        is_source_expanded = expansion_state.get(source, False)

        if is_source_container and is_source_expanded:
            if value_name:
                # Data edge: find the actual internal producer of this value
                internal_producer = output_to_producer.get(value_name)
                if internal_producer and internal_producer != source and _is_descendant_of(internal_producer, source, flat_graph):
                    # Found internal producer that's a descendant of the container
                    actual_source = internal_producer
                else:
                    # No direct match - this happens when with_outputs renames parameters
                    # (e.g., container exposes "retrieval_eval_results" but internal
                    # node produces "retrieval_eval_result")
                    # Use smart lookup to find the internal producer
                    internal_source = _find_internal_producer_for_output(
                        source, value_name, flat_graph, expansion_state
                    )
                    if internal_source:
                        actual_source = internal_source

        # Determine actual target - re-route if target is expanded container
        actual_target = target
        target_attrs = flat_graph.nodes.get(target, {})
        is_target_container = target_attrs.get("node_type") == "GRAPH"
        is_target_expanded = expansion_state.get(target, False)

        if is_target_container and is_target_expanded:
            if value_name:
                # Data edge: find the actual internal consumer of this value
                consumers = param_to_consumers.get(value_name, [])
                # Filter to consumers that are INSIDE this target container (descendants only)
                # Exclude the container itself - we want the actual internal consumer
                internal_consumers = [
                    c for c in consumers
                    if c != target and _is_descendant_of(c, target, flat_graph)
                ]
                if internal_consumers:
                    # Use the first internal consumer (there should typically be one)
                    actual_target = internal_consumers[0]
                else:
                    # No exact match - this happens when with_inputs renames parameters
                    # Fall back to entry points (nodes with no internal predecessors)
                    entry_points = _find_container_entry_points(
                        target, flat_graph, expansion_state
                    )
                    if entry_points:
                        actual_target = entry_points[0]
            elif edge_type == "control":
                # Control edge: route to entry point(s) of the container
                # Entry points are direct children with no internal predecessors
                entry_points = _find_container_entry_points(
                    target, flat_graph, expansion_state
                )
                if entry_points:
                    actual_target = entry_points[0]

        # Skip if actual source or target is not visible
        if not _is_node_visible(actual_source, flat_graph, expansion_state):
            continue
        if not _is_node_visible(actual_target, flat_graph, expansion_state):
            continue

        # Direct edge from actual source to actual target
        edge_id = f"e_{actual_source}_{actual_target}"
        if value_name:
            edge_id = f"e_{actual_source}_{value_name}_{actual_target}"

        rf_edge = {
            "id": edge_id,
            "source": actual_source,
            "target": actual_target,
            "animated": False,
            "style": {"stroke": "#64748b", "strokeWidth": 2},
            "data": {
                "edgeType": edge_type,
                "valueName": value_name,
            },
        }

        # Add label for IfElse branch edges (True/False)
        if edge_type == "control":
            original_source_attrs = flat_graph.nodes.get(source, {})
            branch_data = original_source_attrs.get("branch_data", {})
            if branch_data and "when_true" in branch_data:
                if target == branch_data["when_true"]:
                    rf_edge["data"]["label"] = "True"
                elif target == branch_data["when_false"]:
                    rf_edge["data"]["label"] = "False"

        edges.append(rf_edge)


def _add_separate_output_edges(
    edges: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    graph_output_visibility: dict[str, set[str]] | None = None,
) -> None:
    """Add edges in separate output mode (separateOutputs=true).

    Edges route through DATA nodes:
    - Function → DATA node (for each output)
    - DATA node → consumer functions

    When a container is EXPANDED:
    - Skip container→DATA edges (container DATA nodes are hidden)
    - Reroute container DATA→consumer edges through internal producer DATA nodes
    """
    # Build output_to_producer mapping to find deepest (internal) producers
    output_to_producer = _build_output_to_producer_map(flat_graph, expansion_state, use_deepest=True)

    # 1. Add edges from function nodes to their DATA nodes
    for node_id, attrs in flat_graph.nodes(data=True):
        if not _is_node_visible(node_id, flat_graph, expansion_state):
            continue

        # Skip containers (GRAPH nodes) that are expanded - their DATA nodes are hidden
        # (internal function DATA nodes are shown instead)
        is_container = attrs.get("node_type") == "GRAPH"
        is_expanded = expansion_state.get(node_id, False)
        if is_container and is_expanded:
            continue

        allowed_outputs = None
        if graph_output_visibility is not None and is_container:
            allowed_outputs = graph_output_visibility.get(node_id, set())

        for output_name in attrs.get("outputs", ()):
            if allowed_outputs is not None and output_name not in allowed_outputs:
                continue
            if not _is_data_node_visible(node_id, output_name, flat_graph, expansion_state):
                continue
            data_node_id = f"data_{node_id}_{output_name}"
            edges.append({
                "id": f"e_{node_id}_to_{data_node_id}",
                "source": node_id,
                "target": data_node_id,
                "animated": False,
                "style": {"stroke": "#64748b", "strokeWidth": 2},
                "data": {"edgeType": "output"},
            })

    # 2. Add edges from DATA nodes to consumer functions
    for source, target, edge_data in flat_graph.edges(data=True):
        # Skip if either node is not visible in this expansion state
        if not _is_node_visible(source, flat_graph, expansion_state):
            continue
        if not _is_node_visible(target, flat_graph, expansion_state):
            continue

        edge_type = edge_data.get("edge_type", "data")
        value_name = edge_data.get("value_name", "")

        # For data edges, route through the DATA node
        if edge_type == "data" and value_name:
            # Check if source is an expanded container (GRAPH node)
            source_attrs = flat_graph.nodes.get(source, {})
            is_source_container = source_attrs.get("node_type") == "GRAPH"
            is_source_expanded = expansion_state.get(source, False)
            if graph_output_visibility is not None and is_source_container:
                allowed_outputs = graph_output_visibility.get(source, set())
                if value_name not in allowed_outputs:
                    continue

            if is_source_container and is_source_expanded:
                # Reroute through internal producer's DATA node
                actual_producer = output_to_producer.get(value_name, source)
                data_value = value_name
                # Handle with_outputs renaming: if actual_producer is the container itself,
                # find the internal node that produces the terminal output
                if actual_producer == source:
                    internal_producer = _find_internal_producer_for_output(
                        source, value_name, flat_graph, expansion_state
                    )
                    if internal_producer:
                        actual_producer = internal_producer
                        # Find the internal output name (may differ due to renaming)
                        internal_outputs = flat_graph.nodes[actual_producer].get("outputs", ())
                        # Use fuzzy match to find internal output name
                        internal_value = value_name
                        for out in internal_outputs:
                            if out in value_name or value_name in out:
                                internal_value = out
                                break
                        data_value = internal_value
                data_source = actual_producer
                if not _is_data_node_visible(data_source, data_value, flat_graph, expansion_state):
                    continue
                data_node_id = f"data_{data_source}_{data_value}"
            else:
                if not _is_data_node_visible(source, value_name, flat_graph, expansion_state):
                    continue
                data_node_id = f"data_{source}_{value_name}"

            edge_id = f"e_{data_node_id}_to_{target}"

            edges.append({
                "id": edge_id,
                "source": data_node_id,
                "target": target,
                "animated": False,
                "style": {"stroke": "#64748b", "strokeWidth": 2},
                "data": {
                    "edgeType": "data",
                    "valueName": value_name,
                },
            })
        else:
            # Control edges go direct (not through DATA nodes)
            # But we still need to re-route if target is an expanded container
            actual_target = target
            if edge_type == "control":
                target_attrs = flat_graph.nodes.get(target, {})
                is_target_container = target_attrs.get("node_type") == "GRAPH"
                is_target_expanded = expansion_state.get(target, False)

                if is_target_container and is_target_expanded:
                    # Route to entry point(s) of the container
                    entry_points = _find_container_entry_points(
                        target, flat_graph, expansion_state
                    )
                    if entry_points:
                        actual_target = entry_points[0]

            edge_id = f"e_{source}_{actual_target}"
            if value_name:
                edge_id = f"e_{source}_{value_name}_{actual_target}"

            rf_edge = {
                "id": edge_id,
                "source": source,
                "target": actual_target,
                "animated": False,
                "style": {"stroke": "#64748b", "strokeWidth": 2},
                "data": {
                    "edgeType": edge_type,
                    "valueName": value_name,
                },
            }

            # Add label for IfElse branch edges (True/False)
            if edge_type == "control":
                source_attrs = flat_graph.nodes.get(source, {})
                branch_data = source_attrs.get("branch_data", {})
                if branch_data and "when_true" in branch_data:
                    if target == branch_data["when_true"]:
                        rf_edge["data"]["label"] = "True"
                    elif target == branch_data["when_false"]:
                        rf_edge["data"]["label"] = "False"

            edges.append(rf_edge)


def _precompute_all_edges(
    flat_graph: nx.DiGraph,
    input_spec: dict[str, Any],
    show_types: bool,
    theme: str,
    input_groups: list[dict[str, Any]] | None = None,
    graph_output_visibility: dict[str, set[str]] | None = None,
    input_consumer_mode: str = "all",
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Pre-compute edges for all valid expansion state combinations.

    Generates TWO edge sets per expansion state:
    - One with key ending in "|sep:0" for merged outputs mode
    - One with key ending in "|sep:1" for separate outputs mode

    For graphs without expandable containers, keys are just "sep:0" or "sep:1".

    Returns:
        Tuple of (edges_by_state dict, expandable_nodes list)
        edges_by_state maps "expansion_key|sep:X" -> list of edges
    """
    expandable_nodes = _get_expandable_nodes(flat_graph)

    if not expandable_nodes:
        # No expandable nodes - generate edges for both sep:0 and sep:1
        edges_merged = _compute_edges_for_state(
            flat_graph,
            {},
            input_spec,
            show_types,
            theme,
            separate_outputs=False,
            input_groups=input_groups,
            graph_output_visibility=graph_output_visibility,
            input_consumer_mode=input_consumer_mode,
        )
        edges_separate = _compute_edges_for_state(
            flat_graph,
            {},
            input_spec,
            show_types,
            theme,
            separate_outputs=True,
            input_groups=input_groups,
            graph_output_visibility=graph_output_visibility,
            input_consumer_mode=input_consumer_mode,
        )
        return {"sep:0": edges_merged, "sep:1": edges_separate}, []

    edges_by_state: dict[str, list[dict[str, Any]]] = {}
    valid_states = _enumerate_valid_expansion_states(flat_graph, expandable_nodes)

    for state in valid_states:
        exp_key = _expansion_state_to_key(state)

        # Generate edges for merged outputs mode (sep:0)
        key_merged = f"{exp_key}|sep:0"
        edges_merged = _compute_edges_for_state(
            flat_graph,
            state,
            input_spec,
            show_types,
            theme,
            separate_outputs=False,
            input_groups=input_groups,
            graph_output_visibility=graph_output_visibility,
            input_consumer_mode=input_consumer_mode,
        )
        edges_by_state[key_merged] = edges_merged

        # Generate edges for separate outputs mode (sep:1)
        key_separate = f"{exp_key}|sep:1"
        edges_separate = _compute_edges_for_state(
            flat_graph,
            state,
            input_spec,
            show_types,
            theme,
            separate_outputs=True,
            input_groups=input_groups,
            graph_output_visibility=graph_output_visibility,
            input_consumer_mode=input_consumer_mode,
        )
        edges_by_state[key_separate] = edges_separate

    return edges_by_state, expandable_nodes


def _precompute_all_nodes(
    flat_graph: nx.DiGraph,
    input_spec: dict[str, Any],
    show_types: bool,
    theme: str,
    graph_output_visibility: dict[str, set[str]] | None = None,
    input_groups: list[dict[str, Any]] | None = None,
    input_consumer_mode: str = "all",
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Pre-compute nodes for all valid expansion state combinations."""
    expandable_nodes = _get_expandable_nodes(flat_graph)

    if not expandable_nodes:
        nodes_merged = _compute_nodes_for_state(
            flat_graph,
            {},
            input_spec,
            show_types,
            theme,
            separate_outputs=False,
            graph_output_visibility=graph_output_visibility,
            input_groups=input_groups,
            input_consumer_mode=input_consumer_mode,
        )
        nodes_separate = _compute_nodes_for_state(
            flat_graph,
            {},
            input_spec,
            show_types,
            theme,
            separate_outputs=True,
            graph_output_visibility=graph_output_visibility,
            input_groups=input_groups,
            input_consumer_mode=input_consumer_mode,
        )
        return {"sep:0": nodes_merged, "sep:1": nodes_separate}, []

    nodes_by_state: dict[str, list[dict[str, Any]]] = {}
    valid_states = _enumerate_valid_expansion_states(flat_graph, expandable_nodes)

    for state in valid_states:
        exp_key = _expansion_state_to_key(state)

        key_merged = f"{exp_key}|sep:0"
        nodes_by_state[key_merged] = _compute_nodes_for_state(
            flat_graph,
            state,
            input_spec,
            show_types,
            theme,
            separate_outputs=False,
            graph_output_visibility=graph_output_visibility,
            input_groups=input_groups,
            input_consumer_mode=input_consumer_mode,
        )

        key_separate = f"{exp_key}|sep:1"
        nodes_by_state[key_separate] = _compute_nodes_for_state(
            flat_graph,
            state,
            input_spec,
            show_types,
            theme,
            separate_outputs=True,
            graph_output_visibility=graph_output_visibility,
            input_groups=input_groups,
            input_consumer_mode=input_consumer_mode,
        )

    return nodes_by_state, expandable_nodes
