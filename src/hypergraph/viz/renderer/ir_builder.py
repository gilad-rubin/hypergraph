"""Build the compact graph IR from a flat NetworkX graph.

This is the single entry point that replaces the legacy
`render_graph` (2^N precompute) and `render_graph_single_state` paths.
Frontends derive expansion state from the IR; Python does not enumerate
all 2^N states ahead of time.
"""

from __future__ import annotations

import networkx as nx

from hypergraph.viz._common import build_param_to_consumer_map, get_expandable_nodes
from hypergraph.viz.ir_schema import GraphIR, IREdge, IRExternalInput, IRNode
from hypergraph.viz.renderer._format import format_type
from hypergraph.viz.renderer.nodes import build_input_groups, get_param_type
from hypergraph.viz.renderer.scope import compute_deepest_input_scope, get_deepest_consumers


def build_graph_ir(flat_graph: nx.DiGraph) -> GraphIR:
    nodes = [_build_ir_node(node_id, attrs) for node_id, attrs in flat_graph.nodes(data=True)]
    edges = [_build_ir_edge(src, tgt, attrs, flat_graph) for src, tgt, attrs in flat_graph.edges(data=True)]

    input_spec = flat_graph.graph.get("input_spec", {})
    bound_params = set(input_spec.get("bound", {}).keys())
    shared_params = set(flat_graph.graph.get("shared", ()))
    # Use the legacy grouping logic so multi-param consumers collapse into
    # a single INPUT_GROUP. show_bounded_inputs=True here so bound params
    # are shipped in the IR; the frontend filters them per-render.
    # use_deepest=True so grouping is by the actual internal consumers
    # (state-independent fact) rather than the per-expansion-state visible
    # consumers — the IR must be a single source of truth across states.
    param_to_consumers = build_param_to_consumer_map(flat_graph, expansion_state={}, use_deepest=True)
    groups = build_input_groups(
        input_spec=input_spec,
        param_to_consumers=param_to_consumers,
        bound_params=bound_params,
        shared_params=shared_params,
        show_bounded_inputs=True,
    )
    external_inputs = [_build_input_group(group, flat_graph) for group in groups]

    return GraphIR(
        nodes=nodes,
        edges=edges,
        expandable_nodes=get_expandable_nodes(flat_graph),
        external_inputs=external_inputs,
    )


def _build_input_group(group: dict, flat_graph: nx.DiGraph) -> IRExternalInput:
    """Convert a build_input_groups dict into an IRExternalInput.

    Multi-param groups (e.g. one consumer takes both alpha and beta)
    become a single IRExternalInput with len(params) > 1, which
    scene_builder renders as INPUT_GROUP."""
    params = tuple(group["params"])
    consumers: list[str] = []
    seen: set[str] = set()
    for param in params:
        for consumer in get_deepest_consumers(param, flat_graph):
            if consumer not in seen:
                seen.add(consumer)
                consumers.append(consumer)
    deepest_owner = compute_deepest_input_scope(params[0], flat_graph)
    type_hints = tuple(format_type(get_param_type(p, flat_graph)) for p in params)
    return IRExternalInput(
        params=params,
        deepest_owner=deepest_owner,
        consumers=tuple(consumers),
        type_hints=type_hints,
        is_bound=bool(group.get("is_bound", False)),
    )


def _build_ir_edge(src: str, tgt: str, attrs: dict, flat_graph: nx.DiGraph) -> IREdge:
    """Pre-compute the source-when-expanded / target-when-expanded rewrites
    so the JS scene_builder can re-route edges on container expansion
    without re-walking the graph."""
    source_when_expanded: str | None = None
    target_when_expanded: str | None = None

    src_attrs = flat_graph.nodes.get(src, {})
    if src_attrs.get("node_type") == "GRAPH":
        # When the source container expands, the edge should originate
        # from whichever internal node produces the value flowing along
        # this edge. value_names lists those output names.
        for value_name in attrs.get("value_names", ()):
            internal = _find_deepest_internal_producer(src, value_name, flat_graph)
            if internal is not None:
                source_when_expanded = internal
                break

    tgt_attrs = flat_graph.nodes.get(tgt, {})
    if tgt_attrs.get("node_type") == "GRAPH":
        for value_name in attrs.get("value_names", ()):
            internal = _find_deepest_internal_consumer(tgt, value_name, flat_graph)
            if internal is not None:
                target_when_expanded = internal
                break

    return IREdge(
        source=src,
        target=tgt,
        edge_type=attrs.get("edge_type", "data"),
        source_when_expanded=source_when_expanded,
        target_when_expanded=target_when_expanded,
        value_names=tuple(attrs.get("value_names", ())),
    )


def _find_deepest_internal_producer(container_id: str, value_name: str, flat_graph: nx.DiGraph) -> str | None:
    """Walk down the container tree, find the deepest descendant that
    produces value_name as an output."""
    candidates = [
        node_id
        for node_id, attrs in flat_graph.nodes(data=True)
        if value_name in attrs.get("outputs", ()) and _is_descendant(node_id, container_id, flat_graph)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: _depth_below(c, container_id, flat_graph))


def _find_deepest_internal_consumer(container_id: str, value_name: str, flat_graph: nx.DiGraph) -> str | None:
    candidates = [
        node_id
        for node_id, attrs in flat_graph.nodes(data=True)
        if value_name in attrs.get("inputs", ()) and _is_descendant(node_id, container_id, flat_graph) and attrs.get("node_type") != "GRAPH"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: _depth_below(c, container_id, flat_graph))


def _is_descendant(node_id: str, ancestor_id: str, flat_graph: nx.DiGraph) -> bool:
    current = flat_graph.nodes.get(node_id, {}).get("parent")
    while current is not None:
        if current == ancestor_id:
            return True
        current = flat_graph.nodes.get(current, {}).get("parent")
    return False


def _depth_below(node_id: str, ancestor_id: str, flat_graph: nx.DiGraph) -> int:
    depth = 0
    current = flat_graph.nodes.get(node_id, {}).get("parent")
    while current is not None and current != ancestor_id:
        depth += 1
        current = flat_graph.nodes.get(current, {}).get("parent")
    return depth


def _build_ir_node(node_id: str, attrs: dict) -> IRNode:
    output_types = attrs.get("output_types", {})
    outputs = tuple({"name": out, "type": format_type(output_types.get(out))} for out in attrs.get("outputs", ()))

    input_types = attrs.get("input_types", {})
    has_defaults = attrs.get("has_defaults", {})
    inputs = tuple(
        {
            "name": param,
            "type": format_type(input_types.get(param)),
            "has_default": has_defaults.get(param, False),
        }
        for param in attrs.get("inputs", ())
    )

    return IRNode(
        id=node_id,
        node_type=attrs.get("node_type", "FUNCTION"),
        parent=attrs.get("parent"),
        label=attrs.get("label"),
        outputs=outputs,
        inputs=inputs,
        branch_data=attrs.get("branch_data"),
    )
