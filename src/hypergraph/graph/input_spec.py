"""Input specification calculation for graphs.

This module contains the InputSpec dataclass and logic for computing
which parameters are required, optional, or cycle entrypoints.
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
    - entrypoints: dict mapping cycle node name -> tuple of params needed
      to enter the cycle at that node. Pick ONE entrypoint per cycle.
    """

    required: tuple[str, ...]
    optional: tuple[str, ...]
    entrypoints: dict[str, tuple[str, ...]]
    bound: dict[str, Any]

    @property
    def all(self) -> tuple[str, ...]:
        """All input names (required + optional + entrypoint params)."""
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

    Returns:
        InputSpec with categorized parameters scoped to the active subgraph
    """
    active_nodes, active_subgraph = _compute_active_scope(
        nodes,
        nx_graph,
        entrypoints=entrypoints,
        selected=selected,
    )

    edge_produced = get_edge_produced_values(active_subgraph)
    cycle_entrypoints = _compute_entrypoints(active_nodes, active_subgraph, edge_produced, bound)
    all_entry_params = {p for params in cycle_entrypoints.values() for p in params}

    required, optional = [], []

    for param in _unique_params(active_nodes):
        if param in all_entry_params:
            continue  # Handled by entrypoints
        category = _categorize_param(param, edge_produced, bound, active_nodes)
        if category == "required":
            required.append(param)
        elif category == "optional":
            optional.append(param)

    all_bound = _collect_bound_values(active_nodes, bound)

    return InputSpec(
        required=tuple(required),
        optional=tuple(optional),
        entrypoints=cycle_entrypoints,
        bound=all_bound,
    )


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

    if param in bound or _any_node_has_default(param, nodes):
        return "optional"

    return "required"


def _is_interrupt_produced(param: str, nodes: dict[str, HyperNode]) -> bool:
    """Check if param is produced by an interrupt node."""
    return any(n.is_interrupt and param in n.outputs for n in nodes.values())


def _any_node_has_default(param: str, nodes: dict[str, HyperNode]) -> bool:
    """Check if any node consuming this param has a default value."""
    return any(param in node.inputs and node.has_default_for(param) for node in nodes.values())


def _compute_entrypoints(
    nodes: dict[str, HyperNode],
    nx_graph: nx.DiGraph,
    edge_produced: set[str],
    bound: dict[str, Any],
) -> dict[str, tuple[str, ...]]:
    """Compute entrypoints for each cycle in the graph.

    For each SCC (strongly connected component) with >1 node or a self-loop,
    find non-gate nodes and compute which cycle-params they need as inputs.
    Each such node is an entrypoint — the user picks one per cycle.

    Returns:
        Dict mapping node name -> tuple of cycle params needed to enter there.
        Empty dict for DAGs (no cycles).
    """
    from hypergraph.nodes.gate import GateNode

    data_graph = _data_only_subgraph(nx_graph)
    cycle_params = _get_all_cycle_params(nodes, data_graph, edge_produced)

    if not cycle_params:
        return {}

    sccs = list(nx.strongly_connected_components(data_graph))
    entrypoints: dict[str, tuple[str, ...]] = {}

    for scc in sccs:
        if not _is_cyclic_scc(scc, data_graph):
            continue

        for node_name in sorted(scc):  # sorted for deterministic order
            node = nodes[node_name]
            if isinstance(node, GateNode):
                continue  # Gates control cycles, not start them

            # Params this node needs that are cycle-produced
            # (minus bound, minus interrupt-produced, minus defaulted)
            needed = tuple(
                p
                for p in node.inputs
                if p in cycle_params and p not in bound and not _is_interrupt_produced(p, nodes) and not node.has_default_for(p)
            )
            if needed:  # Only include if node needs user-provided cycle params
                entrypoints[node_name] = needed

    return entrypoints


def _is_cyclic_scc(scc: set[str], graph: nx.DiGraph) -> bool:
    """Check if an SCC represents a cycle (>1 node, or self-loop)."""
    if len(scc) > 1:
        return True
    node = next(iter(scc))
    return graph.has_edge(node, node)


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
    if entrypoints:
        active = _active_from_entrypoints(entrypoints, nodes, nx_graph)
    if selected:
        active = _active_from_selection(selected, active, nodes, nx_graph)

    active_nodes = {name: nodes[name] for name in nodes if name in active}
    active_subgraph = nx_graph.subgraph(active)
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
        # No active node produces the selected outputs — return full active set
        # as a graceful fallback. graph.select() validates output names at
        # construction time, so this path only triggers via runtime select with
        # entrypoints that exclude the producer (defense-in-depth).
        return active_set

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

    # Merge bound values from nested GraphNodes
    for node in nodes.values():
        if isinstance(node, GraphNode):
            # Get bound values from the inner graph
            inner_bound = node.graph.inputs.bound
            # Merge into all_bound (current graph's values take precedence)
            for key, value in inner_bound.items():
                if key not in all_bound:
                    all_bound[key] = value

    return all_bound
