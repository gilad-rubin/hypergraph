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
    debug_overlays: bool = False,
) -> dict[str, Any]:
    """Convert a flattened NetworkX graph to React Flow JSON format.

    Args:
        flat_graph: NetworkX DiGraph from Graph.to_flat_graph()
        depth: How many levels of nested graphs to expand (0 = collapsed)
        theme: "dark", "light", or "auto" (detect from environment)
        show_types: Whether to show type annotations
        separate_outputs: Whether to render outputs as separate DATA nodes
        debug_overlays: Whether to enable debug overlays (internal use)

    Returns:
        Dict with "nodes", "edges", and "meta" keys ready for React Flow
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # Get input_spec from graph attributes
    input_spec = flat_graph.graph.get("input_spec", {})
    bound_params = set(input_spec.get("bound", {}).keys())

    # Build output_to_source mapping from node attributes
    output_to_source: dict[str, str] = {}
    for node_id, attrs in flat_graph.nodes(data=True):
        for output in attrs.get("outputs", ()):
            output_to_source[output] = node_id

    # Build maps for routing edges to actual internal nodes when expanded
    expansion_state = _build_expansion_state(flat_graph, depth)
    # For static edges: use visibility-based targets
    param_to_consumer = _build_param_to_consumer_map(flat_graph, expansion_state)
    output_to_producer = _build_output_to_producer_map(flat_graph, expansion_state)
    # For JS meta data: use deepest targets (for interactive expand routing)
    param_to_consumer_deepest = _build_param_to_consumer_map(flat_graph, expansion_state, use_deepest=True)
    output_to_producer_deepest = _build_output_to_producer_map(flat_graph, expansion_state, use_deepest=True)
    # For JS meta data: node-to-parent map for routing
    node_to_parent = _build_node_to_parent_map(flat_graph)

    # Create individual INPUT nodes for external inputs
    input_node_map = _create_input_nodes(
        nodes, flat_graph, input_spec, bound_params, theme, show_types,
        param_to_consumer, expansion_state
    )

    # Process each node
    for node_id, attrs in flat_graph.nodes(data=True):
        parent_id = attrs.get("parent")
        node_type = attrs.get("node_type", "FUNCTION")

        # Map node_type for React Flow (GRAPH -> PIPELINE for backwards compat)
        rf_node_type = "PIPELINE" if node_type == "GRAPH" else node_type
        is_expanded = expansion_state.get(node_id, False)

        rf_node = _create_rf_node(
            node_id, attrs, rf_node_type, is_expanded, parent_id,
            bound_params, theme, show_types, separate_outputs
        )
        nodes.append(rf_node)

    # Create DATA nodes for outputs
    _create_data_nodes(nodes, edges, flat_graph, theme, show_types)

    # Create edges from INPUT nodes to their actual targets
    _create_input_edges(nodes, edges, input_node_map)

    # Create edges from graph structure
    _create_graph_edges(edges, flat_graph, input_spec, expansion_state, output_to_producer)

    # Sort nodes and edges by ID for deterministic ordering (prevents layout flickering)
    nodes.sort(key=lambda n: n["id"])
    edges.sort(key=lambda e: e["id"])

    # Pre-compute edges for ALL valid expansion state combinations
    # JavaScript can select the correct edge set based on current expansion state
    edges_by_state, expandable_nodes = _precompute_all_edges(
        flat_graph, input_spec, show_types, theme
    )

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "theme_preference": theme,
            "initial_depth": depth,
            "separate_outputs": separate_outputs,
            "show_types": show_types,
            "debug_overlays": debug_overlays,
            # Routing data for JS to re-route edges to actual internal nodes
            # Use deepest targets so interactive expand can route correctly
            "output_to_producer": output_to_producer_deepest,
            "param_to_consumer": param_to_consumer_deepest,
            "node_to_parent": node_to_parent,
            # Pre-computed edges for all expansion states (collapse/expand consistency)
            "edgesByState": edges_by_state,
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

    An INPUT should be placed inside a container if:
    1. ALL its actual (non-container) consumers are inside that container
    2. The container is expanded (so the INPUT would be visible)

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
    owner_container = _find_deepest_common_container(ancestor_chains)

    if owner_container is None:
        return None

    # Only assign ownership if the container is expanded
    # (if collapsed, the INPUT should stay at root to be visible)
    if not expansion_state.get(owner_container, False):
        return None

    return owner_container


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

    # If source is at root level, output is always externally visible
    if source_parent is None:
        return True

    # Find all consumers of this output
    for node_id, attrs in flat_graph.nodes(data=True):
        if output_param in attrs.get("inputs", ()):
            consumer_parent = _get_parent(node_id, flat_graph)

            # If consumer is at root level, it's external
            if consumer_parent is None:
                return True

            # If consumer is in a different container, it's external
            if consumer_parent != source_parent:
                return True

    # All consumers are in the same container as source
    return False


def _create_input_nodes(
    nodes: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    input_spec: dict,
    bound_params: set[str],
    theme: str,
    show_types: bool,
    param_to_consumers: dict[str, list[str]],
    expansion_state: dict[str, bool],
) -> dict[str, str]:
    """Create individual INPUT nodes for each external input parameter.

    INPUT nodes are placed based on scope analysis:
    - If ALL consumers are inside a single expanded container, the INPUT is
      placed inside that container (with parentNode set)
    - Otherwise, the INPUT stays at root level

    Returns:
        Dict mapping param_name -> input_node_id for edge creation.
    """
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = list(required) + list(optional)

    input_node_map: dict[str, str] = {}

    for param in external_inputs:
        input_node_id = f"input_{param}"
        is_bound = param in bound_params

        # Find the type for this parameter
        param_type = None
        for node_id, attrs in flat_graph.nodes(data=True):
            if param in attrs.get("inputs", ()):
                param_type = attrs.get("input_types", {}).get(param)
                if param_type is not None:
                    break

        # Get all targets for edge routing (multiple consumers supported)
        actual_targets = param_to_consumers.get(param, [])
        if not actual_targets:
            # Fall back to root-level consumer
            for node_id, attrs in flat_graph.nodes(data=True):
                if param in attrs.get("inputs", ()):
                    actual_targets = [_get_root_ancestor(node_id, flat_graph)]
                    break

        # Compute scope: which container (if any) should own this INPUT
        # Note: ownerContainer is used for layout positioning hints and edge routing,
        # NOT for parent-child visibility. INPUTs always stay at root level but
        # can be positioned inside expanded containers by the layout algorithm.
        owner_container = _compute_input_scope(param, flat_graph, expansion_state)

        input_node: dict[str, Any] = {
            "id": input_node_id,
            "type": "custom",
            "position": {"x": 0, "y": 0},
            "data": {
                "nodeType": "INPUT",
                "label": param,
                "typeHint": _format_type(param_type),
                "isBound": is_bound,
                "actualTargets": actual_targets,  # List of targets for edges
                "theme": theme,
                "showTypes": show_types,
                "ownerContainer": owner_container,  # For layout positioning hints
            },
            "sourcePosition": "bottom",
            "targetPosition": "top",
        }

        # Note: We do NOT set parentNode on INPUT nodes. They stay at root level
        # and are positioned via layout. This ensures they remain visible when
        # their owner container is collapsed (edge routes to container instead).

        nodes.append(input_node)
        input_node_map[param] = input_node_id

    return input_node_map


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


def _lift_to_sibling_level(
    node_id: str,
    target_parent: str | None,
    flat_graph: nx.DiGraph,
) -> str:
    """Lift a node to be a sibling of target_parent's children.

    If node_id is deeper nested than target_parent's level, returns the
    ancestor that shares target_parent as its parent.
    If node_id is already at the right level, returns node_id.
    """
    current = node_id
    current_parent = _get_parent(current, flat_graph)

    # Walk up until we find an ancestor whose parent is target_parent
    while current_parent != target_parent:
        if current_parent is None:
            # Reached root level, can't lift further
            return current
        current = current_parent
        current_parent = _get_parent(current, flat_graph)

    return current


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
) -> None:
    """Create DATA nodes for all outputs.

    DATA nodes are marked with `internalOnly: true` if their output is not
    consumed by any nodes outside their container. This allows JavaScript
    to hide them when the container is collapsed.
    """
    for node_id, attrs in flat_graph.nodes(data=True):
        output_types = attrs.get("output_types", {})
        parent_id = attrs.get("parent")

        for output_name in attrs.get("outputs", ()):
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

            # Edge from function to DATA node
            edges.append({
                "id": f"e_{node_id}_to_{data_node_id}",
                "source": node_id,
                "target": data_node_id,
                "animated": False,
                "style": {"stroke": "#64748b", "strokeWidth": 2},
                "data": {"edgeType": "output"},
            })


def _create_input_edges(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    input_node_map: dict[str, str],
) -> None:
    """Create edges from INPUT nodes to their actual targets.

    Each INPUT node connects to ALL its consumer nodes.
    The targets are explicitly computed based on expansion state.
    """
    for node in nodes:
        if node.get("data", {}).get("nodeType") != "INPUT":
            continue

        input_node_id = node["id"]
        actual_targets = node["data"].get("actualTargets", [])

        for actual_target in actual_targets:
            edges.append({
                "id": f"e_{input_node_id}_to_{actual_target}",
                "source": input_node_id,
                "target": actual_target,
                "animated": False,
                "style": {"stroke": "#64748b", "strokeWidth": 2},
                "data": {"edgeType": "input"},
            })


def _find_common_ancestor(
    node_a: str,
    node_b: str,
    flat_graph: nx.DiGraph,
) -> str | None:
    """Find the lowest common ancestor of two nodes.

    Returns None if the common ancestor is the implicit root (both at top level).
    """
    # Get all ancestors of node_a (including itself)
    ancestors_a = set()
    current = node_a
    while current is not None:
        ancestors_a.add(current)
        current = _get_parent(current, flat_graph)

    # Walk up from node_b until we find a common ancestor
    current = node_b
    while current is not None:
        if current in ancestors_a:
            # Found common ancestor, but return its parent (the level where both are siblings)
            return _get_parent(current, flat_graph)
        parent = _get_parent(current, flat_graph)
        if parent in ancestors_a:
            return parent
        current = parent

    # Both are at root level
    return None


def _create_graph_edges(
    edges: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    input_spec: dict,
    expansion_state: dict[str, bool],
    output_to_producer: dict[str, str],
) -> None:
    """Create edges from graph structure, routing through DATA nodes.

    Edges are routed between nodes at the same nesting level for layout
    compatibility. If source and target have different parents, both are
    lifted to share a common parent level.
    """
    for source, target, edge_data in flat_graph.edges(data=True):
        edge_type = edge_data.get("edge_type", "data")
        value_name = edge_data.get("value_name", "")

        # Use the original source and target from the flat graph
        # These are already at appropriate container levels
        actual_source_node = source
        actual_target = target

        # For data edges, route through the DATA node of the source
        if edge_type == "data" and value_name:
            edge_id = f"e_data_{actual_source_node}_{value_name}_to_{actual_target}"
            actual_source = f"data_{actual_source_node}_{value_name}"
        else:
            edge_id = f"e_{actual_source_node}_{actual_target}_{value_name}"
            actual_source = actual_source_node

        rf_edge: dict[str, Any] = {
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

        # Add label for IfElse branch edges
        if edge_type == "control":
            source_attrs = flat_graph.nodes.get(source, {})
            branch_data = source_attrs.get("branch_data", {})
            if branch_data and "when_true" in branch_data:
                if target == branch_data["when_true"]:
                    rf_edge["data"]["label"] = "True"
                elif target == branch_data["when_false"]:
                    rf_edge["data"]["label"] = "False"

        edges.append(rf_edge)


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
    param_to_consumers = _build_param_to_consumer_map(flat_graph, expansion_state)

    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = set(required) | set(optional)

    # 1. Add edges from INPUT nodes to ALL their actual consumers
    for param in external_inputs:
        input_node_id = f"input_{param}"
        actual_targets = param_to_consumers.get(param, [])

        for actual_target in actual_targets:
            edges.append({
                "id": f"e_{input_node_id}_to_{actual_target}",
                "source": input_node_id,
                "target": actual_target,
                "animated": False,
                "style": {"stroke": "#64748b", "strokeWidth": 2},
                "data": {"edgeType": "input"},
            })

    # 2. Add edges between function nodes (based on separate_outputs mode)
    if separate_outputs:
        _add_separate_output_edges(edges, flat_graph, expansion_state)
    else:
        _add_merged_output_edges(edges, flat_graph, expansion_state)

    return edges


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

    for source, target, edge_data in flat_graph.edges(data=True):
        # Skip if source is not visible in this expansion state
        if not _is_node_visible(source, flat_graph, expansion_state):
            continue

        edge_type = edge_data.get("edge_type", "data")
        value_name = edge_data.get("value_name", "")

        # Determine actual target - re-route if target is expanded container
        actual_target = target
        target_attrs = flat_graph.nodes.get(target, {})
        is_target_container = target_attrs.get("node_type") == "GRAPH"
        is_target_expanded = expansion_state.get(target, False)

        if is_target_container and is_target_expanded:
            if value_name:
                # Data edge: find the actual internal consumer of this value
                consumers = param_to_consumers.get(value_name, [])
                # Filter to consumers that are inside this target container
                internal_consumers = [
                    c for c in consumers
                    if _is_descendant_of(c, target, flat_graph) or c == target
                ]
                if internal_consumers:
                    # Use the first internal consumer (there should typically be one)
                    actual_target = internal_consumers[0]
            elif edge_type == "control":
                # Control edge: route to entry point(s) of the container
                # Entry points are direct children with no internal predecessors
                entry_points = _find_container_entry_points(
                    target, flat_graph, expansion_state
                )
                if entry_points:
                    actual_target = entry_points[0]

        # Skip if actual target is not visible
        if not _is_node_visible(actual_target, flat_graph, expansion_state):
            continue

        # Direct edge from source to actual target
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


def _add_separate_output_edges(
    edges: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
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

        for output_name in attrs.get("outputs", ()):
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

            if is_source_container and is_source_expanded:
                # Reroute through internal producer's DATA node
                actual_producer = output_to_producer.get(value_name, source)
                data_node_id = f"data_{actual_producer}_{value_name}"
            else:
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
            flat_graph, {}, input_spec, show_types, theme, separate_outputs=False
        )
        edges_separate = _compute_edges_for_state(
            flat_graph, {}, input_spec, show_types, theme, separate_outputs=True
        )
        return {"sep:0": edges_merged, "sep:1": edges_separate}, []

    edges_by_state: dict[str, list[dict[str, Any]]] = {}
    valid_states = _enumerate_valid_expansion_states(flat_graph, expandable_nodes)

    for state in valid_states:
        exp_key = _expansion_state_to_key(state)

        # Generate edges for merged outputs mode (sep:0)
        key_merged = f"{exp_key}|sep:0"
        edges_merged = _compute_edges_for_state(
            flat_graph, state, input_spec, show_types, theme, separate_outputs=False
        )
        edges_by_state[key_merged] = edges_merged

        # Generate edges for separate outputs mode (sep:1)
        key_separate = f"{exp_key}|sep:1"
        edges_separate = _compute_edges_for_state(
            flat_graph, state, input_spec, show_types, theme, separate_outputs=True
        )
        edges_by_state[key_separate] = edges_separate

    return edges_by_state, expandable_nodes
