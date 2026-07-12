"""HyperTable-specific graph visualization ownership."""

from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence
from typing import Any

from hypergraph.graph import Graph
from hypergraph.materialization._provenance import find_boundary_node
from hypergraph.materialization._schema import TableSpec, item_schema_fields, return_type


def fanout_viz_edges(
    graph: Any,
    spec: TableSpec,
    map_over_nodes: Sequence[Any],
) -> list[tuple[str, str, tuple[str, ...]]]:
    """Viz-only producer-to-mapped-node edges, paired by declaration order."""
    edges: list[tuple[str, str, tuple[str, ...]]] = []
    for child_spec, map_node in zip(spec.children, map_over_nodes, strict=True):
        column = child_spec.map_input
        if not column:
            continue
        producer = find_boundary_node(graph, child_spec)
        if producer is None:
            continue
        edges.append((producer.name, map_node.name, (column,)))
    return edges


def fanout_map_fields(
    graph: Any,
    spec: TableSpec,
    map_over_nodes: Sequence[Any],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Map each injected fan-out edge to fields exposed by one mapped item."""
    fields: dict[tuple[str, str], tuple[str, ...]] = {}
    for child_spec, map_node in zip(spec.children, map_over_nodes, strict=True):
        if not child_spec.map_input:
            continue
        producer = find_boundary_node(graph, child_spec)
        if producer is None:
            continue
        config = getattr(map_node, "_map_config", None) or {}
        schema = config.get("schema")
        if schema is None:
            try:
                schema = return_type(producer)
            except Exception:
                schema = None
        fields[(producer.name, map_node.name)] = item_schema_fields(schema)
    return fields


def render_hypertable(
    graph: Any,
    spec: TableSpec,
    map_over_nodes: Sequence[Any],
    components: Mapping[str, Any],
    *,
    include_children: bool,
    options: dict[str, Any],
) -> Any:
    """Render the compute graph plus its storage-aware mapped-child fan-outs."""
    if not include_children or not spec.children:
        return graph.visualize(**options)

    from hypergraph.viz.widget import render_flat_graph

    all_nodes = list(graph.nodes.values()) if isinstance(graph.nodes, dict) else []
    all_nodes.extend(map_over_nodes)
    combined = Graph(all_nodes, name=spec.name)
    if components:
        valid_inputs = set(combined.inputs.all)
        binds = {name: value for name, value in components.items() if name in valid_inputs}
        if binds:
            combined = combined.bind(**binds)

    extra_edges = fanout_viz_edges(graph, spec, map_over_nodes)
    for source_id, target_id, value_names in extra_edges:
        if source_id in combined.nx_graph.nodes and target_id in combined.nx_graph.nodes and not combined.nx_graph.has_edge(source_id, target_id):
            combined.nx_graph.add_edge(
                source_id,
                target_id,
                edge_type="data",
                value_names=list(value_names),
                is_map=True,
            )

    show_external_inputs = options.pop("show_external_inputs", None)
    show_inputs = options.pop("show_inputs", None)
    if show_external_inputs is not None:
        if show_inputs is not None and show_inputs != show_external_inputs:
            raise TypeError("Pass either show_inputs or show_external_inputs, not both.")
        warnings.warn(
            "show_external_inputs is deprecated; use show_inputs instead.",
            DeprecationWarning,
            stacklevel=3,
        )
        show_inputs = show_external_inputs
    if show_inputs is None:
        show_inputs = True
    options.setdefault("depth", 1)

    flat_graph = combined.to_flat_graph(extra_edges=extra_edges)
    for (source_id, target_id), field_names in fanout_map_fields(graph, spec, map_over_nodes).items():
        if flat_graph.has_edge(source_id, target_id):
            flat_graph[source_id][target_id]["map_fields"] = list(field_names)

    return render_flat_graph(flat_graph, combined, show_inputs=show_inputs, **options)
