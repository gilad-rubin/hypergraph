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


def build_graph_ir(flat_graph: nx.DiGraph) -> GraphIR:
    nodes = [
        IRNode(
            id=node_id,
            node_type=attrs.get("node_type", "FUNCTION"),
            parent=attrs.get("parent"),
        )
        for node_id, attrs in flat_graph.nodes(data=True)
    ]
    edges = [
        IREdge(
            source=src,
            target=tgt,
            edge_type=attrs.get("edge_type", "data"),
        )
        for src, tgt, attrs in flat_graph.edges(data=True)
    ]

    input_spec = flat_graph.graph.get("input_spec", {})
    external_inputs = [IRExternalInput(name=name) for name in input_spec.get("required", ())]

    return GraphIR(
        nodes=nodes,
        edges=edges,
        expandable_nodes=get_expandable_nodes(flat_graph),
        external_inputs=external_inputs,
    )
