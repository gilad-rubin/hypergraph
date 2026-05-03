"""Build the compact graph IR from a flat NetworkX graph.

This is the single entry point that replaces the legacy
`render_graph` (2^N precompute) and `render_graph_single_state` paths.
Frontends derive expansion state from the IR; Python does not enumerate
all 2^N states ahead of time.
"""

from __future__ import annotations

import networkx as nx

from hypergraph.viz._common import (
    build_param_to_consumer_map,
    compute_exclusive_data_edges,
    disambiguate_external_input_ids,
    external_input_display_name,
    get_expandable_nodes,
)
from hypergraph.viz.ir_schema import GraphIR, IREdge, IRExternalInput, IRNode
from hypergraph.viz.renderer._format import format_type
from hypergraph.viz.renderer.nodes import build_input_groups, get_param_type
from hypergraph.viz.renderer.scope import (
    build_graph_output_visibility,
    compute_deepest_input_scope,
    get_deepest_consumers,
    is_output_externally_consumed,
)


def build_graph_ir(flat_graph: nx.DiGraph) -> GraphIR:
    input_spec = flat_graph.graph.get("input_spec", {})
    bound_params = set(input_spec.get("bound", {}).keys())
    shared_params = set(flat_graph.graph.get("shared", ()))

    hidden_nodes = {node_id for node_id, attrs in flat_graph.nodes(data=True) if attrs.get("hide", False)}
    nodes = [
        _build_ir_node(node_id, attrs, bound_params, flat_graph) for node_id, attrs in flat_graph.nodes(data=True) if node_id not in hidden_nodes
    ]
    exclusive_edges = compute_exclusive_data_edges(flat_graph)
    back_edges = _find_back_edges(flat_graph)
    edges = [
        _build_ir_edge(src, tgt, attrs, flat_graph, exclusive_edges, back_edges)
        for src, tgt, attrs in flat_graph.edges(data=True)
        if src not in hidden_nodes and tgt not in hidden_nodes
    ]
    # Use the legacy grouping logic so multi-param consumers collapse into
    # a single INPUT_GROUP. show_bounded_inputs=True here so bound params
    # are shipped in the IR; the frontend filters them per-render.
    # use_deepest=True so grouping is by the actual internal consumers
    # (state-independent fact) rather than the per-expansion-state visible
    # consumers — the IR must be a single source of truth across states.
    param_to_consumers = build_param_to_consumer_map(flat_graph, expansion_state={}, use_deepest=True)
    groups = build_input_groups(
        input_spec=input_spec,
        param_to_consumers=param_to_consumers,
        bound_params=bound_params,
        shared_params=shared_params,
        show_bounded_inputs=True,
    )
    # Pre-compute the synthetic-id mapping so colliding leaf names
    # (e.g. ``A.x`` and ``B.x``) get unique ``input_<id>`` ids.
    id_for_param = disambiguate_external_input_ids([list(group["params"]) for group in groups])
    external_inputs = [_build_input_group(group, flat_graph, id_for_param) for group in groups]

    configured_entrypoints = tuple(flat_graph.graph.get("configured_entrypoints") or ())
    visibility = build_graph_output_visibility(flat_graph)
    graph_output_visibility = {node_id: tuple(sorted(outputs)) for node_id, outputs in visibility.items()}

    return GraphIR(
        nodes=nodes,
        edges=edges,
        expandable_nodes=get_expandable_nodes(flat_graph),
        external_inputs=external_inputs,
        configured_entrypoints=configured_entrypoints,
        graph_output_visibility=graph_output_visibility,
    )


def _build_input_group(
    group: dict,
    flat_graph: nx.DiGraph,
    id_for_param: dict[str, str] | None = None,
) -> IRExternalInput:
    """Convert a build_input_groups dict into an IRExternalInput.

    Multi-param groups (e.g. one consumer takes both alpha and beta)
    become a single IRExternalInput with len(params) > 1, which
    scene_builder renders as INPUT_GROUP."""
    raw_params = tuple(group["params"])
    consumers: list[str] = []
    seen: set[str] = set()
    for param in raw_params:
        # Resolve consumers using the (possibly dot-pathed) full name so
        # the dot-path walk in get_deepest_consumers can address nested
        # subgraphs (issue #94). Deduplicate while preserving order.
        for consumer in get_deepest_consumers(param, flat_graph):
            if consumer not in seen:
                seen.add(consumer)
                consumers.append(consumer)
    deepest_owner = compute_deepest_input_scope(raw_params[0], flat_graph)
    # Type hints are looked up by the dot-pathed name first, then by the
    # leaf — leaf consumers carry the type under their scope-local name.
    type_hints: list[str | None] = []
    for p in raw_params:
        param_type = get_param_type(p, flat_graph)
        if param_type is None:
            param_type = get_param_type(external_input_display_name(p), flat_graph)
        type_hints.append(format_type(param_type))
    # Display labels are the leaf segments; synthetic ids fall back to
    # the full dot-path when leaf names collide.
    display_params = tuple(external_input_display_name(p) for p in raw_params)
    id_segments = tuple((id_for_param or {}).get(p, external_input_display_name(p)) for p in raw_params)
    return IRExternalInput(
        params=display_params,
        deepest_owner=deepest_owner,
        consumers=tuple(consumers),
        type_hints=tuple(type_hints),
        is_bound=bool(group.get("is_bound", False)),
        id_segments=id_segments,
    )


def _build_ir_edge(
    src: str,
    tgt: str,
    attrs: dict,
    flat_graph: nx.DiGraph,
    exclusive_edges: set[tuple[str, str, str]],
    back_edges: set[tuple[str, str]],
) -> IREdge:
    """Pre-compute the source-when-expanded / target-when-expanded rewrites
    so the JS scene_builder can re-route edges on container expansion
    without re-walking the graph."""
    source_when_expanded: str | None = None
    target_when_expanded: str | None = None

    edge_type = attrs.get("edge_type", "data")
    value_names = tuple(attrs.get("value_names", ()))

    src_attrs = flat_graph.nodes.get(src, {})
    if src_attrs.get("node_type") == "GRAPH":
        for value_name in value_names:
            internal = _find_deepest_internal_producer(src, value_name, flat_graph)
            if internal is not None:
                source_when_expanded = internal
                break

    tgt_attrs = flat_graph.nodes.get(tgt, {})
    if tgt_attrs.get("node_type") == "GRAPH":
        for value_name in value_names:
            internal = _find_deepest_internal_consumer(tgt, value_name, flat_graph)
            if internal is not None:
                target_when_expanded = internal
                break
        # Control edges to a container should re-route to the container's
        # entrypoint when the container is expanded — value_names is empty
        # for control edges, so the producer/consumer search above is a no-op.
        if target_when_expanded is None and edge_type == "control":
            target_when_expanded = _first_container_entrypoint(tgt, flat_graph)

    label = _branch_label_for_edge(src_attrs, tgt) if edge_type == "control" else None

    exclusive = edge_type == "data" and any((src, tgt, value_name) in exclusive_edges for value_name in value_names)

    return IREdge(
        source=src,
        target=tgt,
        edge_type=edge_type,
        source_when_expanded=source_when_expanded,
        target_when_expanded=target_when_expanded,
        value_names=value_names,
        label=label,
        exclusive=exclusive,
        is_back_edge=(src, tgt) in back_edges,
    )


def _branch_label_for_edge(src_attrs: dict, target: str) -> str | None:
    """Read ``branch_data`` off the gate source and return the user-facing
    branch label for the edge to ``target`` (e.g. "True"/"False")."""
    branch_data = src_attrs.get("branch_data") or {}
    if "when_true" in branch_data and target == branch_data["when_true"]:
        return "True"
    if "when_false" in branch_data and target == branch_data["when_false"]:
        return "False"
    targets = branch_data.get("targets")
    if isinstance(targets, dict):
        for label, t in targets.items():
            if t == target:
                return str(label)
    return None


def _find_back_edges(flat_graph: nx.DiGraph) -> set[tuple[str, str]]:
    """Return the set of execution-subgraph edges that close a cycle.

    Mirrors the legacy DFS-based back-edge detector. Synthetic edges
    (input/data/start/end) and edges touching synthetic nodes are
    excluded — they don't participate in cycle closure.
    """
    SYNTHETIC_PREFIXES = ("input_", "input_group_", "data_")
    SYNTHETIC_NODES = {"__start__", "__end__"}

    def is_synthetic(node_id: str) -> bool:
        return node_id in SYNTHETIC_NODES or node_id.startswith(SYNTHETIC_PREFIXES)

    adjacency: dict[str, list[tuple[str, tuple[str, str]]]] = {}
    for src, tgt, attrs in flat_graph.edges(data=True):
        if attrs.get("edge_type", "data") not in ("data", "control", "ordering"):
            continue
        if is_synthetic(src) or is_synthetic(tgt):
            continue
        adjacency.setdefault(src, []).append((tgt, (src, tgt)))

    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    back: set[tuple[str, str]] = set()

    def dfs(start: str) -> None:
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = GREY
        while stack:
            node, idx = stack[-1]
            children = adjacency.get(node, ())
            if idx < len(children):
                stack[-1] = (node, idx + 1)
                tgt, edge_key = children[idx]
                state = color.get(tgt, WHITE)
                if state == GREY:
                    back.add(edge_key)
                elif state == WHITE:
                    color[tgt] = GREY
                    stack.append((tgt, 0))
            else:
                color[node] = BLACK
                stack.pop()

    for node in adjacency:
        if color.get(node, WHITE) == WHITE:
            dfs(node)
    return back


def _first_container_entrypoint(container_id: str, flat_graph: nx.DiGraph) -> str | None:
    """State-independent first entrypoint of a container — the first
    direct child whose inputs are not produced by any sibling. Used to
    re-route control edges when the container is expanded."""
    direct_children = [node_id for node_id, attrs in flat_graph.nodes(data=True) if attrs.get("parent") == container_id]
    if not direct_children:
        return None

    sibling_outputs: set[str] = set()
    for child_id in direct_children:
        sibling_outputs.update(flat_graph.nodes.get(child_id, {}).get("outputs", ()))

    for child_id in direct_children:
        child_inputs = set(flat_graph.nodes.get(child_id, {}).get("inputs", ()))
        if not child_inputs & sibling_outputs:
            return child_id

    # Cyclic container — no pure-external child exists; fall back to the
    # first declared child so callers get a stable target.
    return direct_children[0]


def _find_deepest_internal_producer(container_id: str, value_name: str, flat_graph: nx.DiGraph) -> str | None:
    """Walk down the container tree, find the deepest descendant that
    produces value_name as an output. Falls back to fuzzy substring
    matching to handle the ``with_outputs`` rename case (e.g. container
    exposes ``recall_scores`` but the internal node produces
    ``recall_score``)."""
    descendants = [(node_id, attrs) for node_id, attrs in flat_graph.nodes(data=True) if _is_descendant(node_id, container_id, flat_graph)]

    candidates = [node_id for node_id, attrs in descendants if value_name in attrs.get("outputs", ())]
    if candidates:
        return max(candidates, key=lambda c: _depth_below(c, container_id, flat_graph))

    fuzzy = [node_id for node_id, attrs in descendants for out in attrs.get("outputs", ()) if out in value_name or value_name in out]
    if fuzzy:
        return max(fuzzy, key=lambda c: _depth_below(c, container_id, flat_graph))
    return None


def _find_deepest_internal_consumer(container_id: str, value_name: str, flat_graph: nx.DiGraph) -> str | None:
    """Mirror of :func:`_find_deepest_internal_producer` for inputs.

    Falls back to fuzzy substring matching to handle the
    ``map_over`` / ``with_inputs`` rename case (e.g. the outer edge
    carries ``eval_pairs`` but the per-item internal node consumes
    ``eval_pair``)."""
    descendants = [
        (node_id, attrs)
        for node_id, attrs in flat_graph.nodes(data=True)
        if _is_descendant(node_id, container_id, flat_graph) and attrs.get("node_type") != "GRAPH"
    ]

    candidates = [node_id for node_id, attrs in descendants if value_name in attrs.get("inputs", ())]
    if candidates:
        return max(candidates, key=lambda c: _depth_below(c, container_id, flat_graph))

    fuzzy = [node_id for node_id, attrs in descendants for inp in attrs.get("inputs", ()) if inp in value_name or value_name in inp]
    if fuzzy:
        return max(fuzzy, key=lambda c: _depth_below(c, container_id, flat_graph))
    return None


def _is_descendant(node_id: str, ancestor_id: str, flat_graph: nx.DiGraph) -> bool:
    current = flat_graph.nodes.get(node_id, {}).get("parent")
    while current is not None:
        if current == ancestor_id:
            return True
        current = flat_graph.nodes.get(current, {}).get("parent")
    return False


def _depth_below(node_id: str, ancestor_id: str, flat_graph: nx.DiGraph) -> int:
    depth = 0
    current = flat_graph.nodes.get(node_id, {}).get("parent")
    while current is not None and current != ancestor_id:
        depth += 1
        current = flat_graph.nodes.get(current, {}).get("parent")
    return depth


def _build_ir_node(node_id: str, attrs: dict, bound_params: set[str], flat_graph: nx.DiGraph) -> IRNode:
    output_types = attrs.get("output_types", {})
    node_type = attrs.get("node_type", "FUNCTION")
    local_name = node_id.rsplit("/", 1)[-1]
    outputs = tuple(
        {
            "name": out,
            "type": format_type(output_types.get(out)),
            # Gate nodes expose an internal routing-signal output (``_<gate_name>``)
            # that runtime uses for bookkeeping. It must not surface as a
            # DATA scene node.
            "is_gate_internal": node_type == "BRANCH" and out == f"_{local_name}",
            # True when no node outside the source's container consumes this
            # output; the renderer styles such DATA nodes differently and
            # filters them out of the collapsed-container surface.
            "internal_only": not is_output_externally_consumed(out, node_id, flat_graph),
        }
        for out in attrs.get("outputs", ())
    )

    input_types = attrs.get("input_types", {})
    has_defaults = attrs.get("has_defaults", {})
    inputs = tuple(
        {
            "name": param,
            "type": format_type(input_types.get(param)),
            "has_default": has_defaults.get(param, False),
            "is_bound": param in bound_params,
        }
        for param in attrs.get("inputs", ())
    )

    return IRNode(
        id=node_id,
        node_type=attrs.get("node_type", "FUNCTION"),
        parent=attrs.get("parent"),
        label=attrs.get("label"),
        outputs=outputs,
        inputs=inputs,
        branch_data=attrs.get("branch_data"),
    )
