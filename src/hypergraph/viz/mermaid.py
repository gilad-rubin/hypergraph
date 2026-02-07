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

import html as html_module
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
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
    find_container_entry_points,
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
    "graph": {
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
    "GRAPH": "graph",
    "BRANCH": "branch",
    "INPUT": "input",
    "INPUT_GROUP": "input",
    "DATA": "data",
    "END": "end",
}

# =============================================================================
# MermaidDiagram (notebook-renderable result)
# =============================================================================


def _load_beautiful_mermaid_js() -> str:
    """Load the bundled beautiful-mermaid browser JS (cached after first call)."""
    js_path = Path(__file__).parent / "assets" / "beautiful-mermaid.browser.global.js"
    return js_path.read_text(encoding="utf-8")


class MermaidDiagram:
    """A Mermaid diagram that renders in Jupyter notebooks.

    Rendering priority:

    1. **JupyterLab 4.1+ / Notebook 7.1+**: native ``text/vnd.mermaid`` MIME
       type — fully local, zero network requests.
    2. **VSCode / older Jupyter**: ``text/html`` fallback using a bundled copy
       of beautiful-mermaid (renders Mermaid → SVG locally in an iframe).
    3. **Terminal / plain**: raw Mermaid source via ``text/plain``.

    No data is sent to external services. No CDN. No internet required.

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

    def _repr_html_(self) -> str:
        """Render via bundled beautiful-mermaid (VSCode / older Jupyter fallback)."""
        js = _load_beautiful_mermaid_js()
        adapted = _adapt_for_beautiful_mermaid(self.source)
        iframe_id = f"mermaid-{uuid.uuid4().hex[:8]}"

        inner_html = f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ margin: 0; display: flex; justify-content: center; padding: 16px; }}
</style>
</head>
<body>
<div id="diagram"></div>
<script>{js}</script>
<script>
(async () => {{
  try {{
    const src = {_js_string_literal(adapted)};
    const T = beautifulMermaid.THEMES || {{}};
    const isDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches;
    const base = isDark ? (T['github-dark'] || {{}}) : (T['github-light'] || {{}});
    const opts = {{ ...base, font: 'system-ui', layerSpacing: 60 }};
    document.body.style.background = isDark ? (opts.bg || '#0d1117') : 'white';
    let svg = await beautifulMermaid.renderMermaid(src, opts);
    svg = svg.replace(/@import url\\([^)]*fonts\\.googleapis[^)]*\\);?/g, '');
    document.getElementById('diagram').innerHTML = svg;
    const h = document.getElementById('diagram').scrollHeight + 40;
    window.parent.postMessage({{ type: 'mermaid-resize', id: '{iframe_id}', height: h }}, '*');
  }} catch (e) {{
    document.getElementById('diagram').textContent = e.message;
  }}
}})();
</script>
</body>
</html>"""

        escaped_inner = html_module.escape(inner_html, quote=True)
        return (
            f'<iframe id="{iframe_id}" srcdoc="' + escaped_inner + '" '
            'frameborder="0" width="100%" height="100" '
            'style="border: none; max-width: 100%; '
            'border-radius: 8px;" '
            'sandbox="allow-scripts">'
            "</iframe>"
            "<script>"
            "window.addEventListener('message', function(e) {"
            f"  if (e.data && e.data.type === 'mermaid-resize' && e.data.id === '{iframe_id}') {{"
            f"    document.getElementById('{iframe_id}').style.height = e.data.height + 'px';"
            "  }"
            "});"
            "</script>"
        )

    def _repr_mimebundle_(self, **kwargs: Any) -> dict[str, str]:
        """Provide multiple MIME types; notebook picks the best one.

        JupyterLab 4.1+ uses text/vnd.mermaid (native, no JS).
        VSCode / older Jupyter uses text/html (bundled beautiful-mermaid).
        """
        return {
            "text/vnd.mermaid": self.source,
            "text/html": self._repr_html_(),
            "text/plain": str(self),
        }

    def to_ascii(self) -> str:
        """Render the diagram as Unicode box-drawing art for terminal display.

        Requires Node.js and the ``beautiful-mermaid`` npm package::

            npm install -g beautiful-mermaid

        Returns:
            Unicode string with the rendered diagram.

        Raises:
            RuntimeError: If Node.js or beautiful-mermaid is not available.
        """
        return _render_ascii(self.source)


def _js_string_literal(source: str) -> str:
    """Encode a Python string as a safe JS template literal."""
    escaped = source.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    return f"`{escaped}`"


def _adapt_for_beautiful_mermaid(source: str) -> str:
    """Adapt standard Mermaid syntax for beautiful-mermaid's parser.

    beautiful-mermaid doesn't accept quoted labels inside shape delimiters
    (e.g. ``["label"]`` must become ``[label]``). It also doesn't support
    ``<br/>`` or the parallelogram ``[/"label"/]`` shape.
    """
    s = source
    s = re.sub(r'\[\["([^"]*?)"\]\]', r'[[\1]]', s)       # double-border
    s = re.sub(r'\(\["([^"]*?)"\]\)', r'([\1])', s)        # stadium
    s = re.sub(r'\{\{"([^"]*?)"\}\}', r'{{\1}}', s)         # hexagon
    s = re.sub(r'\[/"([^"]*?)"/\]', r'[\1]', s)            # parallelogram → rect

    def _escape_inner_brackets(m: re.Match[str]) -> str:
        label = m.group(1).replace("[", "\u27e8").replace("]", "\u27e9")
        return f"[{label}]"

    s = re.sub(r'\["([^"]*?)"\]', _escape_inner_brackets, s)  # rectangle
    s = s.replace("<br/>", " \u00b7 ")                         # line breaks
    return s


def _get_global_npm_root() -> str | None:
    """Get the global npm root directory, or None if unavailable."""
    try:
        result = subprocess.run(
            ["npm", "root", "-g"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _render_ascii(source: str) -> str:
    """Render Mermaid source as Unicode box-drawing art via Node.js.

    Uses ``renderMermaidAscii`` from the ``beautiful-mermaid`` npm package.
    Searches both local and global node_modules.
    """
    adapted = _adapt_for_beautiful_mermaid(source)
    script = (
        "const bm = require('beautiful-mermaid');"
        "try { const r = bm.renderMermaidAscii(" + json.dumps(adapted) + ");"
        "process.stdout.write(typeof r === 'string' ? r : '');"
        "} catch(e) { process.stderr.write(e.message); process.exit(1); }"
    )
    env = dict(os.environ)
    global_root = _get_global_npm_root()
    if global_root:
        existing = env.get("NODE_PATH", "")
        env["NODE_PATH"] = f"{global_root}:{existing}" if existing else global_root

    try:
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except FileNotFoundError:
        msg = (
            "Node.js is required for ASCII rendering. "
            "Install it from https://nodejs.org"
        )
        raise RuntimeError(msg) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("ASCII rendering timed out") from None

    if result.returncode != 0:
        error = result.stderr.strip() or "unknown error"
        if "beautiful-mermaid" in error or "Cannot find" in error:
            msg = (
                "beautiful-mermaid npm package not found. "
                "Install with: npm install -g beautiful-mermaid"
            )
            raise RuntimeError(msg)
        raise RuntimeError(f"ASCII rendering failed: {error}")
    return result.stdout


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
    if safe.lower() in _RESERVED_WORDS or safe[0:1].isdigit():
        safe = f"n_{safe}"
    return safe


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

    return label + "<br/>" + "<br/>".join(type_parts)


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
    "INPUT": ('(["', '"])',),
    "INPUT_GROUP": ('(["', '"])'),
    "DATA": ('[/"', '"/]'),
    "END": ('(["', '"])'),
}

# Fix INPUT — tuple above was accidentally 1-element
_SHAPE_DELIMITERS["INPUT"] = ('(["', '"])') # type: ignore[assignment]


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
    seen_edges: set[tuple[str, str]] = set()

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
                data_id = f"data_{source}_{value_name}"
                edge_key = (_sanitize_id(data_id), _sanitize_id(target))
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    lines.append(_format_edge(data_id, target, value_name))

        elif edge_type == "ordering":
            value_name = value_names[0] if value_names else ""
            edge_key = (_sanitize_id(source), _sanitize_id(target))
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
            edge_key = (_sanitize_id(source), _sanitize_id(actual_target))
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
        entry_points = find_container_entry_points(target, flat_graph, expansion_state)
        if entry_points:
            actual_target = entry_points[0]
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
                entry = find_container_entry_points(target, flat_graph, expansion_state)
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
    effective = dict(DEFAULT_COLORS)
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

        targets = _get_input_targets(params, flat_graph, param_to_consumers)
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


def _find_gated_targets(flat_graph: nx.DiGraph) -> set[str]:
    """Find nodes that are control-edge targets of gate nodes.

    These nodes are only reachable via a gate's routing decision,
    so input edges directly to them are redundant.
    """
    gated: set[str] = set()
    for _, target, edge_data in flat_graph.edges(data=True):
        if edge_data.get("edge_type") == "control":
            gated.add(target)
    return gated


def _find_gate_inputs(flat_graph: nx.DiGraph) -> set[str]:
    """Find input parameter names consumed by gate (BRANCH) nodes."""
    gate_params: set[str] = set()
    for _, attrs in flat_graph.nodes(data=True):
        if attrs.get("node_type") == "BRANCH":
            gate_params.update(attrs.get("inputs", ()))
    return gate_params


def _get_input_targets(
    params: list[str],
    flat_graph: nx.DiGraph,
    param_to_consumers: dict[str, list[str]],
) -> list[str]:
    """Get unique target nodes for input parameters.

    Skips redundant edges to gated targets — nodes only reachable via
    a gate's control edge when the gate itself consumes the same input.
    """
    gated_targets = _find_gated_targets(flat_graph)
    gate_inputs = _find_gate_inputs(flat_graph)

    targets: list[str] = []
    seen: set[str] = set()
    for param in params:
        for target in param_to_consumers.get(param, []):
            if target in seen:
                continue
            # Skip if target is gated AND a gate consumes this same param
            if target in gated_targets and param in gate_inputs:
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

        label = None
        has_end = False
        if branch_data.get("when_false") == "END":
            label = "False"
            has_end = True
        elif branch_data.get("when_true") == "END":
            label = "True"
            has_end = True
        elif "targets" in branch_data:
            targets = branch_data["targets"]
            target_values = targets.values() if isinstance(targets, dict) else targets
            if "END" in target_values:
                has_end = True

        if has_end:
            lines.append(_format_edge(node_id, "__end__", label))

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
