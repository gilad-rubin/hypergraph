"""Node construction for React Flow visualization.

Creates React Flow node objects (INPUT, INPUT_GROUP, FUNCTION, PIPELINE,
DATA, END) from the flat graph structure.
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from hypergraph.viz._common import (
    get_root_ancestor,
    is_node_visible,
)
from hypergraph.viz.renderer._format import format_type
from hypergraph.viz.renderer.scope import (
    compute_deepest_input_scope,
    compute_input_scope,
    is_output_externally_consumed,
)

# =============================================================================
# Input Grouping
# =============================================================================


def group_inputs_by_consumers_and_bound(
    external_inputs: set[str],
    param_to_consumers: dict[str, list[str]],
    bound_params: set[str],
) -> dict[tuple[frozenset[str], bool], list[str]]:
    """Group input parameters by their consumers and bound status."""
    groups: dict[tuple[frozenset[str], bool], list[str]] = {}
    for param in external_inputs:
        consumers = frozenset(param_to_consumers.get(param, []))
        is_bound = param in bound_params
        key = (consumers, is_bound)
        groups.setdefault(key, []).append(param)
    return groups


def build_input_groups(
    input_spec: dict[str, Any],
    param_to_consumers: dict[str, list[str]],
    bound_params: set[str],
) -> list[dict[str, Any]]:
    """Build stable input groups for rendering and edge routing."""
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = set(required) | set(optional)

    groups = group_inputs_by_consumers_and_bound(external_inputs, param_to_consumers, bound_params)

    group_specs: list[dict[str, Any]] = []
    for (_, is_bound), params in groups.items():
        group_specs.append(
            {
                "params": sorted(params),
                "is_bound": is_bound,
            }
        )

    group_specs.sort(key=lambda g: "_".join(g["params"]))
    return group_specs


def build_classic_input_groups(
    input_spec: dict[str, Any],
    bound_params: set[str],
) -> list[dict[str, Any]]:
    """Build single-parameter input groups (classic layout behavior)."""
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    params = sorted(set(required) | set(optional))
    return [{"params": [param], "is_bound": param in bound_params} for param in params]


# =============================================================================
# Param / Target Helpers
# =============================================================================


def get_param_type(param: str, flat_graph: nx.DiGraph) -> type | None:
    """Find the type annotation for a parameter from the graph."""
    for _, attrs in flat_graph.nodes(data=True):
        if param in attrs.get("inputs", ()):
            param_type = attrs.get("input_types", {}).get(param)
            if param_type is not None:
                return param_type
    return None


def get_param_targets(
    param: str,
    flat_graph: nx.DiGraph,
    param_to_consumers: dict[str, list[str]],
) -> list[str]:
    """Get the actual target nodes for a parameter."""
    actual_targets = param_to_consumers.get(param, [])
    if not actual_targets:
        for node_id, attrs in flat_graph.nodes(data=True):
            if param in attrs.get("inputs", ()):
                return [get_root_ancestor(node_id, flat_graph)]
    return actual_targets


def get_group_targets(
    params: list[str],
    flat_graph: nx.DiGraph,
    param_to_consumers: dict[str, list[str]],
) -> list[str]:
    """Get unique target nodes for a group of parameters."""
    targets: list[str] = []
    seen: set[str] = set()
    for param in params:
        for target in get_param_targets(param, flat_graph, param_to_consumers):
            if target not in seen:
                seen.add(target)
                targets.append(target)
    return targets


# =============================================================================
# Node Creation
# =============================================================================


def create_rf_node(
    node_id: str,
    attrs: dict[str, Any],
    node_type: str,
    is_expanded: bool | None,
    parent_id: str | None,
    bound_params: set[str],
    theme: str,
    show_types: bool,
    separate_outputs: bool,
) -> dict[str, Any]:
    """Create a React Flow node from graph attributes."""
    rf_node: dict[str, Any] = {
        "id": node_id,
        "type": "pipelineGroup" if node_type == "PIPELINE" and is_expanded else "custom",
        "position": {"x": 0, "y": 0},
        "data": {
            "nodeType": node_type,
            "label": attrs.get("label", node_id),
            "theme": theme,
            "showTypes": show_types,
            "separateOutputs": separate_outputs,
        },
        "sourcePosition": "bottom",
        "targetPosition": "top",
    }

    if parent_id is not None:
        rf_node["parentNode"] = parent_id
        rf_node["extent"] = "parent"

    if node_type == "PIPELINE":
        rf_node["data"]["isExpanded"] = is_expanded
        if is_expanded:
            rf_node["style"] = {"width": 600, "height": 400}

    if not separate_outputs and node_type in ("FUNCTION", "PIPELINE"):
        output_types = attrs.get("output_types", {})
        rf_node["data"]["outputs"] = [{"name": out, "type": format_type(output_types.get(out))} for out in attrs.get("outputs", ())]

    input_types = attrs.get("input_types", {})
    has_defaults = attrs.get("has_defaults", {})
    rf_node["data"]["inputs"] = [
        {
            "name": param,
            "type": format_type(input_types.get(param)),
            "has_default": has_defaults.get(param, False),
            "is_bound": param in bound_params,
        }
        for param in attrs.get("inputs", ())
    ]

    branch_data = attrs.get("branch_data")
    if branch_data:
        if "when_true" in branch_data:
            rf_node["data"]["whenTrueTarget"] = branch_data["when_true"]
            rf_node["data"]["whenFalseTarget"] = branch_data["when_false"]
        if "targets" in branch_data:
            rf_node["data"]["targets"] = branch_data["targets"]

    return rf_node


def create_input_nodes(
    nodes: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    input_spec: dict,
    bound_params: set[str],
    theme: str,
    show_types: bool,
    param_to_consumers: dict[str, list[str]],
    expansion_state: dict[str, bool],
    input_groups: list[dict[str, Any]] | None = None,
) -> None:
    """Create INPUT nodes for external input parameters, grouping where possible."""
    if input_groups is None:
        input_groups = build_input_groups(input_spec, param_to_consumers, bound_params)

    for group in input_groups:
        params = group["params"]
        is_bound = group["is_bound"]
        if len(params) == 1:
            param = params[0]
            input_node_id = f"input_{param}"
            param_type = get_param_type(param, flat_graph)
            actual_targets = get_group_targets([param], flat_graph, param_to_consumers)
            owner_container = compute_input_scope(param, flat_graph, expansion_state)
            deepest_owner = compute_deepest_input_scope(param, flat_graph)

            input_node: dict[str, Any] = {
                "id": input_node_id,
                "type": "custom",
                "position": {"x": 0, "y": 0},
                "data": {
                    "nodeType": "INPUT",
                    "label": param,
                    "typeHint": format_type(param_type),
                    "isBound": is_bound,
                    "actualTargets": actual_targets,
                    "theme": theme,
                    "showTypes": show_types,
                    "ownerContainer": owner_container,
                    "deepestOwnerContainer": deepest_owner,
                },
                "sourcePosition": "bottom",
                "targetPosition": "top",
            }
            nodes.append(input_node)
        else:
            group_id = f"input_group_{'_'.join(params)}"
            param_types = [format_type(get_param_type(p, flat_graph)) for p in params]
            actual_targets = get_group_targets(params, flat_graph, param_to_consumers)

            owner_container = compute_input_scope(params[0], flat_graph, expansion_state)
            deepest_owner = compute_deepest_input_scope(params[0], flat_graph)

            group_node: dict[str, Any] = {
                "id": group_id,
                "type": "custom",
                "position": {"x": 0, "y": 0},
                "data": {
                    "nodeType": "INPUT_GROUP",
                    "params": params,
                    "paramTypes": param_types,
                    "isBound": is_bound,
                    "actualTargets": actual_targets,
                    "ownerContainer": owner_container,
                    "deepestOwnerContainer": deepest_owner,
                    "theme": theme,
                    "showTypes": show_types,
                },
                "sourcePosition": "bottom",
                "targetPosition": "top",
            }
            nodes.append(group_node)


def has_end_routing(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> bool:
    """Check if any visible gate node routes to the END sentinel."""
    for node_id, attrs in flat_graph.nodes(data=True):
        branch_data = attrs.get("branch_data", {})
        if not branch_data:
            continue

        if not is_node_visible(node_id, flat_graph, expansion_state):
            continue

        if branch_data.get("when_false") == "END" or branch_data.get("when_true") == "END":
            return True
        if "targets" in branch_data:
            targets = branch_data["targets"]
            target_values = targets.values() if isinstance(targets, dict) else targets
            if "END" in target_values:
                return True
    return False


def create_end_node(
    nodes: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    theme: str,
    show_types: bool,
    expansion_state: dict[str, bool],
) -> None:
    """Create the END node when the graph explicitly routes to END."""
    if not has_end_routing(flat_graph, expansion_state):
        return

    end_node: dict[str, Any] = {
        "id": "__end__",
        "type": "custom",
        "position": {"x": 0, "y": 0},
        "data": {
            "nodeType": "END",
            "label": "End",
            "theme": theme,
            "showTypes": show_types,
        },
        "sourcePosition": "bottom",
        "targetPosition": "top",
    }
    nodes.append(end_node)


def create_data_nodes(
    nodes: list[dict[str, Any]],
    flat_graph: nx.DiGraph,
    theme: str,
    show_types: bool,
    graph_output_visibility: dict[str, set[str]] | None = None,
) -> None:
    """Create DATA nodes for all outputs."""
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("hide", False):
            continue

        output_types = attrs.get("output_types", {})
        parent_id = attrs.get("parent")
        allowed_outputs = None
        if graph_output_visibility is not None and attrs.get("node_type") == "GRAPH":
            allowed_outputs = graph_output_visibility.get(node_id, set())

        for output_name in attrs.get("outputs", ()):
            if allowed_outputs is not None and output_name not in allowed_outputs:
                continue
            data_node_id = f"data_{node_id}_{output_name}"

            is_external = is_output_externally_consumed(output_name, node_id, flat_graph)

            data_node = {
                "id": data_node_id,
                "type": "custom",
                "position": {"x": 0, "y": 0},
                "data": {
                    "nodeType": "DATA",
                    "label": output_name,
                    "typeHint": format_type(output_types.get(output_name)),
                    "sourceId": node_id,
                    "theme": theme,
                    "showTypes": show_types,
                    "internalOnly": not is_external,
                },
                "sourcePosition": "bottom",
                "targetPosition": "top",
            }

            if parent_id is not None:
                data_node["parentNode"] = parent_id
                data_node["extent"] = "parent"

            nodes.append(data_node)


def is_data_node_visible(
    source_id: str,
    output_name: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> bool:
    """Check if a DATA node should be visible for the current expansion state."""
    if not is_node_visible(source_id, flat_graph, expansion_state):
        return False

    source_attrs = flat_graph.nodes.get(source_id, {})
    return not (source_attrs.get("node_type") == "GRAPH" and expansion_state.get(source_id, False))


def apply_node_visibility(
    nodes: list[dict[str, Any]],
    expansion_state: dict[str, bool],
    separate_outputs: bool,
) -> None:
    """Apply visibility rules to nodes in-place, setting `hidden` flags."""
    parent_map: dict[str, str] = {n["id"]: n["parentNode"] for n in nodes if n.get("parentNode")}
    pipeline_ids = {n["id"] for n in nodes if n.get("data", {}).get("nodeType") == "PIPELINE"}

    def _hidden_by_ancestor(node_id: str) -> bool:
        current = node_id
        while current:
            parent = parent_map.get(current)
            if not parent:
                return False
            if expansion_state.get(parent) is False:
                return True
            current = parent
        return False

    for node in nodes:
        data = node.get("data", {})
        node_type = data.get("nodeType")

        hidden = _hidden_by_ancestor(node["id"])

        if node_type == "DATA" and data.get("internalOnly"):
            parent = node.get("parentNode")
            if parent and not expansion_state.get(parent, False):
                hidden = True

        if node_type in ("INPUT", "INPUT_GROUP"):
            owner = data.get("deepestOwnerContainer") or data.get("ownerContainer")
            if owner:
                if expansion_state.get(owner) is not True:
                    hidden = True
                else:
                    current = parent_map.get(owner)
                    while current:
                        if expansion_state.get(current) is False:
                            hidden = True
                            break
                        current = parent_map.get(current)

        if separate_outputs:
            if node_type == "DATA":
                source_id = data.get("sourceId")
                if source_id in pipeline_ids and expansion_state.get(source_id, False):
                    hidden = True
        else:
            if node_type == "DATA":
                hidden = True

        node["hidden"] = hidden
