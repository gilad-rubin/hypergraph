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
    show_inputs: bool = True,
    show_bounded_inputs: bool = False,
) -> dict[str, Any]:
    """Build a React Flow scene (nodes + edges) for the IR's initial state."""
    expansion_state = expansion_state or {}
    parent_map = {n.id: n.parent for n in ir.nodes if n.parent is not None}
    output_visibility = ir.graph_output_visibility or {}

    scene_nodes: list[dict[str, Any]] = []

    for ir_node in ir.nodes:
        is_expanded = expansion_state.get(ir_node.id, False) if ir_node.node_type == "GRAPH" else None
        scene_node_type = _scene_node_type(ir_node.node_type)
        rf_type = "pipelineGroup" if scene_node_type == "PIPELINE" and is_expanded else "custom"

        data = {
            "nodeType": scene_node_type,
            "label": ir_node.label or ir_node.id,
            "separateOutputs": separate_outputs,
            "inputs": [dict(i) for i in ir_node.inputs],
        }
        if not separate_outputs and scene_node_type in ("FUNCTION", "PIPELINE"):
            outputs = list(ir_node.outputs)
            # Collapsed GRAPH containers expose only outputs that flow out
            # of the container (or are explicitly declared collapsed_outputs)
            # — internal-only data should not bubble to the container surface.
            if scene_node_type == "PIPELINE" and ir_node.id in output_visibility:
                visible = set(output_visibility[ir_node.id])
                outputs = [out for out in outputs if out["name"] in visible]
            data["outputs"] = outputs
        if scene_node_type == "PIPELINE":
            data["isExpanded"] = bool(is_expanded)
        if ir_node.branch_data:
            if "when_true" in ir_node.branch_data:
                data["whenTrueTarget"] = ir_node.branch_data["when_true"]
                data["whenFalseTarget"] = ir_node.branch_data["when_false"]
            if "targets" in ir_node.branch_data:
                data["targets"] = ir_node.branch_data["targets"]

        scene_node = {
            "id": ir_node.id,
            "type": rf_type,
            "position": {"x": 0, "y": 0},
            "data": data,
            "sourcePosition": "bottom",
            "targetPosition": "top",
            "hidden": _ancestor_collapsed(ir_node.id, parent_map, expansion_state),
        }
        if ir_node.parent is not None:
            scene_node["parentNode"] = ir_node.parent
            scene_node["extent"] = "parent"
        if scene_node_type == "PIPELINE" and is_expanded:
            scene_node["style"] = {"width": 600, "height": 400}

        scene_nodes.append(scene_node)

    for ext in ir.external_inputs:
        # Bound external inputs are hidden by default; the renderer opts in
        # via show_bounded_inputs=True (e.g. in debug overlays).
        if ext.is_bound and not show_bounded_inputs:
            continue
        # show_inputs=False removes INPUT nodes (and their edges) entirely,
        # not just hidden — matches the legacy renderer's behavior so
        # downstream consumers don't see ghost INPUT artifacts.
        if not show_inputs:
            continue
        hidden = _input_hidden(ext.deepest_owner, parent_map, expansion_state)
        # ownerContainer is the deepest *visible* ancestor of the input's
        # deepest owner — the container the INPUT visually nests into.
        # deepestOwnerContainer is the state-independent fact (always the
        # deepest scope); ownerContainer is per-render.
        owner_container = _visible_owner(ext.deepest_owner, parent_map, expansion_state)
        if ext.is_group:
            scene_nodes.append(
                {
                    "id": f"input_group_{'_'.join(ext.params)}",
                    "type": "custom",
                    "position": {"x": 0, "y": 0},
                    "data": {
                        "nodeType": "INPUT_GROUP",
                        "params": list(ext.params),
                        "paramTypes": list(ext.type_hints),
                        "isBound": ext.is_bound,
                        "ownerContainer": owner_container,
                        "deepestOwnerContainer": ext.deepest_owner,
                        "actualTargets": list(ext.consumers),
                    },
                    "sourcePosition": "bottom",
                    "targetPosition": "top",
                    "hidden": hidden,
                }
            )
        else:
            scene_nodes.append(
                {
                    "id": f"input_{ext.params[0]}",
                    "type": "custom",
                    "position": {"x": 0, "y": 0},
                    "data": {
                        "nodeType": "INPUT",
                        "label": ext.params[0],
                        "typeHint": ext.type_hints[0] if ext.type_hints else None,
                        "isBound": ext.is_bound,
                        "ownerContainer": owner_container,
                        "deepestOwnerContainer": ext.deepest_owner,
                        "actualTargets": list(ext.consumers),
                    },
                    "sourcePosition": "bottom",
                    "targetPosition": "top",
                    "hidden": hidden,
                }
            )

    if separate_outputs:
        # Note: BRANCH nodes also produce DATA scene nodes for their emit
        # outputs; only the internal routing-signal output is filtered out.
        for ir_node in ir.nodes:
            if ir_node.node_type not in ("FUNCTION", "GRAPH", "BRANCH"):
                continue
            # GRAPH containers only expose externally-consumed (or explicit
            # collapsed_outputs) outputs; internal data nodes don't surface.
            visible_for_node = set(output_visibility.get(ir_node.id, ())) if ir_node.node_type == "GRAPH" else None
            for out in ir_node.outputs:
                if out.get("is_gate_internal"):
                    continue
                if visible_for_node is not None and out["name"] not in visible_for_node:
                    continue
                data_node_id = f"data_{ir_node.id}_{out['name']}"
                ancestor_hidden = _ancestor_collapsed(ir_node.id, parent_map, expansion_state)
                scene_node: dict[str, Any] = {
                    "id": data_node_id,
                    "type": "custom",
                    "position": {"x": 0, "y": 0},
                    "data": {
                        "nodeType": "DATA",
                        "label": out["name"],
                        "typeHint": out.get("type"),
                        "sourceId": ir_node.id,
                        "internalOnly": bool(out.get("internal_only")),
                    },
                    "sourcePosition": "bottom",
                    "targetPosition": "top",
                    "hidden": ancestor_hidden,
                }
                if ir_node.parent is not None:
                    scene_node["parentNode"] = ir_node.parent
                    scene_node["extent"] = "parent"
                scene_nodes.append(scene_node)

    visible_ids = {n["id"] for n in scene_nodes if not n["hidden"]}

    scene_edges: list[dict[str, Any]] = []

    for ir_edge in ir.edges:
        # Container expansion rewrites: when source/target container is
        # expanded, route the edge to the deepest internal producer/consumer
        # instead of the container hull.
        source = ir_edge.source
        if expansion_state.get(source) and ir_edge.source_when_expanded:
            source = ir_edge.source_when_expanded
        target = ir_edge.target
        if expansion_state.get(target) and ir_edge.target_when_expanded:
            target = ir_edge.target_when_expanded

        # separate_outputs reroutes data edges through DATA nodes:
        # producer -> data_<producer>_<value_name> -> consumer
        if separate_outputs and ir_edge.edge_type == "data" and ir_edge.value_names:
            value_name = ir_edge.value_names[0]
            source = f"data_{source}_{value_name}"

        scene_edges.append(
            {
                "id": f"{source}__{target}",
                "source": source,
                "target": target,
                "data": {
                    "edgeType": ir_edge.edge_type,
                    "valueName": ir_edge.value_names[0] if ir_edge.value_names else None,
                    "label": ir_edge.label,
                    "exclusive": bool(ir_edge.exclusive),
                    "forceFeedback": bool(ir_edge.is_back_edge),
                },
                "hidden": source not in visible_ids or target not in visible_ids,
            }
        )

    if separate_outputs:
        # Add output edges from each producer to its DATA nodes.
        for ir_node in ir.nodes:
            if ir_node.node_type not in ("FUNCTION", "GRAPH"):
                continue
            for out in ir_node.outputs:
                data_node_id = f"data_{ir_node.id}_{out['name']}"
                scene_edges.append(
                    {
                        "id": f"{ir_node.id}__{data_node_id}",
                        "source": ir_node.id,
                        "target": data_node_id,
                        "data": {"edgeType": "output"},
                        "hidden": ir_node.id not in visible_ids or data_node_id not in visible_ids,
                    }
                )

    for ext in ir.external_inputs:
        if ext.is_bound and not show_bounded_inputs:
            continue
        if not show_inputs:
            continue
        input_node_id = f"input_group_{'_'.join(ext.params)}" if ext.is_group else f"input_{ext.params[0]}"
        for consumer in ext.consumers:
            scene_edges.append(
                {
                    "id": f"{input_node_id}__{consumer}",
                    "source": input_node_id,
                    "target": consumer,
                    "data": {"edgeType": "input"},
                    "hidden": input_node_id not in visible_ids or consumer not in visible_ids,
                }
            )

    _add_start_end_nodes_and_edges(ir, scene_nodes, scene_edges, parent_map, expansion_state, visible_ids)

    return {"nodes": scene_nodes, "edges": scene_edges}


def _add_start_end_nodes_and_edges(
    ir: GraphIR,
    scene_nodes: list[dict[str, Any]],
    scene_edges: list[dict[str, Any]],
    parent_map: dict[str, str],
    expansion_state: dict[str, bool],
    visible_ids: set[str],
) -> None:
    """Synthesize the synthetic ``__start__`` / ``__end__`` boundary nodes
    and the edges connecting them to visible entrypoints / END-routed
    gates. Mirrors the behavior of the legacy ``create_start_node`` /
    ``create_end_node`` helpers."""

    # When an entrypoint is itself a GRAPH and currently expanded, the
    # START edge should attach to its inner entrypoint (the first child
    # node) so it visually connects to executable code instead of the
    # container chrome. Pre-compute entrypoint mapping once.
    entrypoint_overrides = _expanded_container_entrypoints(ir, expansion_state)

    start_targets: list[str] = []
    seen_start: set[str] = set()
    for entry in ir.configured_entrypoints:
        target = entrypoint_overrides.get(entry, entry)
        resolved = _resolve_to_visible(target, parent_map, expansion_state, visible_ids)
        if resolved is None or resolved in seen_start:
            continue
        seen_start.add(resolved)
        start_targets.append(resolved)

    if start_targets:
        scene_nodes.append(_synthetic_node("__start__", "START", "Start"))
        for target in start_targets:
            scene_edges.append(
                {
                    "id": f"__start____{target}",
                    "source": "__start__",
                    "target": target,
                    "data": {"edgeType": "start"},
                    "hidden": False,
                }
            )

    end_sources: list[str] = []
    seen_end: set[str] = set()
    for ir_node in ir.nodes:
        branch_data = ir_node.branch_data or {}
        if not branch_data:
            continue
        if not _routes_to_end(branch_data):
            continue
        resolved = _resolve_to_visible(ir_node.id, parent_map, expansion_state, visible_ids)
        if resolved is None or resolved in seen_end:
            continue
        seen_end.add(resolved)
        end_sources.append(resolved)

    if end_sources:
        scene_nodes.append(_synthetic_node("__end__", "END", "End"))
        for source in end_sources:
            scene_edges.append(
                {
                    "id": f"{source}____end__",
                    "source": source,
                    "target": "__end__",
                    "data": {"edgeType": "end"},
                    "hidden": False,
                }
            )


def _routes_to_end(branch_data: dict) -> bool:
    if branch_data.get("when_true") == "END" or branch_data.get("when_false") == "END":
        return True
    targets = branch_data.get("targets")
    if isinstance(targets, dict):
        return "END" in targets.values()
    if isinstance(targets, (list, tuple)):
        return "END" in targets
    return False


def _resolve_to_visible(
    node_id: str,
    parent_map: dict[str, str],
    expansion_state: dict[str, bool],
    visible_ids: set[str],
) -> str | None:
    """Walk up to the first ancestor that is currently visible; return
    None if every ancestor is hidden."""
    current: str | None = node_id
    while current is not None and current not in visible_ids:
        current = parent_map.get(current)
    return current


def _expanded_container_entrypoints(
    ir: GraphIR,
    expansion_state: dict[str, bool],
) -> dict[str, str]:
    """For each GRAPH currently expanded, find an inner child to receive
    edges that would otherwise attach to the container hull.

    The chosen inner child is the first non-GRAPH descendant whose own
    parents are all expanded — i.e. the visible entrypoint inside the
    expanded container.
    """
    children_by_parent: dict[str, list[str]] = {}
    for ir_node in ir.nodes:
        if ir_node.parent is not None:
            children_by_parent.setdefault(ir_node.parent, []).append(ir_node.id)

    overrides: dict[str, str] = {}
    for ir_node in ir.nodes:
        if ir_node.node_type != "GRAPH":
            continue
        if not expansion_state.get(ir_node.id):
            continue
        # First visible non-GRAPH descendant inherits the START attachment.
        for child_id in children_by_parent.get(ir_node.id, []):
            overrides[ir_node.id] = child_id
            break
    return overrides


def _synthetic_node(node_id: str, node_type: str, label: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "custom",
        "position": {"x": 0, "y": 0},
        "data": {"nodeType": node_type, "label": label},
        "sourcePosition": "bottom",
        "targetPosition": "top",
        "hidden": False,
    }


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


def _visible_owner(
    deepest_owner: str | None,
    parent_map: dict[str, str],
    expansion_state: dict[str, bool],
) -> str | None:
    """Walk up from ``deepest_owner`` to the first ancestor that is currently
    expanded (or has no further parent). The returned id is the container
    the INPUT scene node visually nests inside at the current state."""
    if deepest_owner is None:
        return None
    current: str | None = deepest_owner
    while current is not None and not expansion_state.get(current, False):
        current = parent_map.get(current)
    return current


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
