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

    # Create INPUT_GROUP nodes for external inputs
    _create_input_groups(nodes, flat_graph, input_spec, bound_params, theme, show_types)

    # Process each node
    for node_id, attrs in flat_graph.nodes(data=True):
        parent_id = attrs.get("parent")
        node_type = attrs.get("node_type", "FUNCTION")

        # Map node_type for React Flow (GRAPH -> PIPELINE for backwards compat)
        rf_node_type = "PIPELINE" if node_type == "GRAPH" else node_type
        is_expanded = _is_node_expanded(node_id, parent_id, depth, flat_graph)

        rf_node = _create_rf_node(
            node_id, attrs, rf_node_type, is_expanded, parent_id,
            bound_params, theme, show_types, separate_outputs
        )
        nodes.append(rf_node)

    # Create DATA nodes for outputs
    _create_data_nodes(nodes, edges, flat_graph, theme, show_types)

    # Create edges from INPUT_GROUP nodes
    _create_input_edges(nodes, edges)

    # Create edges from graph structure
    _create_graph_edges(edges, flat_graph, input_spec)

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "theme_preference": theme,
            "initial_depth": depth,
            "separate_outputs": separate_outputs,
            "show_types": show_types,
            "debug_overlays": debug_overlays,
        },
    }


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


def _create_input_groups(
    nodes: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    input_spec: dict,
    bound_params: set[str],
    theme: str,
    show_types: bool,
) -> None:
    """Create INPUT_GROUP nodes for external inputs."""
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = list(required) + list(optional)

    if not external_inputs:
        return

    # Build param -> (targets, type, is_bound) mapping
    # Only consider root-level nodes (parent=None)
    param_info: dict[str, tuple[frozenset[str], type | None, bool]] = {}
    for param in external_inputs:
        targets = set()
        param_type = None
        is_bound = param in bound_params

        for node_id, attrs in flat_graph.nodes(data=True):
            if attrs.get("parent") is not None:
                continue  # Skip nested nodes
            if param in attrs.get("inputs", ()):
                targets.add(node_id)
                if param_type is None:
                    param_type = attrs.get("input_types", {}).get(param)

        param_info[param] = (frozenset(targets), param_type, is_bound)

    # Group params by (targets, is_bound)
    groups: dict[tuple[frozenset[str], bool], list[tuple[str, type | None]]] = {}
    for param, (targets, param_type, is_bound) in param_info.items():
        key = (targets, is_bound)
        groups.setdefault(key, []).append((param, param_type))

    # Create INPUT_GROUP node for each group
    for idx, ((targets, is_bound), params) in enumerate(groups.items()):
        group_id = f"__inputs_{idx}__" if len(groups) > 1 else "__inputs__"
        nodes.append({
            "id": group_id,
            "type": "custom",
            "position": {"x": 0, "y": 0},
            "data": {
                "nodeType": "INPUT_GROUP",
                "label": "Inputs" if not is_bound else "Bound",
                "params": [p[0] for p in params],
                "paramTypes": [_format_type(p[1]) for p in params],
                "isBound": is_bound,
                "targets": list(targets),
                "theme": theme,
                "showTypes": show_types,
            },
            "sourcePosition": "bottom",
            "targetPosition": "top",
        })


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
) -> None:
    """Create edges from INPUT_GROUP nodes to their targets."""
    for node in nodes:
        if node.get("data", {}).get("nodeType") != "INPUT_GROUP":
            continue

        group_id = node["id"]
        targets = node["data"].get("targets", [])
        params = node["data"].get("params", [])

        for target in targets:
            edges.append({
                "id": f"e_{group_id}_to_{target}",
                "source": group_id,
                "target": target,
                "animated": False,
                "style": {"stroke": "#64748b", "strokeWidth": 2},
                "data": {"edgeType": "input", "params": params},
            })


def _create_graph_edges(
    edges: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    input_spec: dict,
) -> None:
    """Create edges from graph structure, routing through DATA nodes."""
    for source, target, edge_data in flat_graph.edges(data=True):
        edge_type = edge_data.get("edge_type", "data")
        value_name = edge_data.get("value_name", "")

        # Data edges go from DATA node to target
        if edge_type == "data" and value_name:
            edge_id = f"e_data_{source}_{value_name}_to_{target}"
            actual_source = f"data_{source}_{value_name}"
        else:
            edge_id = f"e_{source}_{target}_{value_name}"
            actual_source = source

        rf_edge: dict[str, Any] = {
            "id": edge_id,
            "source": actual_source,
            "target": target,
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
