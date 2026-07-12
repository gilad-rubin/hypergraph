#!/usr/bin/env python3
"""Generate debug HTML and summarize the current visualization payload."""

from __future__ import annotations

import argparse
import importlib
import json
import keyword
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


def _validate_identifier(name: str, param_name: str) -> None:
    """Validate string is a safe Python identifier (allows dotted module paths)."""
    parts = name.split(".")
    for part in parts:
        if not part.isidentifier():
            raise ValueError(f"Invalid {param_name}: '{name}' - '{part}' is not a valid identifier")
        if keyword.iskeyword(part):
            raise ValueError(f"Invalid {param_name}: '{name}' - '{part}' is a Python keyword")


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parents[4]
    src_path = root / "src"
    if src_path.is_dir() and str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))


def _load_graph_object(graph_module: str, graph_var: str) -> Any:
    _validate_identifier(graph_module, "graph_module")
    _validate_identifier(graph_var, "graph_var")
    _ensure_src_on_path()

    module = importlib.import_module(graph_module)
    obj = module
    for part in graph_var.split("."):
        obj = getattr(obj, part)
    return obj


def _unwrap_graph(graph_obj: Any) -> Any:
    if hasattr(graph_obj, "to_flat_graph"):
        return graph_obj
    wrapped_graph = getattr(graph_obj, "graph", None)
    return wrapped_graph if hasattr(wrapped_graph, "to_flat_graph") else graph_obj


class _GraphDataParser(HTMLParser):
    """Extract the JSON text from the widget's ``graph-data`` script."""

    def __init__(self) -> None:
        super().__init__()
        self._capturing = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("id") == "graph-data" and attributes.get("type") == "application/json":
            self._capturing = True

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capturing:
            self._capturing = False

    @property
    def graph_data_json(self) -> str:
        """Return the captured JSON text."""
        return "".join(self._chunks)


def _read_embedded_payload(html_path: str) -> dict[str, Any]:
    """Read the exact ``graph-data`` payload embedded in generated HTML."""
    parser = _GraphDataParser()
    parser.feed(Path(html_path).read_text())
    parser.close()
    if not parser.graph_data_json:
        raise RuntimeError(
            "Generated visualization HTML has no graph-data JSON payload.\n\n"
            f"Path: {html_path}\n\n"
            "How to fix: Generate the file through hypergraph.viz.widget.visualize()."
        )
    return json.loads(parser.graph_data_json)


def build_debug_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a compact summary of the widget payload embedded in HTML."""
    meta = payload.get("meta") or {}
    ir = meta.get("ir") or {}
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []

    return {
        "embedded_payload": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "contains_prebuilt_scene": bool(nodes or edges),
        },
        "ir": {
            "schema_version": ir.get("schema_version"),
            "node_count": len(ir.get("nodes") or []),
            "edge_count": len(ir.get("edges") or []),
            "expandable_nodes": ir.get("expandable_nodes") or [],
            "external_input_count": len(ir.get("external_inputs") or []),
        },
        "initial_expansion": meta.get("initial_expansion") or {},
        "scene_derivation": {
            "visible_scene": "browser-derived from embedded GraphIR and initial expansion",
            "browser_debug_state": "browser-derived after scene layout; routing maps are not embedded",
            "python_oracle": ".claude/skills/debug-viz/scripts/inspect_scene.py",
        },
        "render_options": {
            "theme_preference": meta.get("theme_preference", "auto"),
            "show_types": bool(meta.get("show_types", True)),
            "separate_outputs": bool(meta.get("separate_outputs")),
            "show_inputs": bool(meta.get("show_inputs", True)),
            "show_bounded_inputs": bool(meta.get("show_bounded_inputs")),
            "debug_overlays_metadata": bool(meta.get("debug_overlays")),
        },
        "browser_debug": {
            "api": "window.__hypergraphVizDebug",
            "dev_controls": "Set window.__hypergraph_debug_viz = true before rendering.",
        },
    }


def generate_debug_html(
    graph_module: str,
    graph_var: str,
    depth: int = 1,
    separate_outputs: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Generate debug HTML and summarize its exact embedded widget payload.

    Args:
        graph_module: Module path, for example ``mypackage.graphs``.
        graph_var: Variable name of the graph config.
        depth: Initial browser expansion depth.
        separate_outputs: Whether to render outputs as separate DATA nodes.

    Returns:
        A tuple containing the temporary HTML path and debug summary.
    """
    from hypergraph.viz.widget import visualize

    graph = _unwrap_graph(_load_graph_object(graph_module, graph_var))
    bound = graph.bind() if hasattr(graph, "bind") else graph

    with tempfile.NamedTemporaryFile(delete=False, suffix=".html") as tmp:
        html_path = tmp.name

    visualize(
        bound,
        depth=depth,
        separate_outputs=separate_outputs,
        filepath=html_path,
        _debug_overlays=True,
    )

    embedded_payload = _read_embedded_payload(html_path)
    return html_path, build_debug_summary(embedded_payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Debug hypergraph visualization")
    parser.add_argument("module", help="Module path (e.g., 'myapp.graphs')")
    parser.add_argument("variable", help="Graph variable name")
    parser.add_argument("--depth", type=int, default=1, help="Expansion depth")
    parser.add_argument("--separate-outputs", action="store_true", help="Enable separate outputs mode")
    parser.add_argument("--open", action="store_true", help="Open in browser")
    args = parser.parse_args()

    html_path, debug_info = generate_debug_html(
        args.module,
        args.variable,
        args.depth,
        args.separate_outputs,
    )

    print("\n=== HTML Generated ===")
    print(f"Path: {html_path}")

    if args.open:
        subprocess.run(["open", html_path], check=False)

    print("\n=== Embedded Payload Summary ===")
    print(json.dumps(debug_info, indent=2))
    print("\nThe browser debug API is available at window.__hypergraphVizDebug after layout.")


if __name__ == "__main__":
    main()
