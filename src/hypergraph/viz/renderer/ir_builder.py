"""Build the compact graph IR from a flat NetworkX graph.

This is the single entry point that replaces the legacy
`render_graph` (2^N precompute) and `render_graph_single_state` paths.
Frontends derive expansion state from the IR; Python does not enumerate
all 2^N states ahead of time.
"""

from __future__ import annotations

import networkx as nx

from hypergraph.viz._common import get_expandable_nodes
from hypergraph.viz.ir_schema import GraphIR, IREdge, IRExternalInput, IRNode
from hypergraph.viz.renderer._format import format_type
from hypergraph.viz.renderer.scope import compute_deepest_input_scope, get_deepest_consumers


def build_graph_ir(flat_graph: nx.DiGraph) -> GraphIR:
    nodes = [_build_ir_node(node_id, attrs) for node_id, attrs in flat_graph.nodes(data=True)]
    edges = [
        IREdge(
            source=src,
            target=tgt,
            edge_type=attrs.get("edge_type", "data"),
        )
        for src, tgt, attrs in flat_graph.edges(data=True)
    ]

    input_spec = flat_graph.graph.get("input_spec", {})
    external_inputs = [
        IRExternalInput(
            name=name,
            deepest_owner=compute_deepest_input_scope(name, flat_graph),
            consumers=tuple(get_deepest_consumers(name, flat_graph)),
        )
        for name in input_spec.get("required", ())
    ]

    return GraphIR(
        nodes=nodes,
        edges=edges,
        expandable_nodes=get_expandable_nodes(flat_graph),
        external_inputs=external_inputs,
    )


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
