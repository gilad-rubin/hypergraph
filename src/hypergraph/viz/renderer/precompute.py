"""Orchestration for precomputing nodes and edges across all expansion states.

Generates the nodesByState and edgesByState dictionaries that allow JavaScript
to instantly switch between expansion states without re-computing layout.
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from hypergraph.viz._common import (
    build_param_to_consumer_map,
    enumerate_valid_expansion_states,
    expansion_state_to_key,
    get_expandable_nodes,
    is_node_visible,
)
from hypergraph.viz.renderer.edges import compute_edges_for_state
from hypergraph.viz.renderer.nodes import (
    apply_node_visibility,
    build_input_groups,
    create_data_nodes,
    create_end_node,
    create_input_nodes,
    create_rf_node,
)


def compute_nodes_for_state(
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

    self_loop_nodes = {source for source, target in flat_graph.edges() if source == target and is_node_visible(source, flat_graph, expansion_state)}

    bound_params = set(input_spec.get("bound", {}).keys())
    param_to_consumer = build_param_to_consumer_map(
        flat_graph,
        expansion_state,
        mode=input_consumer_mode,
    )
    if input_groups is None:
        input_groups = build_input_groups(input_spec, param_to_consumer, bound_params)

    create_input_nodes(
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
        if attrs.get("hide", False):
            continue

        parent_id = attrs.get("parent")
        node_type = attrs.get("node_type", "FUNCTION")
        rf_node_type = "PIPELINE" if node_type == "GRAPH" else node_type
        is_expanded = expansion_state.get(node_id, False)

        rf_node = create_rf_node(
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
        if node_id in self_loop_nodes:
            rf_node.setdefault("data", {})["selfLoop"] = True

        if node_type == "GRAPH" and not separate_outputs:
            allowed_outputs = graph_output_visibility.get(node_id) if graph_output_visibility else None
            if allowed_outputs is not None and "outputs" in rf_node["data"]:
                rf_node["data"]["outputs"] = [out for out in rf_node["data"]["outputs"] if out["name"] in allowed_outputs]

        nodes.append(rf_node)

    create_data_nodes(nodes, flat_graph, theme, show_types, graph_output_visibility)

    create_end_node(nodes, flat_graph, theme, show_types, expansion_state)

    for node in nodes:
        node.setdefault("data", {})["separateOutputs"] = separate_outputs

    apply_node_visibility(nodes, expansion_state, separate_outputs)

    nodes.sort(key=lambda n: n["id"])
    return nodes


def precompute_all_edges(
    flat_graph: nx.DiGraph,
    input_spec: dict[str, Any],
    show_types: bool,
    theme: str,
    input_groups: list[dict[str, Any]] | None = None,
    graph_output_visibility: dict[str, set[str]] | None = None,
    input_consumer_mode: str = "all",
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Pre-compute edges for all valid expansion state combinations."""
    expandable_nodes = get_expandable_nodes(flat_graph)

    if not expandable_nodes:
        edges_merged = compute_edges_for_state(
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
        edges_separate = compute_edges_for_state(
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
    valid_states = enumerate_valid_expansion_states(flat_graph, expandable_nodes)

    for state in valid_states:
        exp_key = expansion_state_to_key(state)

        key_merged = f"{exp_key}|sep:0"
        edges_merged = compute_edges_for_state(
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

        key_separate = f"{exp_key}|sep:1"
        edges_separate = compute_edges_for_state(
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


def precompute_all_nodes(
    flat_graph: nx.DiGraph,
    input_spec: dict[str, Any],
    show_types: bool,
    theme: str,
    graph_output_visibility: dict[str, set[str]] | None = None,
    input_groups: list[dict[str, Any]] | None = None,
    input_consumer_mode: str = "all",
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Pre-compute nodes for all valid expansion state combinations."""
    expandable_nodes = get_expandable_nodes(flat_graph)

    if not expandable_nodes:
        nodes_merged = compute_nodes_for_state(
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
        nodes_separate = compute_nodes_for_state(
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
    valid_states = enumerate_valid_expansion_states(flat_graph, expandable_nodes)

    for state in valid_states:
        exp_key = expansion_state_to_key(state)

        key_merged = f"{exp_key}|sep:0"
        nodes_by_state[key_merged] = compute_nodes_for_state(
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
        nodes_by_state[key_separate] = compute_nodes_for_state(
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
