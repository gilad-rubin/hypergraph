"""Input specification calculation for graphs.

This module contains the InputSpec dataclass and logic for computing
which parameters are required or optional for a configured graph scope.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import networkx as nx

from hypergraph.graph._helpers import get_edge_produced_values, sources_of

if TYPE_CHECKING:
    from hypergraph.nodes.base import HyperNode


@dataclass(frozen=True)
class InputSpec:
    """Specification of graph input parameters.

    Categories follow the "edge cancels default" rule:
    - required: No edge, no default, not bound -> must always provide
    - optional: No edge, has default OR bound -> can omit (fallback exists)
    - entrypoints: Reserved for compatibility; empty for configured graphs.
    """

    required: tuple[str, ...]
    optional: tuple[str, ...]
    entrypoints: dict[str, tuple[str, ...]]
    bound: dict[str, Any]

    @property
    def all(self) -> tuple[str, ...]:
        """All input names (required + optional)."""
        seen = set(self.required + self.optional)
        entry_params: list[str] = []
        for params in self.entrypoints.values():
            for p in params:
                if p not in seen:
                    seen.add(p)
                    entry_params.append(p)
        return self.required + self.optional + tuple(entry_params)


def compute_input_spec(
    nodes: dict[str, HyperNode],
    nx_graph: nx.DiGraph,
    bound: dict[str, Any],
    *,
    entrypoints: tuple[str, ...] | None = None,
    selected: tuple[str, ...] | None = None,
    _active_scope: tuple[dict[str, HyperNode], nx.DiGraph] | None = None,
) -> InputSpec:
    """Compute input specification for a graph.

    Required inputs depend on four dimensions:
    - Entrypoints (start): which nodes execute
    - Selection (end): which outputs are needed
    - Bindings (pre-fill): which params have fixed values
    - Defaults (fallback): which params have function-level fallbacks

    Args:
        nodes: Map of node name -> HyperNode
        nx_graph: The NetworkX directed graph
        bound: Currently bound values
        entrypoints: Optional entry point node names (narrows to forward-reachable)
        selected: Optional output names to produce (narrows to backward-reachable)
        _active_scope: Pre-computed (active_nodes, active_subgraph) to skip
            redundant graph traversal. Internal optimization detail.

    Returns:
        InputSpec with categorized parameters scoped to the active subgraph
    """
    if _active_scope is not None:
        active_nodes, active_subgraph = _active_scope
    else:
        active_nodes, active_subgraph = _compute_active_scope(
            nodes,
            nx_graph,
            entrypoints=entrypoints,
            selected=selected,
        )

    data_subgraph = _data_only_subgraph(active_subgraph)
    edge_produced = get_edge_produced_values(active_subgraph)
    cycle_seed_params = _compute_cycle_seed_params(
        active_nodes,
        data_subgraph,
        edge_produced,
        bound,
        configured_entrypoints=entrypoints or (),
    )

    required, optional = [], []

    for param in _unique_params(active_nodes):
        if param in cycle_seed_params:
            if param in bound:
                optional.append(param)
            else:
                required.append(param)
            continue
        category = _categorize_param(param, edge_produced, bound, active_nodes)
        if category == "required":
            required.append(param)
        elif category == "optional":
            optional.append(param)

    all_bound = _collect_bound_values(active_nodes, bound)

    return InputSpec(
        required=tuple(required),
        optional=tuple(optional),
        entrypoints={},
        bound=all_bound,
    )


def _compute_cycle_seed_params(
    nodes: dict[str, HyperNode],
    data_graph: nx.DiGraph,
    edge_produced: set[str],
    bound: dict[str, Any],
    *,
    configured_entrypoints: tuple[str, ...],
) -> set[str]:
    """Compute cycle bootstrap params required by configured entrypoint nodes."""
    if not configured_entrypoints:
        return set()

    cycle_params = _get_all_cycle_params(nodes, data_graph, edge_produced)
    if not cycle_params:
        return set()

    required: set[str] = set()
    for ep_name in configured_entrypoints:
        node = nodes.get(ep_name)
        if node is None:
            continue
        for param in node.inputs:
            if param not in cycle_params:
                continue
            if param in bound:
                continue
            if _is_interrupt_produced(param, nodes):
                continue
            if node.has_default_for(param):
                continue
            required.add(param)

    return required


def _unique_params(nodes: dict[str, HyperNode]) -> Iterator[str]:
    """Yield each unique parameter name across all nodes."""
    seen: set[str] = set()
    for node in nodes.values():
        for param in node.inputs:
            if param not in seen:
                seen.add(param)
                yield param


def _categorize_param(
    param: str,
    edge_produced: set[str],
    bound: dict[str, Any],
    nodes: dict[str, HyperNode],
) -> str | None:
    """Categorize a non-cycle parameter: 'required', 'optional', or None (edge-produced)."""
    if param in edge_produced:
        return None  # Produced by an edge, not a user input

    if param in bound or _all_consumers_have_default(param, nodes):
        return "optional"

    return "required"


def _is_interrupt_produced(param: str, nodes: dict[str, HyperNode]) -> bool:
    """Check if param is produced by an interrupt node."""
    return any(n.is_interrupt and param in n.outputs for n in nodes.values())


def _all_consumers_have_default(param: str, nodes: dict[str, HyperNode]) -> bool:
    """Check if every node consuming this param has a fallback value."""
    consumers = [node for node in nodes.values() if param in node.inputs]
    if not consumers:
        return False
    return all(node.has_default_for(param) for node in consumers)


def _get_all_cycle_params(
    nodes: dict[str, HyperNode],
    data_graph: nx.DiGraph,
    edge_produced: set[str],
) -> set[str]:
    """Get all parameter names that flow within any cycle."""
    cycles = list(nx.simple_cycles(data_graph))
    if not cycles:
        return set()

    return {param for cycle in cycles for param in _params_flowing_in_cycle(cycle, nodes, edge_produced)}


def _params_flowing_in_cycle(
    cycle: list[str],
    nodes: dict[str, HyperNode],
    edge_produced: set[str],
) -> Iterator[str]:
    """Yield params that flow within a cycle."""
    cycle_nodes = set(cycle)

    for node_name in cycle:
        for param in nodes[node_name].inputs:
            if param not in edge_produced:
                continue
            if any(p in cycle_nodes for p in sources_of(param, nodes)):
                yield param


def _data_only_subgraph(nx_graph: nx.DiGraph) -> nx.DiGraph:
    """Return subgraph containing only data edges (no control edges)."""
    data_edges = [(u, v) for u, v, data in nx_graph.edges(data=True) if data.get("edge_type") == "data"]
    subgraph = nx.DiGraph()
    subgraph.add_nodes_from(nx_graph.nodes())
    subgraph.add_edges_from(data_edges)
    return subgraph


# =============================================================================
# Active Subgraph Computation
# =============================================================================


def _compute_active_scope(
    nodes: dict[str, HyperNode],
    nx_graph: nx.DiGraph,
    *,
    entrypoints: tuple[str, ...] | None = None,
    selected: tuple[str, ...] | None = None,
) -> tuple[dict[str, HyperNode], nx.DiGraph]:
    """Compute active node set and induced subgraph.

    The active set is determined by:
    1. Forward-reachable from entrypoints (or all nodes if none)
    2. Narrowed to backward-reachable from selected outputs
       (with pessimistic gate expansion)
    """
    active = set(nodes)
    if entrypoints is not None:
        active = _active_from_entrypoints(entrypoints, nodes, nx_graph)
    if selected is not None:
        active = _active_from_selection(selected, active, nodes, nx_graph)

    active_nodes = {name: nodes[name] for name in nodes if name in active}
    active_subgraph = nx_graph.subgraph(active).copy()
    return active_nodes, active_subgraph


def _active_from_entrypoints(
    entrypoint_nodes: tuple[str, ...],
    nodes: dict[str, HyperNode],
    nx_graph: nx.DiGraph,
) -> set[str]:
    """Compute active nodes by forward reachability from entrypoints.

    Everything upstream of entrypoints is excluded. Only the entrypoint
    nodes and their downstream descendants are active.
    """
    active = set(entrypoint_nodes)
    for ep in entrypoint_nodes:
        active.update(nx.descendants(nx_graph, ep))
    return active & set(nodes)


def _active_from_selection(
    selected_outputs: tuple[str, ...],
    active_set: set[str],
    nodes: dict[str, HyperNode],
    nx_graph: nx.DiGraph,
) -> set[str]:
    """Narrow active set to nodes needed for selected outputs.

    Walks backward from output producers. When a gate is encountered,
    pessimistically includes ALL its targets and their descendants
    (since routing decisions are made at runtime).
    """
    from hypergraph.nodes.gate import END, GateNode

    selected_set = set(selected_outputs)
    producers = {name for name in active_set if set(nodes[name].outputs) & selected_set}
    if not producers:
        # No active node produces the selected outputs — return empty set.
        # graph.select() validates output names at construction time; runtime
        # select names are validated in resolve_runtime_selected(). This path
        # only triggers when entrypoints exclude the output's producer, in which
        # case no nodes are needed for this selection.
        return set()

    sub = nx_graph.subgraph(active_set)
    needed: set[str] = set()
    worklist = list(producers)

    while worklist:
        name = worklist.pop()
        if name in needed or name not in active_set:
            continue
        needed.add(name)

        # Backward: include predecessors
        for pred in sub.predecessors(name):
            if pred not in needed:
                worklist.append(pred)

        # Pessimistic gate expansion: all targets might execute
        node = nodes.get(name)
        if isinstance(node, GateNode):
            for target in node.targets:
                if target is END or target not in active_set:
                    continue
                if target not in needed:
                    worklist.append(target)
                    for desc in nx.descendants(sub, target):
                        if desc not in needed:
                            worklist.append(desc)

    return needed


def _collect_bound_values(
    nodes: dict[str, HyperNode],
    bound: dict[str, Any],
) -> dict[str, Any]:
    """Collect all bound values from graph and nested GraphNodes.

    When a graph contains GraphNodes with bound values, those values need to be
    accessible at runtime for parameter resolution. This function merges:
    1. The graph's own bound values
    2. Bound values from all nested GraphNodes (recursively)

    Args:
        nodes: Map of node name -> HyperNode
        bound: Bound values from the current graph

    Returns:
        Merged dict of all bound values (current graph + nested graphs)
    """
    from hypergraph.nodes.graph_node import GraphNode

    # Start with current graph's bound values
    all_bound = dict(bound)
    nested_candidates: dict[str, list[Any]] = {}

    # Merge bound values from nested GraphNodes
    for node in nodes.values():
        if isinstance(node, GraphNode):
            # Get bound values from the inner graph
            inner_bound = node.graph.inputs.bound
            # Merge unique nested bindings only. If sibling nested graphs bind
            # the same key to different values, that key is ambiguous at the
            # outer graph level and should stay out of graph.inputs.bound.
            for key, value in inner_bound.items():
                public_key = node.map_input_name_from_original(key)
                if public_key in all_bound:
                    continue
                nested_candidates.setdefault(public_key, []).append(value)

    for key, candidates in nested_candidates.items():
        if len(candidates) == 1 or _all_values_equal(candidates):
            all_bound[key] = candidates[0]

    return all_bound


def _all_values_equal(values: list[Any]) -> bool:
    """Best-effort equality check for merged nested bound values."""
    if len(values) <= 1:
        return True
    first = values[0]
    for value in values[1:]:
        if first is value:
            continue
        try:
            equal = first == value
            if hasattr(equal, "__iter__"):
                if not all(equal):
                    return False
            elif not bool(equal):
                return False
        except Exception:
            return False
    return True
