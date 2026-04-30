"""Build a React Flow scene from the compact IR.

This is the Python reference implementation. The JS port
(assets/scene_builder.js) must produce semantically equivalent output
for the same IR. Both run on the same pure-graph facts; neither relies
on Python-side 2^N expansion-state precomputation.
"""

from __future__ import annotations

from typing import Any

from hypergraph.viz.ir_schema import GraphIR


def build_initial_scene(
    ir: GraphIR,
    *,
    expansion_state: dict[str, bool] | None = None,
    separate_outputs: bool = False,
) -> dict[str, Any]:
    """Build a React Flow scene (nodes + edges) for the IR's initial state."""
    expansion_state = expansion_state or {}
    parent_map = {n.id: n.parent for n in ir.nodes if n.parent is not None}

    scene_nodes: list[dict[str, Any]] = []

    for ir_node in ir.nodes:
        scene_nodes.append(
            {
                "id": ir_node.id,
                "data": {"nodeType": _scene_node_type(ir_node.node_type), "label": ir_node.id},
                "parentNode": ir_node.parent,
                "hidden": _ancestor_collapsed(ir_node.id, parent_map, expansion_state),
            }
        )

    for ext in ir.external_inputs:
        scene_nodes.append(
            {
                "id": f"input_{ext.name}",
                "data": {
                    "nodeType": "INPUT",
                    "label": ext.name,
                    "deepestOwnerContainer": ext.deepest_owner,
                },
                "hidden": _input_hidden(ext.deepest_owner, parent_map, expansion_state),
            }
        )

    return {"nodes": scene_nodes, "edges": []}


def _scene_node_type(ir_node_type: str) -> str:
    if ir_node_type == "GRAPH":
        return "PIPELINE"
    return ir_node_type


def _ancestor_collapsed(
    node_id: str,
    parent_map: dict[str, str],
    expansion_state: dict[str, bool],
) -> bool:
    current = node_id
    while True:
        parent = parent_map.get(current)
        if parent is None:
            return False
        if expansion_state.get(parent) is False or expansion_state.get(parent) is None:
            return True
        current = parent


def _input_hidden(
    deepest_owner: str | None,
    parent_map: dict[str, str],
    expansion_state: dict[str, bool],
) -> bool:
    """An INPUT is visible only when its deepest owner container (and all
    ancestors) are expanded. No deepest owner = top-level input, always
    visible."""
    if deepest_owner is None:
        return False
    if not expansion_state.get(deepest_owner, False):
        return True
    current = parent_map.get(deepest_owner)
    while current is not None:
        if not expansion_state.get(current, False):
            return True
        current = parent_map.get(current)
    return False
