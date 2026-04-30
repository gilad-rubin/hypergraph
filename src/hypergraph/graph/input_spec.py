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
    all_bound = _collect_bound_values(active_nodes, bound)
    cycle_seed_params = _compute_cycle_seed_params(
        active_nodes,
        data_subgraph,
        edge_produced,
        all_bound,
        configured_entrypoints=entrypoints or (),
    )

    required, optional = [], []
    declared = _names_declared_at_scope(active_nodes)

    for addressed, original, _source in _addressed_params(active_nodes, declared):
        if original in cycle_seed_params:
            if addressed in all_bound:
                optional.append(addressed)
            else:
                required.append(addressed)
            continue
        category = _categorize_addressed_param(addressed, original, edge_produced, all_bound, active_nodes)
        if category == "required":
            required.append(addressed)
        elif category == "optional":
            optional.append(addressed)

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


def _names_declared_at_scope(nodes: dict[str, HyperNode]) -> set[str]:
    """Names declared directly at this graph's scope.

    A name is declared at this scope if any node here can produce it (leaf
    output OR GraphNode output, since both surface at this scope) or if a
    leaf node here consumes it. GraphNode inputs do NOT count -- they are
    private to the subscope unless declared by something else here.

    Two siblings GraphNodes that happen to share an input name therefore stay
    private to each. But a GraphNode whose output flows (via edge) to a sibling
    GraphNode's same-named input auto-links, because the producer's output is
    declared at this scope.
    """
    from hypergraph.nodes.graph_node import GraphNode

    names: set[str] = set()
    for node in nodes.values():
        if isinstance(node, GraphNode):
            names.update(node.outputs)
        else:
            names.update(node.inputs)
            names.update(node.outputs)
    return names


def _addressed_params(
    nodes: dict[str, HyperNode],
    declared: set[str],
) -> Iterator[tuple[str, str, str | None]]:
    """Yield (addressed_name, original_name, source_node_name_or_None) per input.

    Inputs declared at this scope are emitted once with their flat name and no
    source. Inputs private to a GraphNode subscope are emitted as dot-paths.
    """
    from hypergraph.nodes.graph_node import GraphNode

    seen_flat: set[str] = set()
    for node_name, node in nodes.items():
        if isinstance(node, GraphNode):
            for param in node.inputs:
                if param in declared:
                    if param not in seen_flat:
                        seen_flat.add(param)
                        yield (param, param, None)
                else:
                    yield (f"{node_name}.{param}", param, node_name)
        else:
            for param in node.inputs:
                if param not in seen_flat:
                    seen_flat.add(param)
                    yield (param, param, None)


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


def _categorize_addressed_param(
    addressed: str,
    original: str,
    edge_produced: set[str],
    bound: dict[str, Any],
    nodes: dict[str, HyperNode],
) -> str | None:
    """Categorize an addressed parameter: 'required', 'optional', or None (edge-produced)."""
    # Edge-produced is checked against the original name, since edges act in scope.
    if original in edge_produced and addressed == original:
        return None

    if addressed in bound:
        return "optional"

    if addressed == original and _all_consumers_have_default(original, nodes):
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
    """Collect all bound values from graph and nested GraphNodes under lexical scope.

    Inner binds surface as dot-pathed keys (``"<graphnode_name>.<public_name>"``)
    on the outer bound dict. If an inner bind's public name is declared at this
    scope (would auto-link to an ancestor's same-named input), the bind would be
    silently overridden by the ancestor's value at run time -- so this is a
    build-time error pointing to both the bind site and the ancestor scope.

    Args:
        nodes: Map of node name -> HyperNode
        bound: Bound values from the current graph

    Returns:
        Merged dict of all bound values (current graph + nested graphs)

    Raises:
        GraphConfigError: when a nested bind is shadowed by an ancestor-declared name.
    """
    from hypergraph.graph.validation import GraphConfigError
    from hypergraph.nodes.graph_node import GraphNode

    all_bound = dict(bound)
    declared = _names_declared_at_scope(nodes)

    for node_name, node in nodes.items():
        if not isinstance(node, GraphNode):
            continue
        for inner_key, value in node.graph.inputs.bound.items():
            public_key = node.map_input_name_from_original(inner_key)
            if public_key in declared:
                raise GraphConfigError(
                    f"Bind on '{node_name}.{public_key}' is shadowed by '{public_key}' "
                    f"declared at this scope (a node here consumes or produces it). "
                    f"At run time the parent's value would silently override the bind. "
                    f"Fix: either remove the bind on the inner subgraph, or rename "
                    f"the input via with_inputs(...) so it no longer matches the ancestor."
                )
            all_bound[f"{node_name}.{public_key}"] = value

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
