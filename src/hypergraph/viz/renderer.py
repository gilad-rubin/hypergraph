"""Render hypergraph Graph to React Flow JSON format.

This module transforms a hypergraph Graph into the React Flow node/edge
format expected by the visualization. Layout is performed client-side
using ELK (Eclipse Layout Kernel).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hypergraph.graph.core import Graph

from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.nodes.gate import GateNode, RouteNode, IfElseNode, END


def _get_node_type(hypernode: HyperNode) -> str:
    """Determine visualization node type from HyperNode class."""
    if isinstance(hypernode, GraphNode):
        return "PIPELINE"
    if isinstance(hypernode, (RouteNode, IfElseNode)):
        return "BRANCH"
    if isinstance(hypernode, GateNode):
        return "BRANCH"
    return "FUNCTION"


def _format_type(t: type | None) -> str | None:
    """Format a type annotation for display."""
    if t is None:
        return None
    if hasattr(t, "__name__"):
        return t.__name__
    # Handle generic types like list[str], dict[str, int]
    return str(t).replace("typing.", "")


def render_graph(
    graph: Graph,
    *,
    depth: int = 1,
    theme: str = "auto",
    show_types: bool = False,
    separate_outputs: bool = False,
    _is_nested: bool = False,
) -> dict[str, Any]:
    """Convert a Graph to React Flow JSON format.

    Args:
        graph: The hypergraph Graph to render
        depth: How many levels of nested graphs to expand (0 = collapsed)
        theme: "dark", "light", or "auto" (detect from environment)
        show_types: Whether to show type annotations
        separate_outputs: Whether to render outputs as separate DATA nodes
        _is_nested: Internal flag - True when rendering a nested subgraph

    Returns:
        Dict with "nodes", "edges", and "meta" keys ready for React Flow
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # Get bound parameters from graph's InputSpec
    input_spec = graph.inputs
    bound_params = set(input_spec.bound)

    # Track which outputs are produced by which nodes (for DATA node sourceId)
    output_to_source: dict[str, str] = {}
    for name, hypernode in graph.nodes.items():
        for output in hypernode.outputs:
            output_to_source[output] = name

    # Create INPUT_GROUP nodes for external inputs, grouped by (targets, is_bound)
    # Inputs targeting the same node(s) with the same bound state get grouped together
    # Skip INPUT_GROUP creation for nested graphs - their inputs are pass-through from parent
    external_inputs = list(input_spec.required) + list(input_spec.optional)
    if external_inputs and not _is_nested:
        # Build mapping: param -> (targets, type, is_bound)
        param_info: dict[str, tuple[frozenset[str], type | None, bool]] = {}
        for param in external_inputs:
            targets = set()
            param_type = None
            is_bound = param in bound_params
            # Find all nodes that consume this parameter
            for node_name, hypernode in graph.nodes.items():
                if param in hypernode.inputs:
                    targets.add(node_name)
                    if param_type is None:
                        param_type = hypernode.get_input_type(param)
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
    for name, hypernode in graph.nodes.items():
        node_type = _get_node_type(hypernode)
        is_expanded = depth > 0 if node_type == "PIPELINE" else None

        rf_node: dict[str, Any] = {
            "id": name,
            "type": "pipelineGroup" if node_type == "PIPELINE" and is_expanded else "custom",
            "position": {"x": 0, "y": 0},  # ELK will calculate
            "data": {
                "nodeType": node_type,
                "label": name,
                "theme": theme,
                "showTypes": show_types,
                "separateOutputs": separate_outputs,
            },
            "sourcePosition": "bottom",
            "targetPosition": "top",
        }

        # Add expansion state for pipelines
        if node_type == "PIPELINE":
            rf_node["data"]["isExpanded"] = is_expanded
            if is_expanded:
                rf_node["style"] = {"width": 600, "height": 400}

        # Add outputs for function/pipeline nodes (when not separate_outputs mode)
        if not separate_outputs and node_type in ("FUNCTION", "PIPELINE"):
            outputs = []
            for output_name in hypernode.outputs:
                output_type = hypernode.get_output_type(output_name)
                outputs.append({
                    "name": output_name,
                    "type": _format_type(output_type),
                })
            rf_node["data"]["outputs"] = outputs

        # Add inputs info for bound input badge
        inputs = []
        for param in hypernode.inputs:
            input_type = hypernode.get_input_type(param)
            has_default = hypernode.has_default_for(param)
            is_bound = param in bound_params
            inputs.append({
                "name": param,
                "type": _format_type(input_type),
                "has_default": has_default,
                "is_bound": is_bound,
            })
        rf_node["data"]["inputs"] = inputs

        # Add branch-specific data
        if isinstance(hypernode, IfElseNode):
            rf_node["data"]["whenTrueTarget"] = hypernode.when_true
            rf_node["data"]["whenFalseTarget"] = hypernode.when_false
        elif isinstance(hypernode, RouteNode):
            # Convert END sentinel to string "END" for JSON serialization
            rf_node["data"]["targets"] = [
                "END" if t is END else t for t in hypernode.targets
            ]

        nodes.append(rf_node)

        # Handle nested graphs - always include children for expandability
        # Children are hidden by default when collapsed (handled by JS visibility)
        if isinstance(hypernode, GraphNode):
            # Recursively render inner graph with depth-1 (or 0 if already 0)
            inner_depth = max(0, depth - 1)
            inner_result = render_graph(
                hypernode.graph,
                depth=inner_depth,
                theme=theme,
                show_types=show_types,
                separate_outputs=separate_outputs,
                _is_nested=True,  # Mark as nested to skip INPUT_GROUP creation
            )
            # Add inner nodes with parent reference
            for inner_node in inner_result["nodes"]:
                # Only set parentNode for direct children (not already nested deeper)
                if "parentNode" not in inner_node:
                    inner_node["parentNode"] = name
                inner_node["extent"] = "parent"
                nodes.append(inner_node)
            # Add inner edges
            edges.extend(inner_result["edges"])

    # Always create DATA nodes for all outputs (visibility controlled by JS)
    for name, hypernode in graph.nodes.items():
        for output_name in hypernode.outputs:
            output_type = hypernode.get_output_type(output_name)
            data_node = {
                "id": f"data_{name}_{output_name}",
                "type": "custom",
                "position": {"x": 0, "y": 0},
                "data": {
                    "nodeType": "DATA",
                    "label": output_name,
                    "typeHint": _format_type(output_type),
                    "sourceId": name,  # Link back to source node
                    "theme": theme,
                    "showTypes": show_types,
                },
                "sourcePosition": "bottom",
                "targetPosition": "top",
            }
            nodes.append(data_node)

            # Edge from function to its DATA output node
            edges.append({
                "id": f"e_{name}_to_{data_node['id']}",
                "source": name,
                "target": data_node["id"],
                "animated": False,
                "style": {"stroke": "#64748b", "strokeWidth": 2},
                "data": {"edgeType": "output"},  # Mark as output edge
            })

    # Create edges from INPUT_GROUP nodes to their target nodes
    # Each INPUT_GROUP connects to its specific targets (stored in node data)
    # Note: Even for expanded pipelines, we create the edge to the container for layout
    # positioning. The visual routing to inner nodes is handled in layout.js.
    for node in nodes:
        if node.get("data", {}).get("nodeType") == "INPUT_GROUP":
            group_id = node["id"]
            targets = node["data"].get("targets", [])
            params = node["data"].get("params", [])
            for target in targets:
                # Check if target is an expanded PIPELINE - store info for layout.js
                hypernode = graph.nodes.get(target)
                is_expanded_pipeline = (
                    isinstance(hypernode, GraphNode)
                    and depth > 0
                )

                # Find inner nodes that consume these params (for visual routing)
                inner_targets = []
                if is_expanded_pipeline:
                    inner_graph = hypernode.graph
                    for param in params:
                        for inner_name, inner_node in inner_graph.nodes.items():
                            if param in inner_node.inputs:
                                inner_targets.append(inner_name)

                edges.append({
                    "id": f"e_{group_id}_to_{target}",
                    "source": group_id,
                    "target": target,
                    "animated": False,
                    "style": {"stroke": "#64748b", "strokeWidth": 2},
                    "data": {
                        "edgeType": "input",
                        "params": params,
                        # For expanded pipelines, include inner targets for visual routing
                        "innerTargets": inner_targets if inner_targets else None,
                    },
                })

    # Build edges from nx_graph - always route data edges through DATA nodes
    for source, target, edge_data in graph.nx_graph.edges(data=True):
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

        # Add label for branch edges
        if edge_type == "control" and isinstance(graph.nodes.get(source), IfElseNode):
            # Determine if this is true or false branch
            gate = graph.nodes[source]
            if target == gate.when_true:
                rf_edge["data"]["label"] = "True"
            elif target == gate.when_false:
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
