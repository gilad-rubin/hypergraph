"""Render NetworkX graph to React Flow JSON format.

This package transforms a flattened NetworkX DiGraph into the React Flow
node/edge format expected by the visualization.

Public API:
    from hypergraph.viz.renderer import render_graph
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from hypergraph.viz._common import (
    build_expansion_state,
    build_output_to_producer_map,
    build_param_to_consumer_map,
    expansion_state_to_key,
    get_expandable_nodes,
)
from hypergraph.viz.renderer.precompute import precompute_all_edges, precompute_all_nodes
from hypergraph.viz.renderer.scope import build_graph_output_visibility


def render_graph(
    flat_graph: nx.DiGraph,
    *,
    depth: int = 0,
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
    input_spec = flat_graph.graph.get("input_spec", {})
    input_consumer_mode = "all"

    expansion_state = build_expansion_state(flat_graph, depth)
    graph_output_visibility = build_graph_output_visibility(flat_graph)

    # For JS meta data: use deepest targets (for interactive expand routing)
    param_to_consumer_deepest = build_param_to_consumer_map(flat_graph, expansion_state, use_deepest=True)
    output_to_producer_deepest = build_output_to_producer_map(flat_graph, expansion_state, use_deepest=True)
    # For JS meta data: node-to-parent map for routing
    node_to_parent = _build_node_to_parent_map(flat_graph)

    # Pre-compute edges for ALL valid expansion state combinations
    # CRITICAL: Pass input_groups=None so each state computes its own groups
    edges_by_state, expandable_nodes = precompute_all_edges(
        flat_graph,
        input_spec,
        show_types,
        theme,
        input_groups=None,
        graph_output_visibility=graph_output_visibility,
        input_consumer_mode=input_consumer_mode,
    )

    # Pre-compute nodes for ALL valid expansion state combinations
    nodes_by_state, _ = precompute_all_nodes(
        flat_graph,
        input_spec,
        show_types,
        theme,
        graph_output_visibility=graph_output_visibility,
        input_groups=None,
        input_consumer_mode=input_consumer_mode,
    )

    # Use pre-computed edges for the initial state
    initial_state_key = expansion_state_to_key(expansion_state)
    sep_key = "sep:1" if separate_outputs else "sep:0"
    full_initial_key = f"{initial_state_key}|{sep_key}" if initial_state_key else sep_key
    initial_edges = edges_by_state.get(full_initial_key, [])
    initial_nodes = nodes_by_state.get(full_initial_key, [])

    # Sort for deterministic ordering (prevents layout flickering)
    initial_nodes = sorted(initial_nodes, key=lambda n: n["id"])
    initial_edges = sorted(initial_edges, key=lambda e: e["id"])

    return {
        "nodes": initial_nodes,
        "edges": initial_edges,
        "meta": {
            "theme_preference": theme,
            "initial_depth": depth,
            "separate_outputs": separate_outputs,
            "show_types": show_types,
            "debug_overlays": debug_overlays,
            "output_to_producer": output_to_producer_deepest,
            "param_to_consumer": param_to_consumer_deepest,
            "node_to_parent": node_to_parent,
            "edgesByState": edges_by_state,
            "nodesByState": nodes_by_state,
            "expandableNodes": expandable_nodes,
        },
    }


def _build_node_to_parent_map(flat_graph: nx.DiGraph) -> dict[str, str]:
    """Build mapping from node name to parent name for routing."""
    return {
        node_id: attrs.get("parent")
        for node_id, attrs in flat_graph.nodes(data=True)
        if attrs.get("parent") is not None
    }
