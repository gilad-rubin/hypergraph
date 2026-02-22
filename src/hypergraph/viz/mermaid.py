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

import re
from typing import Any

import networkx as nx

from hypergraph.viz._common import (
    build_expansion_state,
    build_output_to_producer_map,
    build_param_to_consumer_map,
    is_descendant_of,
    is_node_visible,
)
from hypergraph.viz.renderer._format import format_type
from hypergraph.viz.renderer.nodes import build_input_groups, has_end_routing
from hypergraph.viz.renderer.scope import (
    find_container_entrypoints,
    find_internal_producer_for_output,
)

# =============================================================================
# Constants
# =============================================================================

_VALID_DIRECTIONS = {"TD", "TB", "BT", "LR", "RL"}

# Characters unsafe in Mermaid IDs (anything not alphanumeric or underscore)
_UNSAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_]")

# Mermaid reserved words that cannot be used as bare node IDs
_RESERVED_WORDS = frozenset({
    "end", "subgraph", "direction", "click", "style", "classDef", "class",
    "linkStyle", "graph", "flowchart",
})

DEFAULT_COLORS: dict[str, dict[str, str]] = {
    "function": {
        "fill": "#E8F5E8", "stroke": "#388E3C", "stroke-width": "2px", "color": "#1B5E20",
    },
    "container": {
        "fill": "#FFF3E0", "stroke": "#F57C00", "stroke-width": "2px", "color": "#E65100",
    },
    "branch": {
        "fill": "#FFF8E1", "stroke": "#FBC02D", "stroke-width": "2px", "color": "#F57F17",
    },
    "input": {
        "fill": "#E3F2FD", "stroke": "#1976D2", "stroke-width": "2px", "color": "#0D47A1",
    },
    "data": {
        "fill": "#F3E5F5", "stroke": "#7B1FA2", "stroke-width": "2px", "color": "#4A148C",
    },
    "end": {
        "fill": "#ECEFF1", "stroke": "#546E7A", "stroke-width": "2px", "color": "#263238",
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
    "END": "end",
}

# =============================================================================
# MermaidDiagram (notebook-renderable result)
# =============================================================================


class MermaidDiagram:
    """A Mermaid diagram that renders in Jupyter notebooks.

    Rendering:

    - **JupyterLab 4.1+ / Notebook 7.1+**: native ``text/vnd.mermaid`` MIME
      type — fully local, zero network requests.
    - **Terminal / plain**: raw Mermaid source via ``text/plain``.

    Example:
        >>> diagram = graph.to_mermaid()
        >>> diagram                  # renders in notebook
        >>> print(diagram)           # prints raw Mermaid source
        >>> diagram.source           # raw string
    """

    def __init__(self, source: str) -> None:
        self.source = source

    def __str__(self) -> str:
        return self.source

    def __repr__(self) -> str:
        lines = self.source.split("\n")
        preview = lines[0] if lines else ""
        return f"MermaidDiagram({preview!r}, {len(lines)} lines)"

    def __contains__(self, item: str) -> bool:
        return item in self.source

    def startswith(self, prefix: str) -> bool:
        """Delegate to source string."""
        return self.source.startswith(prefix)

    def _repr_mimebundle_(self, **kwargs: Any) -> dict[str, str]:
        """Provide MIME types for notebook rendering.

        JupyterLab 4.1+ uses text/vnd.mermaid for native rendering.
        """
        return {
            "text/vnd.mermaid": self.source,
            "text/plain": str(self),
        }


# =============================================================================
# ID Sanitization
# =============================================================================


def _sanitize_id(node_id: str) -> str:
    """Convert a node ID to a Mermaid-safe identifier.

    Replaces '/' with '__', strips unsafe chars, and prefixes with 'n_'
    to avoid collisions with Mermaid reserved words or digit-leading IDs.
    """
    safe = node_id.replace("/", "__")
    safe = _UNSAFE_ID_RE.sub("_", safe)
    if safe and (safe.lower() in _RESERVED_WORDS or safe[0:1].isdigit()):
        safe = f"n_{safe}"
    return safe or "n_empty"


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
    for param, ptype in zip(params, param_types):
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
    "END": ('(["', '"])'),
}


def _format_node(safe_id: str, label: str, node_type: str) -> str:
    """Format a complete Mermaid node definition."""
    open_delim, close_delim = _SHAPE_DELIMITERS.get(node_type, ('["', '"]'))
    return f"    {safe_id}{open_delim}{label}{close_delim}"


# =============================================================================
# Edge Rendering
# =============================================================================


def _render_merged_edges(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> list[str]:
    """Render edges in merged output mode (no DATA intermediaries).

    Mirrors add_merged_output_edges() from renderer/edges.py.
    """
    lines: list[str] = []
    output_to_producer = build_output_to_producer_map(
        flat_graph, expansion_state, use_deepest=True,
    )
    param_to_consumers = build_param_to_consumer_map(
        flat_graph, expansion_state,
    )
    seen_edges: set[tuple[str, str, str]] = set()

    for source, target, edge_data in flat_graph.edges(data=True):
        if not is_node_visible(source, flat_graph, expansion_state):
            continue

        edge_type = edge_data.get("edge_type", "data")
        value_names = edge_data.get("value_names", [])

        if edge_type == "control":
            actual_target = _resolve_control_target(
                source, target, flat_graph, expansion_state,
            )
            if actual_target is None:
                continue
            label = _get_control_label(source, target, flat_graph)
            edge_key = (_sanitize_id(source), _sanitize_id(actual_target), label or "")
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            lines.append(_format_edge(source, actual_target, label))
            continue

        if edge_type == "ordering":
            if not is_node_visible(target, flat_graph, expansion_state):
                continue
            value_name = value_names[0] if value_names else ""
            edge_key = (_sanitize_id(source), _sanitize_id(target), f"ord_{value_name}")
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            lines.append(_format_ordering_edge(source, target, value_name))
            continue

        # Data edges
        values = value_names if value_names else [""]
        for value_name in values:
            actual_source = _resolve_data_source(
                source, value_name, flat_graph, expansion_state, output_to_producer,
            )
            actual_target = _resolve_data_target(
                target, value_name, flat_graph, expansion_state, param_to_consumers,
            )
            if actual_source is None or actual_target is None:
                continue
            if actual_source == actual_target:
                continue
            edge_key = (_sanitize_id(actual_source), _sanitize_id(actual_target), value_name)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            lines.append(_format_edge(actual_source, actual_target, None))

    return lines


def _render_separate_edges(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> list[str]:
    """Render edges in separate output mode (with DATA intermediaries).

    Mirrors add_separate_output_edges() from renderer/edges.py.
    """
    lines: list[str] = []
    output_to_producer = build_output_to_producer_map(
        flat_graph, expansion_state, use_deepest=True,
    )
    seen_edges: set[tuple[str, ...]] = set()

    # Function → DATA edges
    for node_id, attrs in flat_graph.nodes(data=True):
        if not is_node_visible(node_id, flat_graph, expansion_state):
            continue
        if attrs.get("node_type") == "GRAPH" and expansion_state.get(node_id, False):
            continue
        for output_name in attrs.get("outputs", ()):
            data_id = f"data_{node_id}_{output_name}"
            edge_key = (_sanitize_id(node_id), _sanitize_id(data_id))
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                lines.append(_format_edge(node_id, data_id, None))

    # DATA → consumer edges + control/ordering edges
    for source, target, edge_data in flat_graph.edges(data=True):
        if not is_node_visible(source, flat_graph, expansion_state):
            continue
        if not is_node_visible(target, flat_graph, expansion_state):
            continue

        edge_type = edge_data.get("edge_type", "data")
        value_names = edge_data.get("value_names", [])

        if edge_type == "data":
            for value_name in (value_names or [""]):
                if not value_name:
                    continue
                # Resolve source to internal producer for expanded graphs
                actual_source = _resolve_data_source(
                    source, value_name, flat_graph, expansion_state,
                    output_to_producer,
                )
                if actual_source is None:
                    continue
                data_id = f"data_{actual_source}_{value_name}"
                edge_key = (_sanitize_id(data_id), _sanitize_id(target))
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    lines.append(_format_edge(data_id, target, value_name))

        elif edge_type == "ordering":
            value_name = value_names[0] if value_names else ""
            edge_key = (_sanitize_id(source), _sanitize_id(target), f"ord_{value_name}")
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                lines.append(_format_ordering_edge(source, target, value_name))

        elif edge_type == "control":
            actual_target = _resolve_control_target(
                source, target, flat_graph, expansion_state,
            )
            if actual_target is None:
                continue
            label = _get_control_label(source, target, flat_graph)
            edge_key = (_sanitize_id(source), _sanitize_id(actual_target), label or "")
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                lines.append(_format_edge(source, actual_target, label))

    return lines


# =============================================================================
# Edge Helpers
# =============================================================================


def _format_edge(
    source: str,
    target: str,
    label: str | None,
) -> str:
    """Format a solid-arrow Mermaid edge."""
    s, t = _sanitize_id(source), _sanitize_id(target)
    if label:
        return f"    {s} -->|{label}| {t}"
    return f"    {s} --> {t}"


def _format_ordering_edge(source: str, target: str, label: str) -> str:
    """Format a dotted-arrow Mermaid edge (for ordering/emit edges)."""
    s, t = _sanitize_id(source), _sanitize_id(target)
    if label:
        return f"    {s} -.->|{label}| {t}"
    return f"    {s} -.-> {t}"


def _resolve_control_target(
    source: str,
    target: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> str | None:
    """Resolve the actual target for a control edge, entering containers."""
    actual_target = target
    target_attrs = flat_graph.nodes.get(target, {})
    if target_attrs.get("node_type") == "GRAPH" and expansion_state.get(target, False):
        entrypoints = find_container_entrypoints(target, flat_graph, expansion_state)
        if entrypoints:
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
) -> str | None:
    """Resolve actual source for a data edge, exiting expanded containers."""
    actual_source = source
    source_attrs = flat_graph.nodes.get(source, {})
    if source_attrs.get("node_type") == "GRAPH" and expansion_state.get(source, False):
        if value_name:
            internal = output_to_producer.get(value_name)
            if internal and internal != source and is_descendant_of(internal, source, flat_graph):
                actual_source = internal
            else:
                found = find_internal_producer_for_output(
                    source, value_name, flat_graph, expansion_state,
                )
                if found:
                    actual_source = found
    if not is_node_visible(actual_source, flat_graph, expansion_state):
        return None
    return actual_source


def _resolve_data_target(
    target: str,
    value_name: str,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    param_to_consumers: dict[str, list[str]],
) -> str | None:
    """Resolve actual target for a data edge, entering expanded containers."""
    actual_target = target
    target_attrs = flat_graph.nodes.get(target, {})
    if target_attrs.get("node_type") == "GRAPH" and expansion_state.get(target, False):
        if value_name:
            consumers = param_to_consumers.get(value_name, [])
            internal = [
                c for c in consumers
                if c != target and is_descendant_of(c, target, flat_graph)
            ]
            if internal:
                actual_target = internal[0]
            else:
                entry = find_container_entrypoints(target, flat_graph, expansion_state)
                if entry:
                    actual_target = entry[0]
    if not is_node_visible(actual_target, flat_graph, expansion_state):
        return None
    return actual_target


def _get_control_label(
    source: str,
    target: str,
    flat_graph: nx.DiGraph,
) -> str | None:
    """Get True/False label for ifelse control edges."""
    source_attrs = flat_graph.nodes.get(source, {})
    branch_data = source_attrs.get("branch_data", {})
    if not branch_data:
        return None
    if "when_true" in branch_data:
        if target == branch_data["when_true"]:
            return "True"
        if target == branch_data["when_false"]:
            return "False"
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
    indent: int = 1,
) -> list[str]:
    """Render a subgraph block for an expanded GRAPH node."""
    attrs = flat_graph.nodes[container_id]
    safe_id = _sanitize_id(container_id)
    label = _escape_label(attrs.get("label", container_id))
    prefix = "    " * indent

    lines = [f"{prefix}subgraph {safe_id} [\"{label}\"]"]

    # Render child nodes
    children = [
        (nid, nattrs) for nid, nattrs in flat_graph.nodes(data=True)
        if nattrs.get("parent") == container_id
        and not nattrs.get("hide", False)
    ]

    for child_id, child_attrs in children:
        child_type = child_attrs.get("node_type", "FUNCTION")

        # Nested subgraph
        if child_type == "GRAPH" and expansion_state.get(child_id, False):
            lines.extend(_render_subgraph_block(
                child_id, flat_graph, expansion_state,
                show_types, separate_outputs, node_class_map,
                indent=indent + 1,
            ))
        else:
            child_label = _build_label(child_attrs, show_types, separate_outputs)
            mermaid_type = "GRAPH" if child_type == "GRAPH" else child_type
            lines.append("    " * (indent + 1) + _format_node(
                _sanitize_id(child_id), child_label, mermaid_type,
            ).strip())
            node_class_map[child_id] = _NODE_TYPE_TO_CLASS.get(mermaid_type, "function")

    lines.append(f"{prefix}end")
    return lines


# =============================================================================
# Style Section
# =============================================================================


def _build_style_section(
    colors: dict[str, dict[str, str]] | None,
    node_class_map: dict[str, str],
    ordering_edge_indices: list[int],
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
        class_to_ids.setdefault(cls, []).append(_sanitize_id(node_id))

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
    show_types: bool = False,
    separate_outputs: bool = False,
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
    input_spec = flat_graph.graph.get("input_spec", {})
    bound_params = set(input_spec.get("bound", {}).keys())
    param_to_consumers = build_param_to_consumer_map(flat_graph, expansion_state)

    lines: list[str] = [f"flowchart {direction}"]
    node_class_map: dict[str, str] = {}

    # --- Input nodes ---
    input_groups = build_input_groups(input_spec, param_to_consumers, bound_params)
    if input_groups:
        lines.append("    %% Inputs")
    for group in input_groups:
        params = group["params"]
        param_types = [
            format_type(_get_param_type(p, flat_graph))
            for p in params
        ]
        label = _build_input_label(params, param_types, show_types)

        if len(params) == 1:
            node_id = f"input_{params[0]}"
            node_type = "INPUT"
        else:
            node_id = f"input_group_{'_'.join(params)}"
            node_type = "INPUT_GROUP"

        lines.append(_format_node(_sanitize_id(node_id), label, node_type))
        node_class_map[node_id] = "input"

    # --- Function / Graph / Branch nodes ---
    lines.append("    %% Nodes")
    # Track which containers are expanded so we skip their children
    # (they're rendered inside the subgraph block, not at top level)
    expanded_containers = {
        nid for nid, expanded in expansion_state.items() if expanded
    }

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
            lines.extend(_render_subgraph_block(
                node_id, flat_graph, expansion_state,
                show_types, separate_outputs, node_class_map,
            ))
            continue

        label = _build_label(attrs, show_types, separate_outputs)
        lines.append(_format_node(_sanitize_id(node_id), label, node_type))
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
                data_id = f"data_{node_id}_{output_name}"
                data_label = _build_data_label(
                    output_name,
                    format_type(output_types.get(output_name)),
                    show_types,
                )
                lines.append(_format_node(_sanitize_id(data_id), data_label, "DATA"))
                node_class_map[data_id] = "data"

    # --- END node ---
    if has_end_routing(flat_graph, expansion_state):
        end_id = "__end__"
        lines.append(_format_node(_sanitize_id(end_id), "End", "END"))
        node_class_map[end_id] = "end"

    # --- Input → consumer edges ---
    lines.append("    %% Edges")
    for group in input_groups:
        params = group["params"]
        if len(params) == 1:
            input_node_id = f"input_{params[0]}"
        else:
            input_node_id = f"input_group_{'_'.join(params)}"

        targets = _get_input_targets(params, flat_graph, param_to_consumers, expansion_state)
        for tgt in targets:
            lines.append(_format_edge(input_node_id, tgt, None))

    # --- Internal edges ---
    if separate_outputs:
        edge_lines = _render_separate_edges(flat_graph, expansion_state)
    else:
        edge_lines = _render_merged_edges(flat_graph, expansion_state)
    lines.extend(edge_lines)

    # --- END edges ---
    end_edges = _render_end_edges(flat_graph, expansion_state)
    lines.extend(end_edges)

    # Track ordering edge indices for linkStyle
    ordering_indices = _find_ordering_edge_indices(lines)

    # --- Styling ---
    lines.append("")
    lines.append("    %% Styling")
    lines.extend(_build_style_section(colors, node_class_map, ordering_indices))

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
            lines.append(_format_edge(node_id, "__end__", "True"))
            emitted = True
        if branch_data.get("when_false") == "END":
            lines.append(_format_edge(node_id, "__end__", "False"))
            emitted = True
        if not emitted and "targets" in branch_data:
            targets = branch_data["targets"]
            target_values = targets.values() if isinstance(targets, dict) else targets
            if "END" in target_values:
                lines.append(_format_edge(node_id, "__end__", None))

    return lines


def _find_ordering_edge_indices(lines: list[str]) -> list[int]:
    """Find 0-based edge indices for ordering (dotted) edges.

    Mermaid linkStyle uses the order edges appear in the document.
    We count all edge lines (containing --> or -.-> ) and track which
    are ordering edges.
    """
    indices: list[int] = []
    edge_index = 0
    for line in lines:
        stripped = line.strip()
        if "-->" in stripped or "-.->" in stripped:
            if "-.->" in stripped:
                indices.append(edge_index)
            edge_index += 1
    return indices
