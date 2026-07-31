"""Build the compact graph IR from a flat NetworkX graph.

This is the single entry point that replaces the legacy
`render_graph` (2^N precompute) and `render_graph_single_state` paths.
Frontends derive expansion state from the IR; Python does not enumerate
all 2^N states ahead of time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import networkx as nx

from hypergraph.viz._common import (
    _compute_mutex_groups,
    _is_pair_mutex,
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
    compute_container_entrypoints,
    compute_deepest_input_scope,
    find_back_edges,
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
    # ``container -> {item-field names}`` fed by a fan-out edge's mapped item.
    # An inner input whose name is in this set is supplied by the parent's list
    # column through the map, not by a genuine external supplier — the fan-out
    # edge re-routes into its pill on expansion, and the pill is marked map-fed.
    map_fed_fields = _map_fed_fields(flat_graph)
    exclusive_edges = compute_exclusive_data_edges(flat_graph)
    mutex_groups = _compute_mutex_groups(flat_graph)
    back_edges = find_back_edges(flat_graph)
    # The ONE canonical container-entrypoint derivation (D14, #211): stamped
    # on the IR below and reused for the entrypoint fallback of edges that
    # enter an expanded container.
    container_entrypoints = compute_container_entrypoints(flat_graph)
    container_transits = compute_container_transits(flat_graph)
    edges = [
        _build_ir_edge(src, tgt, attrs, flat_graph, exclusive_edges, mutex_groups, back_edges, container_entrypoints)
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
    external_inputs = [_build_input_group(group, flat_graph, id_for_param, map_fed_fields) for group in groups]

    # Re-route each identity-mode fan-out edge, when its mapped container is
    # expanded, into the inner INPUT pill(s) fed by an item field — instead of
    # the container entrypoint (#169). Done here (not in _build_ir_edge) because
    # it needs the built ``external_inputs`` to resolve field -> pill id, which
    # keeps the edge target guaranteed to be a real scene node.
    edges = _reroute_fanout_edges_to_field_pills(edges, flat_graph, external_inputs, map_fed_fields)

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
        container_entrypoints=container_entrypoints,
        container_transits=container_transits,
    )


def _map_fed_fields(flat_graph: nx.DiGraph) -> dict[str, set[str]]:
    """``container_id -> {item-field names}`` from fan-out edges' ``map_fields``.

    ``HyperTable.visualize`` stamps ``map_fields`` (the mapped item's schema
    field names) on each injected fan-out edge; the target is the mapped
    container. Only fields the container actually consumes matter — a schema
    field with no matching input (e.g. ``page_id``) is not a viz input, so it
    is intersected out here.

    Matching is against the mapped container's OWN input ports (parent-facing
    names), not descendants' inner inputs. That is the name space the item
    fields and the external-input pills share, so a container that renames an
    inner input (``with_inputs(chunk="page_text")`` / ``rename_inputs``) still
    matches on the parent-facing ``page_text`` rather than the renamed ``chunk``.
    """
    result: dict[str, set[str]] = {}
    for _src, tgt, attrs in flat_graph.edges(data=True):
        fields = attrs.get("map_fields")
        if not fields:
            continue
        container_inputs = set(flat_graph.nodes.get(tgt, {}).get("inputs", ()))
        matched = {f for f in fields if f in container_inputs}
        if matched:
            result.setdefault(tgt, set()).update(matched)
    return result


def _owner_maps_field(
    deepest_owner: str | None,
    field: str | None,
    map_fed_fields: dict[str, set[str]],
    flat_graph: nx.DiGraph,
) -> bool:
    """True when ``field`` is an item field of a mapped container that owns the
    input — the input's ``deepest_owner`` or any ancestor of it. Handles a field
    consumer nested one or more graphs below the mapped container."""
    if field is None:
        return False
    current = deepest_owner
    while current is not None:
        if field in map_fed_fields.get(current, set()):
            return True
        current = flat_graph.nodes.get(current, {}).get("parent")
    return False


def _mapped_container_for(owner: str | None, field: str, map_fed_fields: dict[str, set[str]], flat_graph: nx.DiGraph) -> str | None:
    """The mapped container (``owner`` or an ancestor) whose map_fields hold ``field``."""
    current = owner
    while current is not None:
        if field in map_fed_fields.get(current, set()):
            return current
        current = flat_graph.nodes.get(current, {}).get("parent")
    return None


def _reroute_fanout_edges_to_field_pills(
    edges: list[IREdge],
    flat_graph: nx.DiGraph,
    external_inputs: list[IRExternalInput],
    map_fed_fields: dict[str, set[str]],
) -> list[IREdge]:
    """Point each map-fed fan-out edge at its item-field INPUT pill(s) on expand.

    For a fan-out edge into a container whose ``map_fields`` name inner inputs,
    the expanded target becomes the pill(s) for those fields (``input_page_text``)
    rather than the entrypoint — so the visible flow is
    ``segment_pages ──pages──▶ [page_text] ──▶ embed_page``. A field consumer
    nested a graph deeper than the mapped container still resolves: the pill is
    keyed under the mapped container it belongs to, not its ``deepest_owner``.
    Falls back to the entrypoint target already computed by ``_build_ir_edge``
    when no field pill exists (broadcast-only inner inputs, or a fieldless
    ``list[str]`` item).
    """
    # mapped-container_id -> {field leaf name -> pill synthetic id}. A map-fed
    # pill is indexed under the mapped container that owns its field, which may
    # be an ancestor of the pill's own deepest_owner (nested consumer).
    pill_by_field: dict[str, dict[str, str]] = {}
    for ext in external_inputs:
        if ext.map_fed and len(ext.params) == 1:
            leaf = external_input_display_name(ext.params[0])
            container = _mapped_container_for(ext.deepest_owner, leaf, map_fed_fields, flat_graph)
            if container is not None:
                pill_by_field.setdefault(container, {})[leaf] = ext.synthetic_id

    rerouted: list[IREdge] = []
    for edge in edges:
        fields = flat_graph.edges[edge.source, edge.target].get("map_fields") if flat_graph.has_edge(edge.source, edge.target) else None
        pills_for_container = pill_by_field.get(edge.target, {}) if fields else {}
        pill_ids = tuple(pills_for_container[f] for f in fields if f in pills_for_container) if fields else ()
        if pill_ids:
            new_target = pill_ids[0] if len(pill_ids) == 1 else pill_ids
            rerouted.append(replace(edge, target_when_expanded=new_target))
        else:
            rerouted.append(edge)
    return rerouted


def _build_input_group(
    group: dict,
    flat_graph: nx.DiGraph,
    id_for_param: dict[str, str] | None = None,
    map_fed_fields: dict[str, set[str]] | None = None,
) -> IRExternalInput:
    """Convert a build_input_groups dict into an IRExternalInput.

    Multi-param groups (e.g. one consumer takes both alpha and beta)
    become a single IRExternalInput with len(params) > 1, which
    scene_builder renders as INPUT_GROUP."""
    raw_params = tuple(group["params"])
    consumers: list[str] = []
    seen: set[str] = set()
    for param in raw_params:
        # Resolve consumers using the full graph-scope address so boundary
        # projection can address nested subgraphs. Deduplicate while preserving
        # order.
        for consumer in get_deepest_consumers(param, flat_graph):
            if consumer not in seen:
                seen.add(consumer)
                consumers.append(consumer)
    deepest_owner = compute_deepest_input_scope(raw_params[0], flat_graph)
    # Type hints are looked up by the parent-facing address first. If that
    # misses (collapsed view, leaf consumer carries the type under its local
    # name), fall back to the leaf name -- but only when at least one of the
    # param's actual consumers exposes that type, to avoid grabbing an
    # unrelated sibling's annotation when two namespaced inputs share a leaf.
    type_hints: list[str | None] = []
    for p in raw_params:
        param_type = get_param_type(p, flat_graph)
        if param_type is None:
            leaf = external_input_display_name(p)
            for consumer in get_deepest_consumers(p, flat_graph):
                consumer_type = flat_graph.nodes[consumer].get("input_types", {}).get(leaf)
                if consumer_type is not None:
                    param_type = consumer_type
                    break
        type_hints.append(format_type(param_type))
    # Display labels are resolved graph-scope port addresses. Synthetic ids
    # still use the short leaf segment when it is unambiguous.
    display_params = raw_params
    id_segments = tuple((id_for_param or {}).get(p, external_input_display_name(p)) for p in raw_params)
    # Map-fed when this single-param input's leaf name is an item field of the
    # fan-out edge into a mapped container that owns this input. The owning
    # container is ``deepest_owner`` OR any ancestor of it — a field consumer
    # nested one graph deeper than the mapped container still counts (the pill's
    # deepest_owner is then the inner container, but the map_fields live on the
    # outer mapped container). Multi-param INPUT_GROUPs never arise for a single
    # mapped item's fields, so only single-param inputs are considered.
    leaf = external_input_display_name(raw_params[0]) if raw_params else None  # type: ignore[assignment]
    map_fed = len(raw_params) == 1 and _owner_maps_field(deepest_owner, leaf, map_fed_fields or {}, flat_graph)
    return IRExternalInput(
        params=display_params,
        deepest_owner=deepest_owner,
        consumers=tuple(consumers),
        type_hints=tuple(type_hints),
        is_bound=bool(group.get("is_bound", False)),
        id_segments=id_segments,
        map_fed=map_fed,
    )


def _build_ir_edge(
    src: str,
    tgt: str,
    attrs: dict,
    flat_graph: nx.DiGraph,
    exclusive_edges: set[tuple[str, str, str]],
    mutex_groups: list[list[set[str]]],
    back_edges: set[tuple[str, str]],
    container_entrypoints: dict[str, tuple[str, ...]],
) -> IREdge:
    """Pre-compute the source-when-expanded / target-when-expanded rewrites
    so the JS scene_builder can re-route edges on container expansion
    without re-walking the graph."""
    source_when_expanded: str | tuple[str, ...] | None = None
    target_when_expanded: str | None = None

    edge_type = attrs.get("edge_type", "data")
    value_names = tuple(attrs.get("value_names", ()))

    src_attrs = flat_graph.nodes.get(src, {})
    if src_attrs.get("node_type") == "GRAPH":
        for value_name in value_names:
            internal = _find_deepest_internal_producers(src, value_name, flat_graph)
            if internal:
                source_when_expanded = internal[0] if len(internal) == 1 else internal
                break

    tgt_attrs = flat_graph.nodes.get(tgt, {})
    if tgt_attrs.get("node_type") == "GRAPH":
        for value_name in value_names:
            internal_consumers = _find_internal_consumers(tgt, value_name, flat_graph)
            if internal_consumers:
                target_when_expanded = internal_consumers[0] if len(internal_consumers) == 1 else internal_consumers
                break
        # Any edge into a container still unresolved here must re-route to the
        # container's entrypoint when the container is expanded, or the scene
        # holds an edge into a node the layouter never ranked. Two ways to land
        # here: control edges (value_names is empty, so the consumer search is
        # a no-op) and identity-mode fan-out edges (the value names a parent
        # column — e.g. "pages" — that no inner node consumes by name).
        # First entry of the canonical container_entrypoints map (D14, #211).
        if target_when_expanded is None:
            entrypoints = container_entrypoints.get(tgt)
            target_when_expanded = entrypoints[0] if entrypoints else None

    label = _branch_label_for_edge(src_attrs, tgt) if edge_type == "control" else None

    exclusive = edge_type == "data" and any((src, tgt, value_name) in exclusive_edges for value_name in value_names)
    if not exclusive and edge_type == "data" and isinstance(source_when_expanded, tuple) and len(source_when_expanded) > 1:
        exclusive = any(
            _is_pair_mutex(left, right, mutex_groups)
            for index, left in enumerate(source_when_expanded)
            for right in source_when_expanded[index + 1 :]
        )

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
    local_target = target.rsplit("/", 1)[-1]
    if "when_true" in branch_data and local_target == branch_data["when_true"]:
        return "True"
    if "when_false" in branch_data and local_target == branch_data["when_false"]:
        return "False"
    targets = branch_data.get("targets")
    if isinstance(targets, dict):
        for label, t in targets.items():
            if t == local_target:
                return str(label)
    return None


def _inner_output_name(container_id: str, value_name: str, flat_graph: nx.DiGraph) -> str:
    """Translate a container-level output name to the original inner name
    via the exact ``output_name_map`` recorded on the GRAPH node."""
    name_map = flat_graph.nodes.get(container_id, {}).get("output_name_map") or {}
    return name_map.get(value_name, value_name)


def _inner_input_names(container_id: str, value_name: str, flat_graph: nx.DiGraph) -> tuple[str, ...]:
    """Translate a container-level input name to the original inner names
    via the exact ``input_name_map`` recorded on the GRAPH node."""
    name_map = flat_graph.nodes.get(container_id, {}).get("input_name_map") or {}
    return name_map.get(value_name) or (value_name,)


def _find_deepest_internal_producer(container_id: str, value_name: str, flat_graph: nx.DiGraph) -> str | None:
    """Walk down the container tree, find the deepest descendant that
    produces value_name as an output. The exact rename recorded on the
    GRAPH node is applied first (e.g. ``with_outputs(item_out="generated")``);
    fuzzy substring matching remains as a fallback for renames that cross
    several boundaries (e.g. container exposes ``recall_scores`` but the
    internal node produces ``recall_score``)."""
    inner_name = _inner_output_name(container_id, value_name, flat_graph)
    descendants = [(node_id, attrs) for node_id, attrs in flat_graph.nodes(data=True) if _is_descendant(node_id, container_id, flat_graph)]

    candidates = [node_id for node_id, attrs in descendants if inner_name in attrs.get("outputs", ())]
    if candidates:
        return max(candidates, key=lambda c: _depth_below(c, container_id, flat_graph))

    fuzzy = [node_id for node_id, attrs in descendants for out in attrs.get("outputs", ()) if out in inner_name or inner_name in out]
    if fuzzy:
        return max(fuzzy, key=lambda c: _depth_below(c, container_id, flat_graph))
    return None


def _find_deepest_internal_producers(container_id: str, value_name: str, flat_graph: nx.DiGraph) -> tuple[str, ...]:
    """Return all deepest internal producers for a container output."""
    inner_name = _inner_output_name(container_id, value_name, flat_graph)
    descendants = [(node_id, attrs) for node_id, attrs in flat_graph.nodes(data=True) if _is_descendant(node_id, container_id, flat_graph)]

    candidates = [node_id for node_id, attrs in descendants if inner_name in attrs.get("outputs", ())]
    if not candidates:
        candidates = [node_id for node_id, attrs in descendants for out in attrs.get("outputs", ()) if out in inner_name or inner_name in out]
    if not candidates:
        return ()

    max_depth = max(_depth_below(node_id, container_id, flat_graph) for node_id in candidates)
    return tuple(node_id for node_id in candidates if _depth_below(node_id, container_id, flat_graph) == max_depth)


def _find_internal_consumers(container_id: str, value_name: str, flat_graph: nx.DiGraph) -> tuple[str, ...]:
    """EVERY internal consumer a boundary value feeds, not just the deepest.

    One incoming value can enter a container at several nodes — panda's
    generation graph consumes ``document`` at both its ``@ifelse`` guard and
    the nested messages graph. Returning only ``max(..., key=depth)`` left
    every other consumer with no incoming edge once the container expanded.

    Exact name matches are authoritative: each one genuinely reads the value,
    so each one gets the edge. A consumer already fed by an *internal*
    producer of the same name is excluded — its value does not cross the
    boundary. The fuzzy fallback stays single-answer (deepest): a substring
    match is a guess, and fanning a guess out would draw edges nobody can
    defend."""
    inner_names = _inner_input_names(container_id, value_name, flat_graph)
    descendants = [
        (node_id, attrs)
        for node_id, attrs in flat_graph.nodes(data=True)
        if _is_descendant(node_id, container_id, flat_graph) and attrs.get("node_type") != "GRAPH"
    ]

    def fed_internally(node_id: str) -> bool:
        for pred, _, attrs in flat_graph.in_edges(node_id, data=True):
            if not _is_descendant(pred, container_id, flat_graph):
                continue
            if any(inner in attrs.get("value_names", ()) for inner in inner_names):
                return True
        return False

    candidates = [
        node_id for node_id, attrs in descendants if any(inner in attrs.get("inputs", ()) for inner in inner_names) and not fed_internally(node_id)
    ]
    if candidates:
        return tuple(candidates)

    fuzzy = [node_id for node_id, attrs in descendants for inp in attrs.get("inputs", ()) for inner in inner_names if inp in inner or inner in inp]
    if fuzzy:
        return (max(fuzzy, key=lambda c: _depth_below(c, container_id, flat_graph)),)
    return ()


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


def resolve_boundary_ports(
    flat_graph: nx.DiGraph,
    src: str,
    tgt: str,
    value_names: Sequence[str],
) -> tuple[str | None, str | None]:
    """The ``(exit, entry)`` direct children a boundary edge leaves from and
    arrives at, for endpoints that are GRAPH containers.

    ``simplify`` needs these to tell a real pass-through from an assumed one:
    paired with ``compute_container_transits`` they say whether a
    collapsed box actually carries this value across. Direct children, not the
    deepest node, because transits are recorded between direct children.

    The scene builders read the equivalent off
    ``IREdge.source_when_expanded`` / ``target_when_expanded``; the Mermaid
    exporter, which has no IR, calls this so both pipelines agree.

    An **ambiguous** exit stays unresolved. Several deepest producers means a
    fan-out or mutex output where no single child definitely emits the value;
    picking the first would assert a route that may not run. This matches what
    the scene builders already do with a tuple ``*_when_expanded`` — anything
    unresolved becomes a dead end, never a pass-through.

    An entry resolves as long as every internal consumer sits under the SAME
    direct child — the value provably arrives there. Consumers spread across
    several children leave the entry unresolved for the same reason as an
    ambiguous exit: no single child is THE port.
    """
    exit_port: str | None = None
    entry_port: str | None = None

    if flat_graph.nodes.get(src, {}).get("node_type") == "GRAPH":
        for value_name in value_names:
            producers = _find_deepest_internal_producers(src, value_name, flat_graph)
            if producers:
                if len(producers) == 1:
                    exit_port = _direct_child(src, producers[0], flat_graph)
                break

    if flat_graph.nodes.get(tgt, {}).get("node_type") == "GRAPH":
        for value_name in value_names:
            consumers = _find_internal_consumers(tgt, value_name, flat_graph)
            if consumers:
                children = {_direct_child(tgt, consumer, flat_graph) for consumer in consumers}
                if len(children) == 1:
                    entry_port = children.pop()
                break

    return exit_port, entry_port


def _direct_child(container_id: str, descendant: str, flat_graph: nx.DiGraph) -> str | None:
    """Walk ``descendant`` up to the direct child of ``container_id``."""
    current: str | None = descendant
    for _ in range(len(flat_graph) + 1):
        if current is None:
            return None
        parent = flat_graph.nodes.get(current, {}).get("parent")
        if parent == container_id:
            return current
        current = parent
    return None


def compute_container_transits(flat_graph: nx.DiGraph) -> dict[str, list[list[str]]]:
    """Which ``[entry, exit]`` pairs a container actually carries a value between.

    A collapsed container is drawn as one box, which tempts every consumer to
    assume that whatever goes in can come out. That is wrong whenever a
    container does two unrelated jobs: ``side`` may consume ``doc`` at
    ``make_thumb`` and emit ``stats`` from ``count_words`` while the two never
    touch. Treating the box as a pass-through then lets ``simplify`` hide a real
    ``load → render`` edge and leave the reader following a route that does not
    exist.

    So this answers the question exactly: for each container, the pairs
    ``[entry, exit]`` where ``exit`` really is reachable from ``entry`` using
    only **unconditional** ``data`` edges *inside* that container. Control edges
    and mutex (``exclusive``) arms are both excluded, for the same reason as in
    the reduction itself: "may run" is not "received the value", and a route
    that exists only on one branch cannot make the box a pass-through. A
    container whose internal route is conditional must not license hiding an
    unconditional edge outside it.

    Only ports a boundary edge actually uses are considered, so the result is
    bounded by the container's boundary width rather than the square of its
    child count. That matters: this ships inside every widget payload, and
    ``tests/viz/test_payload_size.py`` caps per-container growth precisely to
    stop a precompute like this scaling super-linearly.

    This is the single derivation authority. The IR builder stamps it on
    ``GraphIR.container_transits``; the scene builders (Python and JS) and the
    Mermaid exporter consume it. Do not re-derive transits elsewhere.
    """
    exclusive_edges = compute_exclusive_data_edges(flat_graph)

    entries: dict[str, set[str]] = {}
    exits: dict[str, set[str]] = {}
    for src, tgt, attrs in flat_graph.edges(data=True):
        if attrs.get("edge_type", "data") != "data":
            continue
        exit_port, entry_port = resolve_boundary_ports(flat_graph, src, tgt, attrs.get("value_names", ()))
        if exit_port is not None:
            exits.setdefault(src, set()).add(exit_port)
        if entry_port is not None:
            entries.setdefault(tgt, set()).add(entry_port)

    children_of: dict[str, list[str]] = {}
    for node_id, attrs in flat_graph.nodes(data=True):
        parent = attrs.get("parent")
        if parent is not None:
            children_of.setdefault(parent, []).append(node_id)

    transits: dict[str, list[list[str]]] = {}
    for container_id, container_entries in entries.items():
        container_exits = exits.get(container_id)
        if not container_exits:
            continue  # nothing leaves: no pair can matter
        member_set = set(children_of.get(container_id, ()))
        inner: dict[str, set[str]] = {}
        for src, tgt, attrs in flat_graph.edges(data=True):
            if src not in member_set or tgt not in member_set:
                continue
            if attrs.get("edge_type", "data") != "data":
                continue
            value_names = attrs.get("value_names", ())
            if any((src, tgt, value_name) in exclusive_edges for value_name in value_names):
                continue  # a mutex arm carries its value only on its branch
            inner.setdefault(src, set()).add(tgt)

        pairs: list[list[str]] = []
        for entry in sorted(container_entries):
            # Reflexive on purpose: one child that both consumes at the
            # boundary and produces at it does carry the value across.
            reached = {entry}
            stack = [entry]
            while stack:
                current = stack.pop()
                for successor in inner.get(current, ()):
                    if successor not in reached:
                        reached.add(successor)
                        stack.append(successor)
            pairs.extend([entry, exit_node] for exit_node in sorted(reached & container_exits))
        if pairs:
            transits[container_id] = pairs
    return transits
