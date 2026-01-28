"""Graph validation logic.

This module contains all build-time validation for Graph construction.
"""

from __future__ import annotations

import keyword
from collections import defaultdict
from typing import Any, TYPE_CHECKING

import networkx as nx

from hypergraph._typing import is_type_compatible

if TYPE_CHECKING:
    from hypergraph.nodes.base import HyperNode


class GraphConfigError(Exception):
    """Raised when graph configuration is invalid."""

    pass


def validate_graph(
    nodes: dict[str, "HyperNode"],
    nx_graph: nx.DiGraph,
    graph_name: str | None,
    strict_types: bool,
) -> None:
    """Run all build-time validations on a graph.

    Args:
        nodes: Map of node name -> HyperNode
        nx_graph: The NetworkX directed graph
        graph_name: Optional graph name
        strict_types: Whether to validate type compatibility
    """
    _validate_graph_name(graph_name)
    _validate_reserved_names(nodes)
    _validate_valid_identifiers(nodes)
    _validate_no_namespace_collision(nodes)
    _validate_consistent_defaults(nodes)
    _validate_gate_targets(nodes)
    _validate_no_gate_self_loop(nodes)
    _validate_multi_target_output_conflicts(nodes)
    if strict_types:
        _validate_types(nodes, nx_graph)


def _validate_graph_name(graph_name: str | None) -> None:
    """Graph names cannot contain reserved path separators."""
    reserved_chars = {".", "/"}
    if graph_name is not None:
        for char in reserved_chars:
            if char in graph_name:
                raise GraphConfigError(
                    f"Invalid graph name: '{graph_name}'\n\n"
                    f"  -> Graph names cannot contain '{char}'\n\n"
                    f"How to fix:\n"
                    f"  Use underscores or hyphens instead"
                )


def _validate_valid_identifiers(nodes: dict[str, "HyperNode"]) -> None:
    """Node and output names must be valid Python identifiers (not keywords)."""
    from hypergraph.nodes.graph_node import GraphNode

    for node in nodes.values():
        # Skip GraphNode - it uses graph name validation (allows hyphens)
        if isinstance(node, GraphNode):
            continue
        if not node.name.isidentifier():
            raise GraphConfigError(
                f"Invalid node name: '{node.name}'\n\n"
                f"  -> Names must be valid Python identifiers\n\n"
                f"How to fix:\n"
                f"  Use letters, numbers, underscores only"
            )
        if keyword.iskeyword(node.name):
            raise GraphConfigError(
                f"Invalid node name: '{node.name}'\n\n"
                f"  -> '{node.name}' is a Python keyword and cannot be used\n\n"
                f"How to fix:\n"
                f"  Use a different name (e.g., '{node.name}_node' or '{node.name}_func')"
            )
        for output in node.outputs:
            if not output.isidentifier():
                raise GraphConfigError(
                    f"Invalid output name: '{output}' (from node '{node.name}')\n\n"
                    f"  -> Output names must be valid Python identifiers"
                )
            if keyword.iskeyword(output):
                raise GraphConfigError(
                    f"Invalid output name: '{output}' (from node '{node.name}')\n\n"
                    f"  -> '{output}' is a Python keyword and cannot be used\n\n"
                    f"How to fix:\n"
                    f"  Use a different name (e.g., '{output}_value' or '{output}_result')"
                )


def _validate_no_namespace_collision(nodes: dict[str, "HyperNode"]) -> None:
    """Ensure GraphNode names don't collide with output names.

    GraphNode names are used for path-based result access (e.g., results['subgraph.output']).
    If a GraphNode name matches an output name, it creates ambiguity.
    """
    from hypergraph.nodes.graph_node import GraphNode

    graph_node_names = {
        node.name for node in nodes.values() if isinstance(node, GraphNode)
    }

    if not graph_node_names:
        return  # No GraphNodes, nothing to validate

    # Collect ALL outputs (including from other GraphNodes)
    all_outputs: dict[str, str] = {}  # output_name -> source_node_name
    for node in nodes.values():
        for output in node.outputs:
            all_outputs[output] = node.name

    # Check for collision between GraphNode names and any output
    for gn_name in graph_node_names:
        if gn_name in all_outputs:
            source_node = all_outputs[gn_name]
            # Skip if the GraphNode's own output matches its name (that's fine)
            if source_node == gn_name:
                continue
            raise GraphConfigError(
                f"GraphNode name '{gn_name}' collides with output name\n\n"
                f"  -> GraphNode '{gn_name}' exists\n"
                f"  -> Node '{source_node}' outputs '{gn_name}'\n\n"
                f"How to fix:\n"
                f"  Rename the GraphNode: graph.as_node(name='other_name')"
            )


def _validate_consistent_defaults(nodes: dict[str, "HyperNode"]) -> None:
    """Shared input parameters must have ALL-or-NONE consistent defaults."""
    param_info = _collect_param_default_info(nodes)

    for param, info_list in param_info.items():
        if len(info_list) < 2:
            continue
        _check_defaults_consistency(param, info_list)


def _collect_param_default_info(
    nodes: dict[str, "HyperNode"],
) -> dict[str, list[tuple[bool, Any, str]]]:
    """Collect default info for each parameter across all nodes."""
    param_info: dict[str, list[tuple[bool, Any, str]]] = defaultdict(list)
    for node in nodes.values():
        for param in node.inputs:
            has_default = node.has_default_for(param)
            if has_default:
                default_value = node.get_default_for(param)
                param_info[param].append((True, default_value, node.name))
            else:
                param_info[param].append((False, None, node.name))
    return param_info


def _check_defaults_consistency(
    param: str, info_list: list[tuple[bool, Any, str]]
) -> None:
    """Check that defaults are consistent for a shared parameter."""
    with_default = []
    without_default = []
    for has, v, n in info_list:
        if has:
            with_default.append((v, n))
        else:
            without_default.append(n)

    if with_default and without_default:
        raise GraphConfigError(
            f"Inconsistent defaults for '{param}'\n\n"
            f"  -> Nodes with default: {', '.join(n for _, n in with_default)}\n"
            f"  -> Nodes without default: {', '.join(without_default)}\n\n"
            f"How to fix:\n"
            f"  Add the same default to all nodes, or use graph.bind()"
        )

    _check_default_values_match(param, with_default)


def _check_default_values_match(
    param: str, with_default: list[tuple[Any, str]]
) -> None:
    """Check that all default values for a parameter are identical."""
    if len(with_default) <= 1:
        return

    first_value, first_node = with_default[0]
    for value, node_name in with_default[1:]:
        if not _values_equal(first_value, value):
            raise GraphConfigError(
                f"Inconsistent defaults for '{param}'\n\n"
                f"  -> Node '{first_node}' has default: {first_value!r}\n"
                f"  -> Node '{node_name}' has default: {value!r}\n\n"
                f"How to fix:\n"
                f"  Use the same default in both nodes"
            )


def _values_equal(a: Any, b: Any) -> bool:
    """Compare two values, handling types like numpy arrays gracefully.

    Uses identity check first, then equality. Falls back to False if
    comparison fails or returns ambiguous results.
    """
    if a is b:
        return True
    try:
        equal = a == b
        # Handle numpy-like arrays that return non-scalar from ==
        if hasattr(equal, "__iter__"):
            return all(equal)
        return bool(equal)
    except (ValueError, TypeError):
        return False


def _validate_types(nodes: dict[str, "HyperNode"], nx_graph: nx.DiGraph) -> None:
    """Validate type compatibility between connected nodes.

    Checks each edge (source_node -> target_node) for:
    1. Missing type annotations (raises error if either side is missing)
    2. Type mismatches (raises error if types are incompatible)

    Only called when strict_types=True.
    """
    for source_name, target_name, edge_data in nx_graph.edges(data=True):
        value_name = edge_data.get("value_name")
        if value_name is None:
            continue

        source_node = nodes[source_name]
        target_node = nodes[target_name]

        # Get types using universal capability methods
        output_type = source_node.get_output_type(value_name)
        input_type = target_node.get_input_type(value_name)

        # Check for missing annotations
        if output_type is None:
            raise GraphConfigError(
                f"Missing type annotation in strict_types mode\n\n"
                f"  -> Node '{source_name}' output '{value_name}' has no type annotation\n\n"
                f"How to fix:\n"
                f"  Add type annotation: def {source_name}(...) -> ReturnType"
            )

        if input_type is None:
            raise GraphConfigError(
                f"Missing type annotation in strict_types mode\n\n"
                f"  -> Node '{target_name}' parameter '{value_name}' has no type annotation\n\n"
                f"How to fix:\n"
                f"  Add type annotation: def {target_name}({value_name}: YourType) -> ReturnType"
            )

        # Check type compatibility
        if not is_type_compatible(output_type, input_type):
            raise GraphConfigError(
                f"Type mismatch between nodes\n\n"
                f"  -> Node '{source_name}' output '{value_name}' has type: {output_type}\n"
                f"  -> Node '{target_name}' input '{value_name}' expects type: {input_type}\n\n"
                f"How to fix:\n"
                f"  Either change the type annotation on one of the nodes, or add a\n"
                f"  conversion node between them."
            )


# =============================================================================
# Gate Validation Functions
# =============================================================================


def _validate_reserved_names(nodes: dict[str, "HyperNode"]) -> None:
    """Node names cannot be reserved words like 'END'."""
    for name in nodes:
        if name == "END":
            raise GraphConfigError(
                f"Invalid node name: 'END'\n\n"
                f"  -> 'END' is reserved for the routing sentinel\n\n"
                f"How to fix: Use a different name (e.g., 'end_node', 'finish')"
            )


def _validate_gate_targets(nodes: dict[str, "HyperNode"]) -> None:
    """Validate that all gate targets exist in the graph (or are END)."""
    from hypergraph.nodes.gate import GateNode, END

    for node in nodes.values():
        if not isinstance(node, GateNode):
            continue

        for target in node.targets:
            if target is END:
                continue  # END is always valid
            if target not in nodes:
                raise GraphConfigError(
                    f"Gate '{node.name}' targets unknown node '{target}'\n\n"
                    f"  -> Target '{target}' is not in the graph\n"
                    f"  -> Available nodes: {sorted(nodes.keys())}\n\n"
                    f"How to fix:\n"
                    f"  1. Add a node named '{target}' to the graph, OR\n"
                    f"  2. Remove '{target}' from the gate's targets"
                )


def _validate_no_gate_self_loop(nodes: dict[str, "HyperNode"]) -> None:
    """Gates cannot route to themselves."""
    from hypergraph.nodes.gate import GateNode

    for node in nodes.values():
        if not isinstance(node, GateNode):
            continue

        if node.name in node.targets:
            raise GraphConfigError(
                f"Gate '{node.name}' cannot target itself\n\n"
                f"  -> targets include '{node.name}'\n\n"
                f"How to fix:\n"
                f"  Create a separate node for the retry logic and route to it"
            )


def _validate_multi_target_output_conflicts(nodes: dict[str, "HyperNode"]) -> None:
    """Validate that multi_target gates don't have targets sharing outputs.

    For multi_target=True, we can't know which targets will run, so if
    two targets produce the same output name, it's an error (could overwrite).

    For multi_target=False (default), exactly one target runs, so same
    output names are allowed (mutually exclusive).
    """
    from hypergraph.nodes.gate import RouteNode, END

    for node in nodes.values():
        if not isinstance(node, RouteNode):
            continue
        if not node.multi_target:
            continue  # Mutex - no validation needed

        # Check for duplicate outputs among targets
        output_producers: dict[str, list[str]] = {}
        for target_name in node.targets:
            if target_name is END:
                continue
            target = nodes.get(target_name)
            if target is None:
                continue
            for output in target.outputs:
                output_producers.setdefault(output, []).append(target_name)

        for output, producers in output_producers.items():
            if len(producers) > 1:
                raise GraphConfigError(
                    f"Multi-target gate '{node.name}' has targets sharing output '{output}'\n\n"
                    f"  -> Targets producing '{output}': {producers}\n"
                    f"  -> multi_target=True means multiple targets can run in parallel\n\n"
                    f"How to fix:\n"
                    f"  - Rename outputs so each target produces unique names, OR\n"
                    f"  - Use multi_target=False if targets are mutually exclusive"
                )
