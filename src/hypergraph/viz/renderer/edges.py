"""Edge routing for React Flow visualization.

Computes edges between nodes for both merged and separate output modes,
handling container expansion and re-routing to internal nodes.
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from hypergraph.viz._common import (
    build_output_to_producer_map,
    build_param_to_consumer_map,
    is_descendant_of,
    is_node_visible,
)
from hypergraph.viz.renderer.nodes import (
    build_input_groups,
    get_group_targets,
    has_end_routing,
    is_data_node_visible,
)
from hypergraph.viz.renderer.scope import (
    find_container_entrypoints,
    find_internal_producer_for_output,
)


def add_end_node_edges(
    edges: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> None:
    """Add edges from gates that route to END."""
    if not has_end_routing(flat_graph, expansion_state):
        return

    for node_id, attrs in flat_graph.nodes(data=True):
        branch_data = attrs.get("branch_data", {})
        if not branch_data:
            continue

        if not is_node_visible(node_id, flat_graph, expansion_state):
            continue

        label = None
        has_end = False
        if branch_data.get("when_false") == "END":
            label = "False"
            has_end = True
        elif branch_data.get("when_true") == "END":
            label = "True"
            has_end = True
        elif "targets" in branch_data:
            targets = branch_data["targets"]
            target_values = targets.values() if isinstance(targets, dict) else targets
            if "END" in target_values:
                has_end = True

        if has_end:
            edges.append({
                "id": f"e_{node_id}_to___end__",
                "source": node_id,
                "target": "__end__",
                "animated": False,
                "style": {"stroke": "#10b981", "strokeWidth": 2},
                "data": {"edgeType": "end", "label": label},
            })


def add_merged_output_edges(
    edges: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> None:
    """Add edges in merged output mode (separateOutputs=false).

    Edges go directly from source function to target function,
    skipping DATA nodes entirely.
    """
    param_to_consumers = build_param_to_consumer_map(flat_graph, expansion_state)
    output_to_producer = build_output_to_producer_map(flat_graph, expansion_state, use_deepest=True)

    for source, target, edge_data in flat_graph.edges(data=True):
        if not is_node_visible(source, flat_graph, expansion_state):
            continue

        edge_type = edge_data.get("edge_type", "data")
        value_names = edge_data.get("value_names", [])

        if edge_type == "control":
            actual_target = target
            target_attrs = flat_graph.nodes.get(target, {})
            is_target_container = target_attrs.get("node_type") == "GRAPH"
            is_target_expanded = expansion_state.get(target, False)

            if is_target_container and is_target_expanded:
                entrypoints = find_container_entrypoints(
                    target, flat_graph, expansion_state
                )
                if entrypoints:
                    actual_target = entrypoints[0]

            if not is_node_visible(actual_target, flat_graph, expansion_state):
                continue

            edge_id = f"e_{source}_{actual_target}"
            rf_edge = {
                "id": edge_id,
                "source": source,
                "target": actual_target,
                "animated": False,
                "style": {"stroke": "#64748b", "strokeWidth": 2},
                "data": {
                    "edgeType": edge_type,
                    "valueName": "",
                },
            }

            original_source_attrs = flat_graph.nodes.get(source, {})
            branch_data = original_source_attrs.get("branch_data", {})
            if branch_data and "when_true" in branch_data:
                if target == branch_data["when_true"]:
                    rf_edge["data"]["label"] = "True"
                elif target == branch_data["when_false"]:
                    rf_edge["data"]["label"] = "False"

            edges.append(rf_edge)
            continue

        if edge_type == "ordering":
            actual_target = target
            if not is_node_visible(actual_target, flat_graph, expansion_state):
                continue

            edge_id = f"e_ord_{source}_{actual_target}"
            value_name = value_names[0] if value_names else ""
            rf_edge = {
                "id": edge_id,
                "source": source,
                "target": actual_target,
                "animated": False,
                "style": {
                    "stroke": "#8b5cf6",
                    "strokeWidth": 1.5,
                    "strokeDasharray": "6 3",
                },
                "data": {
                    "edgeType": "ordering",
                    "valueName": value_name,
                },
            }
            edges.append(rf_edge)
            continue

        values_to_process = value_names if value_names else [""]

        for value_name in values_to_process:
            actual_source = source
            source_attrs = flat_graph.nodes.get(source, {})
            is_source_container = source_attrs.get("node_type") == "GRAPH"
            is_source_expanded = expansion_state.get(source, False)

            if is_source_container and is_source_expanded:
                if value_name:
                    internal_producer = output_to_producer.get(value_name)
                    if internal_producer and internal_producer != source and is_descendant_of(internal_producer, source, flat_graph):
                        actual_source = internal_producer
                    else:
                        internal_source = find_internal_producer_for_output(
                            source, value_name, flat_graph, expansion_state
                        )
                        if internal_source:
                            actual_source = internal_source

            actual_target = target
            target_attrs = flat_graph.nodes.get(target, {})
            is_target_container = target_attrs.get("node_type") == "GRAPH"
            is_target_expanded = expansion_state.get(target, False)

            if is_target_container and is_target_expanded and value_name:
                consumers = param_to_consumers.get(value_name, [])
                internal_consumers = [
                    c for c in consumers
                    if c != target and is_descendant_of(c, target, flat_graph)
                ]
                if internal_consumers:
                    actual_target = internal_consumers[0]
                else:
                    entrypoints = find_container_entrypoints(
                        target, flat_graph, expansion_state
                    )
                    if entrypoints:
                        actual_target = entrypoints[0]

            if not is_node_visible(actual_source, flat_graph, expansion_state):
                continue
            if not is_node_visible(actual_target, flat_graph, expansion_state):
                continue
            if actual_source == actual_target:
                continue

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

            edges.append(rf_edge)


def add_separate_output_edges(
    edges: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    graph_output_visibility: dict[str, set[str]] | None = None,
) -> None:
    """Add edges in separate output mode (separateOutputs=true).

    Edges route through DATA nodes:
    - Function -> DATA node (for each output)
    - DATA node -> consumer functions
    """
    output_to_producer = build_output_to_producer_map(flat_graph, expansion_state, use_deepest=True)

    # 1. Add edges from function nodes to their DATA nodes
    for node_id, attrs in flat_graph.nodes(data=True):
        if not is_node_visible(node_id, flat_graph, expansion_state):
            continue

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
            if not is_data_node_visible(node_id, output_name, flat_graph, expansion_state):
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
        if not is_node_visible(source, flat_graph, expansion_state):
            continue
        if not is_node_visible(target, flat_graph, expansion_state):
            continue

        edge_type = edge_data.get("edge_type", "data")
        value_names = edge_data.get("value_names", [])

        if edge_type == "data":
            values_to_process = value_names if value_names else [""]

            for value_name in values_to_process:
                if not value_name:
                    continue

                source_attrs = flat_graph.nodes.get(source, {})
                is_source_container = source_attrs.get("node_type") == "GRAPH"
                is_source_expanded = expansion_state.get(source, False)
                if graph_output_visibility is not None and is_source_container:
                    allowed_outputs = graph_output_visibility.get(source, set())
                    if value_name not in allowed_outputs:
                        continue

                if is_source_container and is_source_expanded:
                    actual_producer = output_to_producer.get(value_name, source)
                    data_value = value_name
                    if actual_producer == source:
                        internal_producer = find_internal_producer_for_output(
                            source, value_name, flat_graph, expansion_state
                        )
                        if internal_producer:
                            actual_producer = internal_producer
                            internal_outputs = flat_graph.nodes[actual_producer].get("outputs", ())
                            internal_value = value_name
                            for out in internal_outputs:
                                if out in value_name or value_name in out:
                                    internal_value = out
                                    break
                            data_value = internal_value
                    data_source = actual_producer
                    if not is_data_node_visible(data_source, data_value, flat_graph, expansion_state):
                        continue
                    data_node_id = f"data_{data_source}_{data_value}"
                else:
                    if not is_data_node_visible(source, value_name, flat_graph, expansion_state):
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
        elif edge_type == "ordering":
            value_name = value_names[0] if value_names else ""
            edge_id = f"e_ord_{source}_{target}"
            edges.append({
                "id": edge_id,
                "source": source,
                "target": target,
                "animated": False,
                "style": {
                    "stroke": "#8b5cf6",
                    "strokeWidth": 1.5,
                    "strokeDasharray": "6 3",
                },
                "data": {
                    "edgeType": "ordering",
                    "valueName": value_name,
                },
            })

        else:
            actual_target = target
            if edge_type == "control":
                target_attrs = flat_graph.nodes.get(target, {})
                is_target_container = target_attrs.get("node_type") == "GRAPH"
                is_target_expanded = expansion_state.get(target, False)

                if is_target_container and is_target_expanded:
                    entrypoints = find_container_entrypoints(
                        target, flat_graph, expansion_state
                    )
                    if entrypoints:
                        actual_target = entrypoints[0]

            if source == actual_target:
                continue

            edge_id = f"e_{source}_{actual_target}"

            rf_edge = {
                "id": edge_id,
                "source": source,
                "target": actual_target,
                "animated": False,
                "style": {"stroke": "#64748b", "strokeWidth": 2},
                "data": {
                    "edgeType": edge_type,
                    "valueName": "",
                },
            }

            if edge_type == "control":
                source_attrs = flat_graph.nodes.get(source, {})
                branch_data = source_attrs.get("branch_data", {})
                if branch_data and "when_true" in branch_data:
                    if target == branch_data["when_true"]:
                        rf_edge["data"]["label"] = "True"
                    elif target == branch_data["when_false"]:
                        rf_edge["data"]["label"] = "False"

            edges.append(rf_edge)


def compute_edges_for_state(
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
    """Compute edges for a specific expansion state."""
    edges: list[dict[str, Any]] = []

    param_to_consumers = build_param_to_consumer_map(
        flat_graph,
        expansion_state,
        mode=input_consumer_mode,
    )

    bound_params = set(input_spec.get("bound", {}).keys())
    if input_groups is None:
        input_groups = build_input_groups(input_spec, param_to_consumers, bound_params)

    # 1. Add edges from INPUT/INPUT_GROUP nodes to their consumers
    for group in input_groups:
        params = group["params"]
        actual_targets = get_group_targets(params, flat_graph, param_to_consumers)
        if not actual_targets:
            continue

        if len(params) == 1:
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

    # 2. Add edges between function nodes
    if separate_outputs:
        add_separate_output_edges(edges, flat_graph, expansion_state, graph_output_visibility)
    else:
        add_merged_output_edges(edges, flat_graph, expansion_state)

    # 3. Add edges to END node
    add_end_node_edges(edges, flat_graph, expansion_state)

    return edges
