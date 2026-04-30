"""Render NetworkX graph to React Flow JSON format.

Single source of truth: ``build_graph_ir`` produces the compact IR; the
Python ``scene_builder`` (mirrored by ``assets/scene_builder.js``) turns
that IR + an expansion state into a React Flow scene. The legacy 2^N
``edgesByState``/``nodesByState`` precompute is gone (PR #88).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import networkx as nx

from hypergraph.viz._common import (
    build_expansion_state,
    build_output_to_producer_map,
    build_param_to_consumer_map,
)
from hypergraph.viz._common import (
    get_expandable_nodes as get_expandable_nodes,
)
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from hypergraph.viz.scene_builder import build_initial_scene

__all__ = ["render_graph"]


def render_graph(
    flat_graph: nx.DiGraph,
    *,
    depth: int = 0,
    theme: str = "auto",
    show_types: bool = True,
    separate_outputs: bool = False,
    show_inputs: bool = True,
    show_bounded_inputs: bool = False,
    debug_overlays: bool = False,
) -> dict[str, Any]:
    """Convert a flattened NetworkX graph to a React Flow scene + metadata.

    Returns a single-state scene plus the IR (so downstream JS can
    re-derive subsequent expansion states without a kernel).
    """
    expansion_state = build_expansion_state(flat_graph, depth)
    ir = build_graph_ir(flat_graph)
    scene = build_initial_scene(
        ir,
        expansion_state=expansion_state,
        separate_outputs=separate_outputs,
        show_inputs=show_inputs,
        show_bounded_inputs=show_bounded_inputs,
    )

    return {
        "nodes": scene["nodes"],
        "edges": scene["edges"],
        "meta": {
            "ir": asdict(ir),
            "initial_expansion": expansion_state,
            "initial_depth": depth,
            "theme_preference": theme,
            "show_types": show_types,
            "separate_outputs": separate_outputs,
            "show_inputs": show_inputs,
            "show_bounded_inputs": show_bounded_inputs,
            "debug_overlays": debug_overlays,
            # Routing maps consumed by viz.js (`routingData` in
            # `assets/viz.js`). They feed `performRecursiveLayout` for
            # nested-container edge routing and the debug API. The
            # IR-driven `scene_builder.js` doesn't read them itself.
            "output_to_producer": build_output_to_producer_map(flat_graph, expansion_state, use_deepest=True),
            "param_to_consumer": build_param_to_consumer_map(flat_graph, expansion_state, use_deepest=True),
            "node_to_parent": _build_node_to_parent_map(flat_graph),
            "shared": flat_graph.graph.get("shared", []),
        },
    }


def _build_node_to_parent_map(flat_graph: nx.DiGraph) -> dict[str, str]:
    return {node_id: attrs.get("parent") for node_id, attrs in flat_graph.nodes(data=True) if attrs.get("parent") is not None}
