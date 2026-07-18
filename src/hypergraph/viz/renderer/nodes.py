"""Input grouping and node-level helpers for visualization.

Shared by ``mermaid.py`` and ``renderer/ir_builder.py``: input grouping,
parameter types, START/END routing checks, and gate-output filtering.
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from hypergraph.viz._common import is_node_visible
from hypergraph.viz.renderer.scope import resolve_expanded_entrypoints

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
    shared_params: set[str],
    show_bounded_inputs: bool,
) -> list[dict[str, Any]]:
    """Build stable input groups for rendering and edge routing."""
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = (set(required) | set(optional)) - shared_params
    if not show_bounded_inputs:
        external_inputs -= bound_params

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


# =============================================================================
# START/END Routing and Gate Outputs
# =============================================================================


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


def get_start_targets(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    container_entrypoints: dict[str, tuple[str, ...]],
) -> list[str]:
    """Get visible targets for the synthetic START node.

    START edges are only created for explicit graph entrypoints configured
    via ``Graph.with_entrypoint(...)``.
    """
    configured = flat_graph.graph.get("configured_entrypoints") or ()
    targets: list[str] = []
    seen: set[str] = set()

    for node_id in configured:
        if node_id not in flat_graph:
            continue

        resolved: str | None = node_id
        while resolved is not None and not is_node_visible(resolved, flat_graph, expansion_state):
            resolved = flat_graph.nodes[resolved].get("parent")

        if resolved is None:
            continue

        attrs = flat_graph.nodes.get(resolved, {})
        if attrs.get("node_type") == "GRAPH" and expansion_state.get(resolved, False):
            # Canonical derivation (D14, #211). Mermaid draws a single START
            # edge, so only the first entrypoint is used here.
            entrypoints = resolve_expanded_entrypoints(
                (resolved,),
                container_entrypoints,
                expansion_state,
            )
            if not entrypoints:
                continue
            resolved = entrypoints[0]

        if not is_node_visible(resolved, flat_graph, expansion_state):
            continue

        if resolved in seen:
            continue

        seen.add(resolved)
        targets.append(resolved)

    return targets


def is_internal_gate_output(node_id: str, output_name: str, attrs: dict[str, Any]) -> bool:
    """Whether output_name is the gate's internal routing signal output.

    Gate nodes expose an internal data output ``_{gate_name}`` for runtime
    bookkeeping. It should not be rendered as a user-facing DATA node.
    """
    if attrs.get("node_type") != "BRANCH":
        return False
    local_name = node_id.rsplit("/", 1)[-1]
    return output_name == f"_{local_name}"
