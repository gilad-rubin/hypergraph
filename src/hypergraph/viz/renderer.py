"""Render NetworkX graph to React Flow JSON format.

This module transforms a flattened NetworkX DiGraph into the React Flow
node/edge format expected by the visualization.
"""

from __future__ import annotations

from typing import Any

import networkx as nx


def _format_type(t: type | None) -> str | None:
    """Format a type annotation for display."""
    if t is None:
        return None
    if hasattr(t, "__name__"):
        return t.__name__
    return str(t).replace("typing.", "")


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


def _build_param_to_consumer_map(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    use_deepest: bool = False,
) -> dict[str, str]:
    """Build map of param_name -> actual_consumer_node_id.

    Args:
        use_deepest: If True, include all consumers (for JS interactive routing).
                     If False, only include visible consumers (for static edges).
    """
    param_to_consumer: dict[str, str] = {}

    for node_id, attrs in flat_graph.nodes(data=True):
        for param in attrs.get("inputs", ()):
            # Check visibility unless we want deepest
            if not use_deepest and not _is_node_visible(node_id, flat_graph, expansion_state):
                continue

            if param not in param_to_consumer:
                param_to_consumer[param] = node_id
            else:
                # Prefer the deeper (more specific) consumer
                existing = param_to_consumer[param]
                if _get_nesting_depth(node_id, flat_graph) > _get_nesting_depth(existing, flat_graph):
                    param_to_consumer[param] = node_id

    return param_to_consumer


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


def _create_input_nodes(
    nodes: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    input_spec: dict,
    bound_params: set[str],
    theme: str,
    show_types: bool,
    param_to_consumer: dict[str, str],
    expansion_state: dict[str, bool],
) -> dict[str, str]:
    """Create individual INPUT nodes for each external input parameter.

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

        # Get actual target for edge routing
        actual_target = param_to_consumer.get(param)
        if actual_target is None:
            # Fall back to root-level consumer
            for node_id, attrs in flat_graph.nodes(data=True):
                if param in attrs.get("inputs", ()):
                    actual_target = _get_root_ancestor(node_id, flat_graph)
                    break

        nodes.append({
            "id": input_node_id,
            "type": "custom",
            "position": {"x": 0, "y": 0},
            "data": {
                "nodeType": "INPUT",
                "label": param,
                "typeHint": _format_type(param_type),
                "isBound": is_bound,
                "actualTarget": actual_target,  # Explicit target for edge
                "theme": theme,
                "showTypes": show_types,
            },
            "sourcePosition": "bottom",
            "targetPosition": "top",
        })

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
    """Create DATA nodes for all outputs."""
    for node_id, attrs in flat_graph.nodes(data=True):
        output_types = attrs.get("output_types", {})
        parent_id = attrs.get("parent")

        for output_name in attrs.get("outputs", ()):
            data_node_id = f"data_{node_id}_{output_name}"
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

    Each INPUT node connects directly to its actual consumer node.
    The target is explicitly computed based on expansion state.
    """
    for node in nodes:
        if node.get("data", {}).get("nodeType") != "INPUT":
            continue

        input_node_id = node["id"]
        actual_target = node["data"].get("actualTarget")

        if actual_target:
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
