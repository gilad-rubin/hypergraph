"""Input scope analysis and output visibility.

Determines where INPUT nodes should be placed (root vs. inside a container)
and which outputs are externally consumed (visible outside their container).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypergraph.viz._common import (
    get_parent,
    is_descendant_of,
    is_node_visible,
)

if TYPE_CHECKING:
    import networkx as nx


def get_deepest_consumers(param: str, flat_graph: nx.DiGraph) -> list[str]:
    """Get the deepest (non-container) consumers of a parameter.

    When a container (GRAPH node) and its internal nodes both list a parameter
    as an input, we return only the internal nodes - they are the "actual"
    consumers.
    """
    all_consumers = []
    for node_id, attrs in flat_graph.nodes(data=True):
        if param in attrs.get("inputs", ()):
            all_consumers.append(node_id)

    if len(all_consumers) <= 1:
        return all_consumers

    filtered = []
    for consumer in all_consumers:
        is_superseded = False
        for other in all_consumers:
            if other == consumer:
                continue
            if is_descendant_of(other, consumer, flat_graph):
                is_superseded = True
                break
        if not is_superseded:
            filtered.append(consumer)

    return filtered


def get_ancestor_chain(node_id: str, flat_graph: nx.DiGraph) -> list[str]:
    """Get the chain of container ancestors for a node, from immediate to root."""
    ancestors = []
    current = node_id
    while current is not None:
        parent = get_parent(current, flat_graph)
        if parent is not None:
            ancestors.append(parent)
        current = parent
    return ancestors


def find_deepest_common_container(ancestor_chains: list[list[str]]) -> str | None:
    """Find the deepest common container across all ancestor chains."""
    if not ancestor_chains:
        return None

    if any(not chain for chain in ancestor_chains):
        return None

    first_chain = ancestor_chains[0]
    for container in first_chain:
        if all(container in chain for chain in ancestor_chains[1:]):
            return container

    return None


def compute_input_scope(
    param: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> str | None:
    """Determine which container (if any) should own this INPUT node.

    An INPUT should be placed inside a container ONLY if ALL its consumers
    are inside that same container (or its descendants). If any consumer is
    at root level or in a different container, the INPUT stays at root.
    """
    consumers = get_deepest_consumers(param, flat_graph)

    if not consumers:
        return None

    consumer_containers: set[str | None] = set()
    for consumer in consumers:
        parent = get_parent(consumer, flat_graph)
        if parent is None:
            return None
        chain = get_ancestor_chain(consumer, flat_graph)
        root_container = chain[-1] if chain else parent
        consumer_containers.add(root_container)

    if len(consumer_containers) > 1:
        return None

    common_container = next(iter(consumer_containers))

    chain = [common_container, *get_ancestor_chain(common_container, flat_graph)]
    for container in chain:
        if expansion_state.get(container, False):
            return container

    return None


def compute_deepest_input_scope(
    param: str,
    flat_graph: nx.DiGraph,
) -> str | None:
    """Find the deepest common container of all consumers (ignoring expansion state).

    This is used for JavaScript to walk up at runtime when expansion state changes.
    """
    consumers = get_deepest_consumers(param, flat_graph)

    if not consumers:
        return None

    ancestor_chains = [get_ancestor_chain(c, flat_graph) for c in consumers]
    return find_deepest_common_container(ancestor_chains)


def is_output_externally_consumed(
    output_param: str,
    source_node: str,
    flat_graph: nx.DiGraph,
) -> bool:
    """Check if an output is consumed by any node outside its source's container."""
    source_parent = get_parent(source_node, flat_graph)
    source_attrs = flat_graph.nodes.get(source_node, {})

    if source_parent is None and source_attrs.get("node_type") != "GRAPH":
        return True

    source_container = source_node if source_attrs.get("node_type") == "GRAPH" else source_parent

    for node_id, attrs in flat_graph.nodes(data=True):
        if output_param in attrs.get("inputs", ()):
            if not is_descendant_of(node_id, source_container, flat_graph):
                return True

    return False


def build_graph_output_visibility(flat_graph: nx.DiGraph) -> dict[str, set[str]]:
    """Build mapping of GRAPH node -> externally consumed outputs."""
    visibility: dict[str, set[str]] = {}
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("node_type") != "GRAPH":
            continue
        visible_outputs = {
            output_name
            for output_name in attrs.get("outputs", ())
            if is_output_externally_consumed(output_name, node_id, flat_graph)
        }
        # Leaf GRAPH nodes are still end-user visible branch outcomes.
        # If none of their outputs are externally consumed, keep terminal outputs
        # visible so collapsed containers don't appear output-less.
        if not visible_outputs and flat_graph.out_degree(node_id) == 0:
            visible_outputs = set(attrs.get("outputs", ()))
        visibility[node_id] = visible_outputs
    return visibility


def find_container_entrypoints(
    container_id: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> list[str]:
    """Find entry point nodes inside a container for control edge routing."""
    direct_children = [
        node_id
        for node_id, attrs in flat_graph.nodes(data=True)
        if attrs.get("parent") == container_id
    ]

    internal_outputs = set()
    for node_id in direct_children:
        attrs = flat_graph.nodes.get(node_id, {})
        for output in attrs.get("outputs", ()):
            internal_outputs.add(output)

    entrypoints = []
    for node_id in direct_children:
        attrs = flat_graph.nodes.get(node_id, {})
        inputs = set(attrs.get("inputs", ()))

        consumes_internal = bool(inputs & internal_outputs)

        if not consumes_internal:
            if is_node_visible(node_id, flat_graph, expansion_state):
                entrypoints.append(node_id)

    return entrypoints


def find_container_exit_points(
    container_id: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> list[str]:
    """Find exit point nodes inside a container for data edge routing."""
    exit_points = []
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("parent") != container_id:
            continue
        if not attrs.get("outputs", ()):
            continue
        if is_node_visible(node_id, flat_graph, expansion_state):
            exit_points.append(node_id)

    return exit_points


def find_internal_producer_for_output(
    container_id: str,
    output_name: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> str | None:
    """Find the internal node that produces the data that becomes `output_name`.

    Handles the `with_outputs` rename case: when a container exposes
    `retrieval_eval_results` but internally `compute_recall` produces
    `retrieval_eval_result`, we need to find `compute_recall`.
    """
    internal_producers: dict[str, str] = {}
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("parent") != container_id:
            continue
        for output in attrs.get("outputs", ()):
            internal_producers[output] = node_id

    internal_consumed: set[str] = set()
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("parent") != container_id:
            continue
        for inp in attrs.get("inputs", ()):
            if inp in internal_producers:
                internal_consumed.add(inp)

    terminal_outputs = {
        out: prod
        for out, prod in internal_producers.items()
        if out not in internal_consumed
    }

    if output_name in terminal_outputs:
        producer = terminal_outputs[output_name]
        if is_node_visible(producer, flat_graph, expansion_state):
            return producer

    # Fuzzy fallback: with_outputs renames can differ slightly (e.g. pluralization).
    # Substring matching is intentionally loose to maximize recall in visualization.
    for internal_out, producer in terminal_outputs.items():
        if internal_out in output_name or output_name in internal_out:
            if is_node_visible(producer, flat_graph, expansion_state):
                return producer

    return None
