"""Render hypergraph Graph to React Flow JSON format.

This module transforms a hypergraph Graph into the React Flow node/edge
format expected by the visualization. Layout is performed client-side
using ELK (Eclipse Layout Kernel).
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
    # Handle generic types like list[str], dict[str, int]
    return str(t).replace("typing.", "")


def render_graph(
    viz_graph: nx.DiGraph,
    *,
    depth: int = 1,
    theme: str = "auto",
    show_types: bool = False,
    separate_outputs: bool = False,
) -> dict[str, Any]:
    """Convert a NetworkX visualization graph to React Flow JSON format.

    Args:
        viz_graph: NetworkX DiGraph from Graph.to_viz_graph()
        depth: How many levels of nested graphs to expand (0 = collapsed)
        theme: "dark", "light", or "auto" (detect from environment)
        show_types: Whether to show type annotations
        separate_outputs: Whether to render outputs as separate DATA nodes

    Returns:
        Dict with "nodes", "edges", and "meta" keys ready for React Flow
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # Get input spec from graph attributes
    input_spec = viz_graph.graph.get("input_spec", {})
    bound_params = set(input_spec.get("bound", {}).keys())

    # Track which outputs are produced by which nodes (for DATA node sourceId)
    output_to_source: dict[str, str] = {}
    for node_id, node_attrs in viz_graph.nodes(data=True):
        for output in node_attrs.get("outputs", ()):
            output_to_source[output] = node_id

    # Create INPUT_GROUP nodes for external inputs, grouped by (targets, is_bound)
    # Inputs targeting the same node(s) with the same bound state get grouped together
    required_inputs = input_spec.get("required", ())
    optional_inputs = input_spec.get("optional", ())
    external_inputs = list(required_inputs) + list(optional_inputs)
    if external_inputs:
        # Build mapping: param -> (targets, type, is_bound)
        param_info: dict[str, tuple[frozenset[str], type | None, bool]] = {}
        for param in external_inputs:
            targets = set()
            param_type = None
            is_bound = param in bound_params
            # Find all nodes that consume this parameter
            for node_id, node_attrs in viz_graph.nodes(data=True):
                node_inputs = node_attrs.get("inputs", ())
                if param in node_inputs:
                    targets.add(node_id)
                    if param_type is None:
                        input_types = node_attrs.get("input_types", {})
                        param_type = input_types.get(param)
            param_info[param] = (frozenset(targets), param_type, is_bound)

        # Group params by (targets, is_bound)
        groups: dict[tuple[frozenset[str], bool], list[tuple[str, type | None]]] = {}
        for param, (targets, param_type, is_bound) in param_info.items():
            key = (targets, is_bound)
            if key not in groups:
                groups[key] = []
            groups[key].append((param, param_type))

        # Create an INPUT_GROUP node for each group
        for idx, ((targets, is_bound), params) in enumerate(groups.items()):
            group_id = f"__inputs_{idx}__" if len(groups) > 1 else "__inputs__"
            param_names = [p[0] for p in params]
            param_types_list = [_format_type(p[1]) for p in params]

            input_group_node = {
                "id": group_id,
                "type": "custom",
                "position": {"x": 0, "y": 0},
                "data": {
                    "nodeType": "INPUT_GROUP",
                    "label": "Inputs" if not is_bound else "Bound",
                    "params": param_names,
                    "paramTypes": param_types_list,
                    "isBound": is_bound,
                    "targets": list(targets),  # Store targets for edge creation
                    "theme": theme,
                    "showTypes": show_types,
                },
                "sourcePosition": "bottom",
                "targetPosition": "top",
            }
            nodes.append(input_group_node)

    # Process each node in the graph
    for node_id, node_attrs in viz_graph.nodes(data=True):
        node_type = node_attrs.get("node_type", "FUNCTION")
        parent_id = node_attrs.get("parent")
        is_expanded = depth > 0 if node_type == "PIPELINE" else None

        rf_node: dict[str, Any] = {
            "id": node_id,
            "type": "pipelineGroup" if node_type == "PIPELINE" and is_expanded else "custom",
            "position": {"x": 0, "y": 0},  # ELK will calculate
            "data": {
                "nodeType": node_type,
                "label": node_attrs.get("label", node_id),
                "theme": theme,
                "showTypes": show_types,
                "separateOutputs": separate_outputs,
            },
            "sourcePosition": "bottom",
            "targetPosition": "top",
        }

        # Add parent reference if this is a nested node
        if parent_id is not None:
            rf_node["parentNode"] = parent_id
            rf_node["extent"] = "parent"

        # Add expansion state for pipelines
        if node_type == "PIPELINE":
            rf_node["data"]["isExpanded"] = is_expanded
            if is_expanded:
                rf_node["style"] = {"width": 600, "height": 400}

        # Add outputs for function/pipeline nodes (when not separate_outputs mode)
        if not separate_outputs and node_type in ("FUNCTION", "PIPELINE"):
            outputs = []
            node_outputs = node_attrs.get("outputs", ())
            output_types = node_attrs.get("output_types", {})
            for output_name in node_outputs:
                output_type = output_types.get(output_name)
                outputs.append({
                    "name": output_name,
                    "type": _format_type(output_type),
                })
            rf_node["data"]["outputs"] = outputs

        # Add inputs info for bound input badge
        inputs = []
        node_inputs = node_attrs.get("inputs", ())
        input_types = node_attrs.get("input_types", {})
        has_defaults = node_attrs.get("has_defaults", {})
        for param in node_inputs:
            input_type = input_types.get(param)
            has_default = has_defaults.get(param, False)
            is_bound = param in bound_params
            inputs.append({
                "name": param,
                "type": _format_type(input_type),
                "has_default": has_default,
                "is_bound": is_bound,
            })
        rf_node["data"]["inputs"] = inputs

        # Add branch-specific data
        branch_data = node_attrs.get("branch_data", {})
        if node_type == "BRANCH":
            # Check if this is an IfElse node (has whenTrue/whenFalse)
            if "when_true" in branch_data:
                rf_node["data"]["whenTrueTarget"] = branch_data["when_true"]
                rf_node["data"]["whenFalseTarget"] = branch_data["when_false"]
            # Check if this is a Route node (has targets)
            if "targets" in branch_data:
                rf_node["data"]["targets"] = branch_data["targets"]

        nodes.append(rf_node)

    # Always create DATA nodes for all outputs (visibility controlled by JS)
    for node_id, node_attrs in viz_graph.nodes(data=True):
        node_outputs = node_attrs.get("outputs", ())
        output_types = node_attrs.get("output_types", {})
        for output_name in node_outputs:
            output_type = output_types.get(output_name)
            data_node = {
                "id": f"data_{node_id}_{output_name}",
                "type": "custom",
                "position": {"x": 0, "y": 0},
                "data": {
                    "nodeType": "DATA",
                    "label": output_name,
                    "typeHint": _format_type(output_type),
                    "sourceId": node_id,  # Link back to source node
                    "theme": theme,
                    "showTypes": show_types,
                },
                "sourcePosition": "bottom",
                "targetPosition": "top",
            }
            nodes.append(data_node)

            # Edge from function to its DATA output node
            edges.append({
                "id": f"e_{node_id}_to_{data_node['id']}",
                "source": node_id,
                "target": data_node["id"],
                "animated": False,
                "style": {"stroke": "#64748b", "strokeWidth": 2},
                "data": {"edgeType": "output"},  # Mark as output edge
            })

    # Create edges from INPUT_GROUP nodes to their target nodes
    # Each INPUT_GROUP connects to its specific targets (stored in node data)
    for node in nodes:
        if node.get("data", {}).get("nodeType") == "INPUT_GROUP":
            group_id = node["id"]
            targets = node["data"].get("targets", [])
            params = node["data"].get("params", [])
            for target in targets:
                # Create one edge per target (the edge represents all params going to this target)
                edges.append({
                    "id": f"e_{group_id}_to_{target}",
                    "source": group_id,
                    "target": target,
                    "animated": False,
                    "style": {"stroke": "#64748b", "strokeWidth": 2},
                    "data": {"edgeType": "input", "params": params},
                })

    # Build edges from viz_graph - always route data edges through DATA nodes
    for source, target, edge_data in viz_graph.edges(data=True):
        edge_type = edge_data.get("edge_type", "data")
        value_name = edge_data.get("value_name", "")

        # Data edges go from DATA node to target (function→DATA edge already created above)
        if edge_type == "data" and value_name:
            edge_id = f"e_data_{source}_{value_name}_to_{target}"
            actual_source = f"data_{source}_{value_name}"
        else:
            # Control edges go directly between nodes
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

        # Add label for branch edges (IfElse nodes)
        if edge_type == "control":
            source_attrs = viz_graph.nodes.get(source, {})
            branch_data = source_attrs.get("branch_data", {})
            if "when_true" in branch_data:
                # This is an IfElse node
                if target == branch_data["when_true"]:
                    rf_edge["data"]["label"] = "True"
                elif target == branch_data["when_false"]:
                    rf_edge["data"]["label"] = "False"

        edges.append(rf_edge)

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "theme_preference": theme,
            "initial_depth": depth,
            "separate_outputs": separate_outputs,
            "show_types": show_types,
        },
    }
