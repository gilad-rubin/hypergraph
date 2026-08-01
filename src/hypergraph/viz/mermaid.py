"""Mermaid flowchart exporter for HyperGraph.

Converts a flat NetworkX DiGraph (from Graph.to_flat_graph()) to styled
Mermaid flowchart syntax. Reuses the same visibility, expansion, and
edge-routing logic as the interactive JS visualization.

Usage:
    graph.to_mermaid()                         # Renders in notebooks
    graph.to_mermaid(show_types=True)          # With type annotations
    print(graph.to_mermaid())                  # Raw Mermaid source
    graph.to_mermaid().source                  # Access source directly
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import networkx as nx

from hypergraph.viz._common import (
    build_expansion_state,
    build_output_to_producer_map,
    build_param_to_consumer_map,
    compute_exclusive_data_edges,
    disambiguate_external_input_ids,
    external_input_display_name,
    is_descendant_of,
    is_node_visible,
)
from hypergraph.viz._mermaid_core import MermaidDiagram, _MermaidIdAllocator, _sanitize_id
from hypergraph.viz._simplify import EdgeRef, shortcut_edge_keys
from hypergraph.viz.renderer._format import format_type
from hypergraph.viz.renderer.ir_builder import (
    compute_container_transits,
    container_output_anchors,
    find_internal_consumers,
    resolve_boundary_ports,
    shared_answer_label,
)
from hypergraph.viz.renderer.nodes import (
    build_input_groups,
    get_start_targets,
    has_end_routing,
    is_internal_gate_output,
)
from hypergraph.viz.renderer.scope import (
    compute_container_entrypoints,
    find_back_edges,
    find_internal_producer_for_output,
    resolve_expanded_entrypoints,
)

__all__ = ["MermaidDiagram", "to_mermaid", "_MermaidIdAllocator", "_sanitize_id"]

# =============================================================================
# Constants
# =============================================================================

_VALID_DIRECTIONS = {"TD", "TB", "BT", "LR", "RL"}

DEFAULT_COLORS: dict[str, dict[str, str]] = {
    "function": {
        "fill": "#E8F5E8",
        "stroke": "#388E3C",
        "stroke-width": "2px",
        "color": "#1B5E20",
    },
    "container": {
        "fill": "#FFF3E0",
        "stroke": "#F57C00",
        "stroke-width": "2px",
        "color": "#E65100",
    },
    "branch": {
        "fill": "#FFF8E1",
        "stroke": "#FBC02D",
        "stroke-width": "2px",
        "color": "#F57F17",
    },
    "input": {
        "fill": "#E3F2FD",
        "stroke": "#1976D2",
        "stroke-width": "2px",
        "color": "#0D47A1",
    },
    "data": {
        "fill": "#F3E5F5",
        "stroke": "#7B1FA2",
        "stroke-width": "2px",
        "color": "#4A148C",
    },
    "end": {
        "fill": "#ECEFF1",
        "stroke": "#546E7A",
        "stroke-width": "2px",
        "color": "#263238",
    },
    "start": {
        "fill": "#ECFDF5",
        "stroke": "#10B981",
        "stroke-width": "2px",
        "color": "#065F46",
    },
    "output": {
        "fill": "#ECFDF5",
        "stroke": "#34D399",
        "stroke-width": "2px",
        "color": "#047857",
    },
}

# Maps HyperGraph node_type to Mermaid classDef name
_NODE_TYPE_TO_CLASS = {
    "FUNCTION": "function",
    "GRAPH": "container",
    "BRANCH": "branch",
    "INPUT": "input",
    "INPUT_GROUP": "input",
    "DATA": "data",
    "OUTPUT": "output",
    "START": "start",
    "END": "end",
}

# =============================================================================
# Label Construction
# =============================================================================


def _escape_label(text: str) -> str:
    """Escape characters that have special meaning in Mermaid labels."""
    return text.replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _build_label(
    attrs: dict[str, Any],
    show_types: bool,
    separate_outputs: bool,
) -> str:
    """Build the display label for a node.

    Output names are always shown below the node name (matching the
    interactive JS viz style). When show_types is True, type annotations
    are appended to each output. Skipped in separate_outputs mode since
    outputs are rendered as dedicated DATA nodes.
    """
    label = _escape_label(attrs.get("label", ""))

    if separate_outputs:
        return label

    outputs = attrs.get("outputs", ())
    if not outputs:
        return label

    output_types = attrs.get("output_types", {})
    type_parts = []
    for out in outputs:
        if show_types:
            formatted = format_type(output_types.get(out))
            if formatted:
                type_parts.append(f"{out}: {_escape_label(formatted)}")
            else:
                type_parts.append(out)
        else:
            type_parts.append(out)

    separator = "-" * max(len(label), max(len(p) for p in type_parts))
    return label + "<br/>" + separator + "<br/>" + "<br/>".join(type_parts)


def _build_input_label(
    params: list[str],
    param_types: list[str | None],
    show_types: bool,
) -> str:
    """Build label for an INPUT or INPUT_GROUP node."""
    if len(params) == 1:
        label = _escape_label(params[0])
        if show_types and param_types[0]:
            label += f": {_escape_label(param_types[0])}"
        return label

    parts = []
    for param, ptype in zip(params, param_types, strict=False):
        entry = _escape_label(param)
        if show_types and ptype:
            entry += f": {_escape_label(ptype)}"
        parts.append(entry)
    return "<br/>".join(parts)


def _build_data_label(
    output_name: str,
    output_type: str | None,
    show_types: bool,
) -> str:
    """Build label for a DATA node."""
    label = _escape_label(output_name)
    if show_types and output_type:
        label += f": {_escape_label(output_type)}"
    return label


# =============================================================================
# Node Formatting
# =============================================================================

# Shape templates: (open, close) delimiters for each node type
_SHAPE_DELIMITERS: dict[str, tuple[str, str]] = {
    "FUNCTION": ('["', '"]'),
    "GRAPH": ('[["', '"]]'),
    "BRANCH": ('{{"', '"}}'),
    "INPUT": ('(["', '"])'),
    "INPUT_GROUP": ('(["', '"])'),
    "DATA": ('[/"', '"/]'),
    "OUTPUT": ('(["', '"])'),
    "START": ('(("', '"))'),
    "END": ('(["', '"])'),
}


def _format_node(safe_id: str, label: str, node_type: str) -> str:
    """Format a complete Mermaid node definition."""
    open_delim, close_delim = _SHAPE_DELIMITERS.get(node_type, ('["', '"]'))
    return f"    {safe_id}{open_delim}{label}{close_delim}"


# =============================================================================
# Edge Rendering
# =============================================================================


@dataclass(frozen=True)
class _RenderedEdge:
    """One emitted Mermaid edge line plus the facts ``simplify`` needs.

    ``source``/``target`` are the *resolved* node ids (post expansion
    rewriting), matching the ids the interactive scene builder uses, so both
    pipelines reduce the same graph. ``kind`` also drives ``linkStyle``
    targeting for ordering edges.
    """

    line: str
    kind: str  # "data" | "output" | "control" | "ordering" | "input" | "start" | "end"
    source: str
    target: str
    exclusive: bool = False
    is_back_edge: bool = False
    # Direct children this edge leaves from / arrives at when its endpoint is a
    # *collapsed* container, from ``ir_builder.resolve_boundary_ports``. They
    # let ``simplify`` cross a closed box only where it really carries the
    # value; see ``_simplify_rendered_edges``.
    exit_port: str | None = None
    entry_port: str | None = None

    @property
    def removable(self) -> bool:
        """Plain data edges and INPUT pill edges may be dropped — one input
        feeding a chain keeps only its earliest consumer(s). An ``output``
        edge is the structural producer→DATA link, and dropping it would
        orphan the pill."""
        return self.kind in ("data", "input") and not self.exclusive and not self.is_back_edge

    @property
    def traversable(self) -> bool:
        """On the *unconditional* data-flow spine, so it may justify dropping
        a shortcut.

        Control and ordering edges are excluded: a gate's dotted arrow means
        "may run", not "receives this value". Exclusive arms are excluded for
        the same reason — an arm carries its value only when its branch is
        taken, so it must not imply away an unconditional edge.
        """
        return self.kind in ("data", "output", "input") and not self.is_back_edge and not self.exclusive


def _simplify_rendered_edges(
    rendered: list[_RenderedEdge],
    collapsed_containers: set[str],
    container_transits: dict[str, list[list[str]]],
) -> list[_RenderedEdge]:
    """Drop rendered data and INPUT edges a longer path already implies.

    START / END edges stay inert — nothing points into ``__start__`` or out of
    ``__end__``. INPUT pill edges enter the path graph AND the candidate set:
    one input feeding a chain keeps only its earliest consumer(s), exactly as
    in ``scene_builder.simplify_transitive_edges``.

    Collapsed containers are split into per-port nodes exactly as in
    ``scene_builder.simplify_transitive_edges``, so the text export and the
    widget agree about which boxes really pass a value through. An
    unresolvable port becomes a dead end: unverified means "do not hide". An
    INPUT edge instead targets the box AS A BOX — the drawn line ends at the
    hull, so its candidacy asks only whether the value reaches the box, which
    the phantom in-port → box links answer.
    """

    def port(node_id: str, index: int, side: str, resolved: str | None) -> str:
        if node_id not in collapsed_containers:
            return node_id
        suffix = resolved if resolved is not None else f"?{index}"
        return f"{node_id}\x00{side}\x00{suffix}"

    def ref_target(edge: _RenderedEdge, index: int) -> str:
        if edge.kind == "input":
            return edge.target
        return port(edge.target, index, "in", edge.entry_port)

    refs = [
        EdgeRef(
            key=index,
            source=port(edge.source, index, "out", edge.exit_port),
            target=ref_target(edge, index),
            removable=edge.removable,
            traversable=edge.traversable,
        )
        for index, edge in enumerate(rendered)
    ]
    for container in collapsed_containers:
        for entry, exit_node in container_transits.get(container, ()):
            refs.append(
                EdgeRef(
                    key=("\x00transit", container, entry, exit_node),
                    source=f"{container}\x00in\x00{entry}",
                    target=f"{container}\x00out\x00{exit_node}",
                    removable=False,
                    traversable=True,
                )
            )
    # Path-only links from every delivered-to in-port to its box, so "reaches
    # the box" means exactly "some rendered edge delivers into the box". The
    # bare box id has no outgoing path links, so these can never manufacture a
    # pass-through — they only answer input-edge candidacy.
    seen_ports: set[str] = set()
    for ref in list(refs):
        port_id = str(ref.target)
        if port_id in seen_ports or "\x00in\x00" not in port_id:
            continue
        seen_ports.add(port_id)
        refs.append(
            EdgeRef(
                key=("\x00boxin", port_id),
                source=port_id,
                target=port_id.split("\x00", 1)[0],
                removable=False,
                traversable=True,
            )
        )
    dropped = shortcut_edge_keys(refs)
    return [edge for index, edge in enumerate(rendered) if index not in dropped]


def _render_merged_edges(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    container_entrypoints: dict[str, tuple[str, ...]],
    exclusive_data_edges: set[tuple[str, str, str]],
    back_edges: set[tuple[str, str]],
    id_allocator: _MermaidIdAllocator,
    output_anchors: dict[str, dict[str, str]] | None = None,
) -> list[_RenderedEdge]:
    """Render edges in merged output mode (no DATA intermediaries).

    Mirrors the interactive renderer's merged-output edge derivation.
    """
    out: list[_RenderedEdge] = []
    output_to_producer = build_output_to_producer_map(
        flat_graph,
        expansion_state,
        use_deepest=True,
    )
    # Control/ordering keys are 3-tuples; merged data keys are the resolved
    # (source, target) pair — the widget's one-arrow-per-pair story.
    seen_edges: set[tuple[str, ...]] = set()

    for source, target, edge_data in flat_graph.edges(data=True):
        if not is_node_visible(source, flat_graph, expansion_state):
            continue

        edge_type = edge_data.get("edge_type", "data")
        value_names = edge_data.get("value_names", [])

        if edge_type == "control":
            actual_target = _resolve_control_target(
                source,
                target,
                flat_graph,
                expansion_state,
                container_entrypoints,
            )
            if actual_target is None:
                continue
            label = _get_control_label(source, target, flat_graph)
            edge_key = (id_allocator.get(source), id_allocator.get(actual_target), label or "")
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            out.append(
                _RenderedEdge(
                    _format_control_edge(source, actual_target, label, id_allocator),
                    "control",
                    source,
                    actual_target,
                    is_back_edge=(source, target) in back_edges,
                )
            )
            continue

        if edge_type == "ordering":
            if not is_node_visible(target, flat_graph, expansion_state):
                continue
            value_name = value_names[0] if value_names else ""
            if not value_name and (source, target) in back_edges:
                # A routed interrupt's answer edge: name the shared value it
                # exists to deliver, so the cycle reads as the answer
                # returning to the gate.
                value_name = shared_answer_label(source, target, flat_graph) or ""
            edge_key = (id_allocator.get(source), id_allocator.get(target), f"ord_{value_name}")
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            out.append(
                _RenderedEdge(
                    _format_ordering_edge(source, target, value_name, id_allocator),
                    "ordering",
                    source,
                    target,
                    is_back_edge=(source, target) in back_edges,
                )
            )
            continue

        # Data edges. Merged mode draws ONE arrow per resolved (source,
        # target) pair — the widget's story — so the dedup key carries no
        # value name, and exclusivity is OR'd across every value the pair
        # carries (a mutex arm on any of them dashes the one drawn arrow).
        values = value_names if value_names else [""]
        pair_exclusive = any((source, target, value_name) in exclusive_data_edges for value_name in values)
        for value_name in values:
            actual_source = _resolve_data_source(
                source,
                value_name,
                flat_graph,
                expansion_state,
                output_to_producer,
                output_anchors,
            )
            actual_targets = _resolve_data_targets(
                target,
                value_name,
                flat_graph,
                expansion_state,
                container_entrypoints,
            )
            if actual_source is None:
                continue
            for resolved_target in actual_targets:
                if actual_source == resolved_target:
                    continue
                pair_key = (id_allocator.get(actual_source), id_allocator.get(resolved_target))
                if pair_key in seen_edges:
                    continue
                seen_edges.add(pair_key)
                exit_port, entry_port = resolve_boundary_ports(flat_graph, source, target, value_names or [value_name])
                out.append(
                    _RenderedEdge(
                        _format_edge(actual_source, resolved_target, None, exclusive=pair_exclusive, id_allocator=id_allocator),
                        "data",
                        actual_source,
                        resolved_target,
                        exclusive=pair_exclusive,
                        is_back_edge=(source, target) in back_edges,
                        exit_port=exit_port,
                        entry_port=entry_port,
                    )
                )

    return out


def _render_separate_edges(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    container_entrypoints: dict[str, tuple[str, ...]],
    exclusive_data_edges: set[tuple[str, str, str]],
    back_edges: set[tuple[str, str]],
    id_allocator: _MermaidIdAllocator,
    output_anchors: dict[str, dict[str, str]] | None = None,
) -> list[_RenderedEdge]:
    """Render edges in separate output mode (with DATA intermediaries).

    Mirrors the interactive renderer's separate-output edge derivation.
    """
    out: list[_RenderedEdge] = []
    output_to_producer = build_output_to_producer_map(
        flat_graph,
        expansion_state,
        use_deepest=True,
    )
    seen_edges: set[tuple[str, ...]] = set()

    # Function → DATA edges
    for node_id, attrs in flat_graph.nodes(data=True):
        if not is_node_visible(node_id, flat_graph, expansion_state):
            continue
        if attrs.get("node_type") == "GRAPH" and expansion_state.get(node_id, False):
            continue
        for output_name in attrs.get("outputs", ()):
            if is_internal_gate_output(node_id, output_name, attrs):
                continue
            data_id = f"data_{node_id}_{output_name}"
            edge_key = (id_allocator.get(node_id), id_allocator.get(data_id))
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                # "output", not "data": the structural producer→DATA link is
                # never a removal candidate (it would orphan the DATA pill).
                out.append(_RenderedEdge(_format_edge(node_id, data_id, None, id_allocator=id_allocator), "output", node_id, data_id))

    # DATA → consumer edges + control/ordering edges
    for source, target, edge_data in flat_graph.edges(data=True):
        if not is_node_visible(source, flat_graph, expansion_state):
            continue
        if not is_node_visible(target, flat_graph, expansion_state):
            continue

        edge_type = edge_data.get("edge_type", "data")
        value_names = edge_data.get("value_names", [])

        if edge_type == "data":
            anchors_for_source = (output_anchors or {}).get(source, {})
            for value_name in value_names or [""]:
                if not value_name:
                    continue
                # Resolve source to internal producer for expanded graphs
                actual_source = _resolve_data_source(
                    source,
                    value_name,
                    flat_graph,
                    expansion_state,
                    output_to_producer,
                    output_anchors,
                )
                if actual_source is None:
                    continue
                actual_targets = _resolve_data_targets(
                    target,
                    value_name,
                    flat_graph,
                    expansion_state,
                    container_entrypoints,
                )
                source_attrs = flat_graph.nodes.get(actual_source, {})
                if is_internal_gate_output(actual_source, value_name, source_attrs):
                    continue
                # A synthesized OUTPUT anchor IS the value pill — no DATA
                # node interposed.
                is_anchor_source = actual_source == anchors_for_source.get(value_name)
                data_id = actual_source if is_anchor_source else f"data_{actual_source}_{value_name}"
                for resolved_target in actual_targets:
                    edge_key = (id_allocator.get(data_id), id_allocator.get(resolved_target))
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        is_exclusive = (source, target, value_name) in exclusive_data_edges
                        out.append(
                            _RenderedEdge(
                                _format_edge(data_id, resolved_target, value_name, exclusive=is_exclusive, id_allocator=id_allocator),
                                "data",
                                data_id,
                                resolved_target,
                                exclusive=is_exclusive,
                                is_back_edge=(source, target) in back_edges,
                            )
                        )

        elif edge_type == "ordering":
            value_name = value_names[0] if value_names else ""
            if not value_name and (source, target) in back_edges:
                value_name = shared_answer_label(source, target, flat_graph) or ""
            edge_key = (id_allocator.get(source), id_allocator.get(target), f"ord_{value_name}")  # type: ignore[assignment]
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                out.append(
                    _RenderedEdge(
                        _format_ordering_edge(source, target, value_name, id_allocator),
                        "ordering",
                        source,
                        target,
                        is_back_edge=(source, target) in back_edges,
                    )
                )

        elif edge_type == "control":
            actual_target = _resolve_control_target(
                source,
                target,
                flat_graph,
                expansion_state,
                container_entrypoints,
            )
            if actual_target is None:
                continue
            label = _get_control_label(source, target, flat_graph)
            edge_key = (id_allocator.get(source), id_allocator.get(actual_target), label or "")  # type: ignore[assignment]
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                out.append(
                    _RenderedEdge(
                        _format_control_edge(source, actual_target, label, id_allocator),
                        "control",
                        source,
                        actual_target,
                        is_back_edge=(source, target) in back_edges,
                    )
                )

    return out


# =============================================================================
# Edge Helpers
# =============================================================================


def _format_edge(
    source: str,
    target: str,
    label: str | None,
    exclusive: bool = False,
    *,
    id_allocator: _MermaidIdAllocator,
) -> str:
    """Format a Mermaid data edge; dashed when fed by mutex producers."""
    s, t = id_allocator.get(source), id_allocator.get(target)
    arrow = "-.->" if exclusive else "-->"
    if label:
        return f"    {s} {arrow}|{label}| {t}"
    return f"    {s} {arrow} {t}"


def _format_ordering_edge(
    source: str,
    target: str,
    label: str,
    id_allocator: _MermaidIdAllocator,
) -> str:
    """Format a dotted-arrow Mermaid edge (for ordering/emit edges)."""
    s, t = id_allocator.get(source), id_allocator.get(target)
    if label:
        return f"    {s} -.->|{label}| {t}"
    return f"    {s} -.-> {t}"


def _format_control_edge(
    source: str,
    target: str,
    label: str | None,
    id_allocator: _MermaidIdAllocator,
) -> str:
    """Format a dotted-arrow Mermaid control edge (for gate-origin edges)."""
    s, t = id_allocator.get(source), id_allocator.get(target)
    if label:
        return f"    {s} -.->|{label}| {t}"
    return f"    {s} -.-> {t}"


def _resolve_control_target(
    source: str,
    target: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    container_entrypoints: dict[str, tuple[str, ...]],
) -> str | None:
    """Resolve the actual target for a control edge, entering containers.

    Uses the canonical container-entrypoint derivation (D14, #211) shared
    with the IR builder; Mermaid renders its first entry, matching the
    interactive renderer's ``IREdge.target_when_expanded`` fallback.
    """
    entrypoints = resolve_expanded_entrypoints(
        (target,),
        container_entrypoints,
        expansion_state,
    )
    if not entrypoints:
        return None
    actual_target = entrypoints[0]
    if not is_node_visible(actual_target, flat_graph, expansion_state):
        return None
    return actual_target


def _resolve_data_source(
    source: str,
    value_name: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    output_to_producer: dict[str, str],
    output_anchors: dict[str, dict[str, str]] | None = None,
) -> str | None:
    """Resolve actual source for a data edge, exiting expanded containers."""
    actual_source = source
    source_attrs = flat_graph.nodes.get(source, {})
    if source_attrs.get("node_type") == "GRAPH" and expansion_state.get(source, False) and value_name:
        internal = output_to_producer.get(value_name)
        if internal and internal != source and is_descendant_of(internal, source, flat_graph):
            actual_source = internal
        else:
            found = find_internal_producer_for_output(
                source,
                value_name,
                flat_graph,
                expansion_state,
            )
            if found:
                actual_source = found
            else:
                # No descendant produces the value (a mounted table's
                # receipt): the edge leaves the synthesized OUTPUT anchor
                # inside the expanded subgraph, never the subgraph hull.
                anchor = (output_anchors or {}).get(source, {}).get(value_name)
                if anchor:
                    return anchor
    if not is_node_visible(actual_source, flat_graph, expansion_state):
        return None
    return actual_source


def _resolve_data_targets(
    target: str,
    value_name: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    container_entrypoints: dict[str, tuple[str, ...]],
) -> list[str]:
    """Resolve actual target(s) for a data edge, entering expanded containers.

    One incoming value can enter a container at several internal consumers, so
    an edge into an EXPANDED container fans out to every one of them — the twin
    of the scene builders' tuple ``target_when_expanded``, resolved by the same
    authority (``ir_builder.find_internal_consumers``), so a consumer already
    fed by an INTERNAL producer of the value never receives the boundary edge.
    A consumer that sits inside a still-collapsed inner container resolves up
    to that visible boundary instead of silently dropping the edge."""
    target_attrs = flat_graph.nodes.get(target, {})
    if target_attrs.get("node_type") == "GRAPH" and expansion_state.get(target, False) and value_name:
        internal = list(find_internal_consumers(target, value_name, flat_graph))
        if not internal:
            entrypoints = resolve_expanded_entrypoints(
                (target,),
                container_entrypoints,
                expansion_state,
            )
            internal = [entrypoints[0]] if entrypoints else []
        resolved: list[str] = []
        for candidate in internal:
            visible = _visible_ancestor(candidate, flat_graph, expansion_state)
            if visible is not None and visible not in resolved:
                resolved.append(visible)
        return resolved
    if not is_node_visible(target, flat_graph, expansion_state):
        return []
    return [target]


def _visible_ancestor(node_id: str, flat_graph: nx.DiGraph, expansion_state: dict[str, bool]) -> str | None:
    """Walk up from ``node_id`` to the first currently-visible node.

    Only collapse-hiding aggregates: a node hidden by ``hide=True`` walks up
    to an EXPANDED ancestor, which is never a legitimate edge target — the
    edge is dropped instead, exactly as before."""
    current: str | None = node_id
    while current is not None and not is_node_visible(current, flat_graph, expansion_state):
        current = flat_graph.nodes.get(current, {}).get("parent")
    if current is not None and current != node_id and expansion_state.get(current):
        return None
    return current


def _get_control_label(
    source: str,
    target: str,
    flat_graph: nx.DiGraph,
) -> str | None:
    """Get the user-facing label for a control edge."""
    source_attrs = flat_graph.nodes.get(source, {})
    branch_data = source_attrs.get("branch_data", {})
    if not branch_data:
        return None
    local_target = target.rsplit("/", 1)[-1]
    if "when_true" in branch_data:
        if local_target == branch_data["when_true"]:
            return "True"
        if local_target == branch_data["when_false"]:
            return "False"
    targets = branch_data.get("targets")
    if isinstance(targets, dict):
        for label, route_target in targets.items():
            if route_target in {target, local_target}:
                return str(label)
    return None


def _get_end_control_label(branch_data: dict[str, Any]) -> str | None:
    """Get the user-facing label for a control edge to END."""
    targets = branch_data.get("targets")
    if isinstance(targets, dict):
        for label, route_target in targets.items():
            if route_target == "END":
                return str(label)
    return None


# =============================================================================
# Subgraph Rendering
# =============================================================================


def _render_subgraph_block(
    container_id: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    show_types: bool,
    separate_outputs: bool,
    node_class_map: dict[str, str],
    id_allocator: _MermaidIdAllocator,
    indent: int = 1,
    output_anchors: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """Render a subgraph block for an expanded GRAPH node."""
    attrs = flat_graph.nodes[container_id]
    safe_id = id_allocator.get(container_id)
    label = _escape_label(attrs.get("label", container_id))
    prefix = "    " * indent

    lines = [f'{prefix}subgraph {safe_id} ["{label}"]']

    # Render child nodes
    children = [
        (nid, nattrs) for nid, nattrs in flat_graph.nodes(data=True) if nattrs.get("parent") == container_id and not nattrs.get("hide", False)
    ]

    for child_id, child_attrs in children:
        child_type = child_attrs.get("node_type", "FUNCTION")

        # Nested subgraph
        if child_type == "GRAPH" and expansion_state.get(child_id, False):
            lines.extend(
                _render_subgraph_block(
                    child_id,
                    flat_graph,
                    expansion_state,
                    show_types,
                    separate_outputs,
                    node_class_map,
                    id_allocator,
                    indent=indent + 1,
                    output_anchors=output_anchors,
                )
            )
        else:
            child_label = _build_label(child_attrs, show_types, separate_outputs)
            mermaid_type = "GRAPH" if child_type == "GRAPH" else child_type
            lines.append(
                "    " * (indent + 1)
                + _format_node(
                    id_allocator.get(child_id),
                    child_label,
                    mermaid_type,
                ).strip()
            )
            node_class_map[child_id] = _NODE_TYPE_TO_CLASS.get(mermaid_type, "function")

    # Synthesized boundary-output anchors: a container output no descendant
    # produces (a mounted table's receipt) surfaces as an OUTPUT pill inside
    # the expanded subgraph, and the boundary edge leaves it instead of the
    # subgraph hull. Same anchor ids as the interactive IR path.
    output_types = attrs.get("output_types", {})
    for value_name, anchor_id in sorted(((output_anchors or {}).get(container_id, {})).items()):
        anchor_label = _build_data_label(value_name, format_type(output_types.get(value_name)), show_types)
        lines.append("    " * (indent + 1) + _format_node(id_allocator.get(anchor_id), anchor_label, "OUTPUT").strip())
        node_class_map[anchor_id] = "output"

    lines.append(f"{prefix}end")
    return lines


# =============================================================================
# Style Section
# =============================================================================


def _build_style_section(
    colors: dict[str, dict[str, str]] | None,
    node_class_map: dict[str, str],
    ordering_edge_indices: list[int],
    id_allocator: _MermaidIdAllocator,
) -> list[str]:
    """Build classDef, class assignments, and linkStyle lines."""
    effective = {cls: props.copy() for cls, props in DEFAULT_COLORS.items()}
    if colors:
        for key, val in colors.items():
            effective.setdefault(key, {}).update(val)

    lines: list[str] = []

    # classDef statements
    used_classes = set(node_class_map.values())
    for cls_name, props in effective.items():
        if cls_name not in used_classes:
            continue
        prop_str = ",".join(f"{k}:{v}" for k, v in props.items())
        lines.append(f"    classDef {cls_name} {prop_str}")

    # class assignments — group node IDs by class
    class_to_ids: dict[str, list[str]] = {}
    for node_id, cls in node_class_map.items():
        class_to_ids.setdefault(cls, []).append(id_allocator.get(node_id))

    for cls_name, ids in sorted(class_to_ids.items()):
        lines.append(f"    class {','.join(ids)} {cls_name}")

    # linkStyle for ordering (dotted) edges — purple stroke
    if ordering_edge_indices:
        indices = ",".join(str(i) for i in ordering_edge_indices)
        lines.append(f"    linkStyle {indices} stroke:#8b5cf6,stroke-width:1.5px")

    return lines


# =============================================================================
# Public API
# =============================================================================


def to_mermaid(
    flat_graph: nx.DiGraph,
    *,
    depth: int = 0,
    show_types: bool = True,
    separate_outputs: bool = False,
    simplify: bool = True,
    direction: str = "TD",
    colors: dict[str, dict[str, str]] | None = None,
) -> MermaidDiagram:
    """Convert a flat NetworkX graph to a Mermaid flowchart diagram.

    Operates on the same flat DiGraph produced by Graph.to_flat_graph(),
    reusing the same visibility/expansion logic as the JS visualization.

    Args:
        flat_graph: Flattened NetworkX DiGraph from Graph.to_flat_graph()
        depth: How many levels of nested graphs to expand (default: 0)
        show_types: Whether to show type annotations in labels
        separate_outputs: Whether to render outputs as separate DATA nodes
        simplify: Hide data and input edges a longer path already implies —
            given ``A → B → C``, a direct ``A → C`` is dropped, and an input
            feeding the whole chain keeps only its earliest consumer
            (default: True)
        direction: Flowchart direction — "TD", "TB", "LR", "RL", "BT"
        colors: Custom color overrides per node class, e.g.
            {"function": {"fill": "#fff", "stroke": "#000"}}

    Returns:
        MermaidDiagram that renders in notebooks and converts to string.

    Example:
        >>> diagram = graph.to_mermaid(show_types=True)
        >>> diagram          # renders in notebook
        >>> print(diagram)   # raw Mermaid source
    """
    if direction not in _VALID_DIRECTIONS:
        msg = f"Invalid direction {direction!r}. Must be one of {sorted(_VALID_DIRECTIONS)}"
        raise ValueError(msg)

    expansion_state = build_expansion_state(flat_graph, depth)
    container_entrypoints = compute_container_entrypoints(flat_graph)
    # Boundary outputs no descendant produces (a mounted table's receipt):
    # each gets an OUTPUT anchor pill inside its expanded subgraph, shared
    # with the interactive IR path so both pipelines agree on ids.
    output_anchors = container_output_anchors(flat_graph)
    input_spec = flat_graph.graph.get("input_spec", {})
    bound_params = set(input_spec.get("bound", {}).keys())
    param_to_consumers = build_param_to_consumer_map(flat_graph, expansion_state)

    lines: list[str] = [f"flowchart {direction}"]
    node_class_map: dict[str, str] = {}
    id_allocator = _MermaidIdAllocator()

    # --- Shared state annotation ---
    shared_params = flat_graph.graph.get("shared", [])
    if shared_params:
        lines.append(f"    %% shared state: {', '.join(shared_params)}")

    start_targets = get_start_targets(
        flat_graph,
        expansion_state,
        container_entrypoints,
    )

    # --- START node (emit early so layout keeps START visually above flow) ---
    if start_targets:
        start_id = "__start__"
        lines.append("    %% Start")
        lines.append(_format_node(id_allocator.get(start_id), "Start", "START"))
        node_class_map[start_id] = "start"

    # --- Input nodes ---
    input_groups = build_input_groups(
        input_spec,
        param_to_consumers,
        bound_params,
        set(shared_params),
        False,
    )
    id_for_param = disambiguate_external_input_ids([list(g["params"]) for g in input_groups])
    if input_groups:
        lines.append("    %% Inputs")
    for group in input_groups:
        params = group["params"]
        # Display labels are resolved graph-scope port addresses; type lookup
        # falls back to the leaf if the projected key has no entry.
        display_params = list(params)
        param_types = [format_type(_get_param_type(p, flat_graph) or _get_param_type(external_input_display_name(p), flat_graph)) for p in params]
        label = _build_input_label(display_params, param_types, show_types)

        if len(params) == 1:
            node_id = f"input_{id_for_param.get(params[0], external_input_display_name(params[0]))}"
            node_type = "INPUT"
        else:
            id_segs = [id_for_param.get(p, external_input_display_name(p)) for p in params]
            node_id = f"input_group_{'_'.join(id_segs)}"
            node_type = "INPUT_GROUP"

        lines.append(_format_node(id_allocator.get(node_id), label, node_type))
        node_class_map[node_id] = "input"

    # --- Function / Graph / Branch nodes ---
    lines.append("    %% Nodes")
    # Track which containers are expanded so we skip their children
    # (they're rendered inside the subgraph block, not at top level)
    expanded_containers = {nid for nid, expanded in expansion_state.items() if expanded}

    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("hide", False):
            continue
        if not is_node_visible(node_id, flat_graph, expansion_state):
            continue

        # Skip nodes inside an expanded container — already in subgraph block
        parent = attrs.get("parent")
        if parent is not None and parent in expanded_containers:
            continue

        node_type = attrs.get("node_type", "FUNCTION")

        # Expanded subgraph
        if node_type == "GRAPH" and expansion_state.get(node_id, False):
            lines.extend(
                _render_subgraph_block(
                    node_id,
                    flat_graph,
                    expansion_state,
                    show_types,
                    separate_outputs,
                    node_class_map,
                    id_allocator,
                    output_anchors=output_anchors,
                )
            )
            continue

        label = _build_label(attrs, show_types, separate_outputs)
        lines.append(_format_node(id_allocator.get(node_id), label, node_type))
        node_class_map[node_id] = _NODE_TYPE_TO_CLASS.get(node_type, "function")

    # --- DATA nodes (separate_outputs mode only) ---
    if separate_outputs:
        for node_id, attrs in flat_graph.nodes(data=True):
            if attrs.get("hide", False):
                continue
            if not is_node_visible(node_id, flat_graph, expansion_state):
                continue
            if attrs.get("node_type") == "GRAPH" and expansion_state.get(node_id, False):
                continue
            output_types = attrs.get("output_types", {})
            for output_name in attrs.get("outputs", ()):
                if is_internal_gate_output(node_id, output_name, attrs):
                    continue
                data_id = f"data_{node_id}_{output_name}"
                data_label = _build_data_label(
                    output_name,
                    format_type(output_types.get(output_name)),
                    show_types,
                )
                lines.append(_format_node(id_allocator.get(data_id), data_label, "DATA"))
                node_class_map[data_id] = "data"

    # --- END node ---
    if has_end_routing(flat_graph, expansion_state):
        end_id = "__end__"
        lines.append(_format_node(id_allocator.get(end_id), "End", "END"))
        node_class_map[end_id] = "end"

    # --- Edge collection (kind-tagged so linkStyle can target ordering only) ---
    lines.append("    %% Edges")
    edge_pairs: list[tuple[str, str]] = []
    edge_pairs.extend((line, "start") for line in _render_start_edges(start_targets, id_allocator))

    input_rendered: list[_RenderedEdge] = []
    for group in input_groups:
        params = group["params"]
        if len(params) == 1:
            input_node_id = f"input_{id_for_param.get(params[0], external_input_display_name(params[0]))}"
        else:
            id_segs = [id_for_param.get(p, external_input_display_name(p)) for p in params]
            input_node_id = f"input_group_{'_'.join(id_segs)}"

        targets = _get_input_targets(
            params,
            flat_graph,
            param_to_consumers,
            expansion_state,
            container_entrypoints,
        )
        for tgt in targets:
            input_rendered.append(
                _RenderedEdge(
                    _format_edge(input_node_id, tgt, None, id_allocator=id_allocator),
                    "input",
                    input_node_id,
                    tgt,
                )
            )

    exclusive_data_edges = compute_exclusive_data_edges(flat_graph)
    back_edges = find_back_edges(flat_graph)
    render_edges = _render_separate_edges if separate_outputs else _render_merged_edges
    rendered = render_edges(
        flat_graph,
        expansion_state,
        container_entrypoints,
        exclusive_data_edges,
        back_edges,
        id_allocator,
        output_anchors,
    )
    # Input edges enter the reduction with the flow edges (an input feeding a
    # chain keeps only its earliest consumer), then emit first so the text
    # keeps its historical section order.
    combined = input_rendered + rendered
    if simplify:
        collapsed_containers = {
            node_id for node_id, attrs in flat_graph.nodes(data=True) if attrs.get("node_type") == "GRAPH" and not expansion_state.get(node_id, False)
        }
        combined = _simplify_rendered_edges(combined, collapsed_containers, compute_container_transits(flat_graph))
    edge_pairs.extend((edge.line, edge.kind) for edge in combined if edge.kind == "input")
    edge_pairs.extend((edge.line, edge.kind) for edge in combined if edge.kind != "input")

    edge_pairs.extend((line, "end") for line in _render_end_edges(flat_graph, expansion_state, id_allocator))

    ordering_indices = [i for i, (_, kind) in enumerate(edge_pairs) if kind == "ordering"]
    lines.extend(line for line, _ in edge_pairs)

    # --- Styling ---
    lines.append("")
    lines.append("    %% Styling")
    lines.extend(_build_style_section(colors, node_class_map, ordering_indices, id_allocator))

    return MermaidDiagram("\n".join(lines))


# =============================================================================
# Internal Helpers
# =============================================================================


def _get_param_type(param: str, flat_graph: nx.DiGraph) -> type | None:
    """Find type annotation for a parameter across all nodes."""
    for _, attrs in flat_graph.nodes(data=True):
        if param in attrs.get("inputs", ()):
            param_type = attrs.get("input_types", {}).get(param)
            if param_type is not None:
                return param_type
    return None


def _build_gated_target_to_gate(flat_graph: nx.DiGraph) -> dict[str, str]:
    """Map each gated target to the gate node that controls it.

    Returns {target_id: gate_id} for all control edges.
    """
    mapping: dict[str, str] = {}
    for source, target, edge_data in flat_graph.edges(data=True):
        if edge_data.get("edge_type") == "control":
            mapping[target] = source
    return mapping


def _get_input_targets(
    params: list[str],
    flat_graph: nx.DiGraph,
    param_to_consumers: dict[str, list[str]],
    expansion_state: dict[str, bool],
    container_entrypoints: dict[str, tuple[str, ...]],
) -> list[str]:
    """Get unique target nodes for input parameters.

    Skips redundant edges to gated targets — nodes only reachable via
    a gate's control edge when that specific gate also consumes the param.
    Falls back to the collapsed container when consumers are hidden.
    """
    gated_target_to_gate = _build_gated_target_to_gate(flat_graph)

    targets: list[str] = []
    seen: set[str] = set()
    for param in params:
        for target in param_to_consumers.get(param, []):
            if target in seen:
                continue
            if expansion_state.get(target) and target in container_entrypoints and not container_entrypoints[target]:
                continue
            # Skip only if the specific gate controlling this target
            # also consumes this same param
            gate = gated_target_to_gate.get(target)
            if gate is not None:
                gate_inputs = set(flat_graph.nodes[gate].get("inputs", ()))
                if param in gate_inputs:
                    continue
            # If consumer is hidden (inside collapsed container), target the container
            if not is_node_visible(target, flat_graph, expansion_state):
                parent = flat_graph.nodes[target].get("parent")
                if parent and not expansion_state.get(parent, False):
                    target = parent
                else:
                    continue
            if target in seen:
                continue
            seen.add(target)
            targets.append(target)
    return targets


def _render_end_edges(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    id_allocator: _MermaidIdAllocator,
) -> list[str]:
    """Render edges from gate nodes to the END node."""
    if not has_end_routing(flat_graph, expansion_state):
        return []

    lines: list[str] = []
    for node_id, attrs in flat_graph.nodes(data=True):
        branch_data = attrs.get("branch_data", {})
        if not branch_data:
            continue
        if not is_node_visible(node_id, flat_graph, expansion_state):
            continue

        emitted = False
        if branch_data.get("when_true") == "END":
            lines.append(_format_control_edge(node_id, "__end__", "True", id_allocator))
            emitted = True
        if branch_data.get("when_false") == "END":
            lines.append(_format_control_edge(node_id, "__end__", "False", id_allocator))
            emitted = True
        if not emitted and "targets" in branch_data:
            targets = branch_data["targets"]
            target_values = targets.values() if isinstance(targets, dict) else targets
            if "END" in target_values:
                lines.append(_format_control_edge(node_id, "__end__", _get_end_control_label(branch_data), id_allocator))

    return lines


def _render_start_edges(start_targets: list[str], id_allocator: _MermaidIdAllocator) -> list[str]:
    """Render edges from START to explicitly configured entrypoints."""
    return [_format_edge("__start__", target, None, id_allocator=id_allocator) for target in start_targets]
