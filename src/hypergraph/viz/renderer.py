"""Render hypergraph Graph to React Flow JSON format."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hypergraph.graph.core import Graph

from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.nodes.gate import GateNode, RouteNode, IfElseNode


def _get_node_type(hypernode: HyperNode) -> str:
    """Determine visualization node type from HyperNode class."""
    if isinstance(hypernode, GraphNode):
        return "PIPELINE"
    if isinstance(hypernode, (RouteNode, IfElseNode)):
        return "ROUTE"
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


def _get_node_inputs(hypernode: HyperNode) -> list[dict[str, Any]]:
    """Get input information for a node."""
    inputs = []
    for param in hypernode.inputs:
        input_type = hypernode.get_input_type(param)
        has_default = hypernode.has_default_for(param)
        inputs.append({
            "name": param,
            "type": _format_type(input_type),
            "has_default": has_default,
        })
    return inputs


def _get_node_outputs(hypernode: HyperNode) -> list[dict[str, Any]]:
    """Get output information for a node."""
    outputs = []
    for output_name in hypernode.outputs:
        output_type = hypernode.get_output_type(output_name)
        outputs.append({
            "name": output_name,
            "type": _format_type(output_type),
        })
    return outputs


def render_graph(
    graph: Graph,
    *,
    depth: int = 1,
    theme: str = "auto",
    show_types: bool = False,
    separate_outputs: bool = False,
) -> dict[str, Any]:
    """Convert a Graph to React Flow JSON format.

    Args:
        graph: The hypergraph Graph to render
        depth: How many levels of nested graphs to expand (0 = collapsed)
        theme: "dark", "light", or "auto" (detect from environment)
        show_types: Whether to show type annotations
        separate_outputs: Whether to render outputs as separate DATA nodes

    Returns:
        Dict with "nodes", "edges", and "options" keys ready for React Flow
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # Get bound parameters from graph's InputSpec
    input_spec = graph.inputs
    bound_params = input_spec.bound

    # Process each node in the graph
    for name, hypernode in graph.nodes.items():
        node_type = _get_node_type(hypernode)
        node_inputs = _get_node_inputs(hypernode)
        node_outputs = _get_node_outputs(hypernode)

        # Mark bound inputs
        for inp in node_inputs:
            inp["is_bound"] = inp["name"] in bound_params

        rf_node = {
            "id": name,
            "type": "custom",
            "position": {"x": 0, "y": 0},  # ELK will calculate
            "data": {
                "nodeType": node_type,
                "label": name,
                "inputs": node_inputs,
                "outputs": node_outputs,
                "isExpanded": depth > 0 if node_type == "PIPELINE" else None,
                "theme": theme,
                "showTypes": show_types,
            },
            "sourcePosition": "bottom",
            "targetPosition": "top",
        }
        nodes.append(rf_node)

        # Handle nested graphs
        if isinstance(hypernode, GraphNode) and depth > 0:
            inner_result = render_graph(
                hypernode.graph,
                depth=depth - 1,
                theme=theme,
                show_types=show_types,
                separate_outputs=separate_outputs,
            )
            # Add inner nodes with parent reference
            for inner_node in inner_result["nodes"]:
                inner_node["parentNode"] = name
                nodes.append(inner_node)
            # Add inner edges
            edges.extend(inner_result["edges"])

    # Build edges from nx_graph
    for source, target, edge_data in graph.nx_graph.edges(data=True):
        edge_type = edge_data.get("edge_type", "data")
        value_name = edge_data.get("value_name", "")

        edge_id = f"e_{source}_{target}_{value_name}"
        rf_edge = {
            "id": edge_id,
            "source": source,
            "target": target,
            "type": "default",
            "animated": edge_type == "control",
            "data": {
                "edgeType": edge_type,
                "valueName": value_name,
            },
        }
        edges.append(rf_edge)

    return {
        "nodes": nodes,
        "edges": edges,
        "options": {
            "theme": theme,
            "showTypes": show_types,
            "separateOutputs": separate_outputs,
            "depth": depth,
        },
    }
