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


def build_debug_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, JSON-serializable summary of a renderer payload."""
    meta = payload.get("meta") or {}
    ir = meta.get("ir") or {}
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []

    return {
        "ir": {
            "schema_version": ir.get("schema_version"),
            "node_count": len(ir.get("nodes") or []),
            "edge_count": len(ir.get("edges") or []),
            "expandable_nodes": ir.get("expandable_nodes") or [],
            "external_input_count": len(ir.get("external_inputs") or []),
        },
        "initial_expansion": meta.get("initial_expansion") or {},
        "initial_scene": {
            "node_ids": [node.get("id") for node in nodes],
            "nodes": [
                {
                    "id": node.get("id"),
                    "node_type": (node.get("data") or {}).get("nodeType"),
                    "parent": node.get("parentNode"),
                    "hidden": bool(node.get("hidden")),
                    "owner_container": (node.get("data") or {}).get("ownerContainer"),
                    "deepest_owner_container": (node.get("data") or {}).get("deepestOwnerContainer"),
                }
                for node in nodes
            ],
            "edges": [
                {
                    "id": edge.get("id"),
                    "source": edge.get("source"),
                    "target": edge.get("target"),
                    "value_name": (edge.get("data") or {}).get("valueName"),
                    "edge_type": (edge.get("data") or {}).get("edgeType"),
                }
                for edge in edges
            ],
        },
        "routing_maps": {
            "output_to_producer": meta.get("output_to_producer") or {},
            "param_to_consumer": meta.get("param_to_consumer") or {},
            "node_to_parent": meta.get("node_to_parent") or {},
        },
        "render_options": {
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
    """Generate debug HTML and return its current IR/scene summary.

    Args:
        graph_module: Module path, for example ``mypackage.graphs``.
        graph_var: Variable name of the graph config.
        depth: Expansion depth for the initial scene.
        separate_outputs: Whether to render outputs as separate DATA nodes.

    Returns:
        A tuple containing the temporary HTML path and debug summary.
    """
    from hypergraph.viz.renderer import render_graph
    from hypergraph.viz.widget import visualize

    graph = _unwrap_graph(_load_graph_object(graph_module, graph_var))
    bound = graph.bind() if hasattr(graph, "bind") else graph
    flat_graph = bound.to_flat_graph()

    payload = render_graph(
        flat_graph,
        depth=depth,
        separate_outputs=separate_outputs,
        debug_overlays=True,
    )

    with tempfile.NamedTemporaryFile(delete=False, suffix=".html") as tmp:
        html_path = tmp.name

    visualize(
        bound,
        depth=depth,
        separate_outputs=separate_outputs,
        filepath=html_path,
        _debug_overlays=True,
    )

    return html_path, build_debug_summary(payload)


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

    print("\n=== Debug Info ===")
    print(json.dumps(debug_info, indent=2))
    print("\nThe browser debug API is available at window.__hypergraphVizDebug after layout.")


if __name__ == "__main__":
    main()
