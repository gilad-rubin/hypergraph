#!/usr/bin/env python3
"""Inspect a scene derived from the current compact visualization IR."""

from __future__ import annotations

import argparse
import importlib
import json
import keyword
import sys
from pathlib import Path
from typing import Any


def _validate_identifier(value: str, name: str) -> None:
    """Validate that a string is a safe Python identifier (allows dotted paths)."""
    parts = value.split(".")
    for part in parts:
        if not part.isidentifier():
            raise ValueError(f"Invalid {name}: '{value}' - '{part}' is not a valid identifier")
        if keyword.iskeyword(part):
            raise ValueError(f"Invalid {name}: '{value}' - '{part}' is a Python keyword")


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


def inspect_scene(
    graph_obj: Any,
    *,
    expanded: bool,
    separate_outputs: bool = False,
    show_inputs: bool = True,
    show_bounded_inputs: bool = False,
) -> dict[str, Any]:
    """Build and report the visible scene for an explicit expansion state."""
    from hypergraph.viz.renderer.ir_builder import build_graph_ir
    from hypergraph.viz.scene_builder import build_initial_scene

    graph = _unwrap_graph(graph_obj)
    bound = graph.bind() if hasattr(graph, "bind") else graph
    flat_graph = bound.to_flat_graph() if hasattr(bound, "to_flat_graph") else bound
    ir = build_graph_ir(flat_graph)
    expansion_state = {node_id: expanded for node_id in ir.expandable_nodes}
    scene = build_initial_scene(
        ir,
        expansion_state=expansion_state,
        separate_outputs=separate_outputs,
        show_inputs=show_inputs,
        show_bounded_inputs=show_bounded_inputs,
    )

    return {
        "schema_version": ir.schema_version,
        "expansion_state": expansion_state,
        "separate_outputs": separate_outputs,
        "show_inputs": show_inputs,
        "show_bounded_inputs": show_bounded_inputs,
        "visible_nodes": [node for node in scene["nodes"] if not node.get("hidden", False)],
        "visible_edges": [edge for edge in scene["edges"] if not edge.get("hidden", False)],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect a scene built from the current GraphIR")
    parser.add_argument("module", help="Module path (e.g., 'myapp.graphs')")
    parser.add_argument("variable", help="Graph variable name")
    state = parser.add_mutually_exclusive_group(required=True)
    state.add_argument("--expanded", action="store_true", help="Expand every nested graph")
    state.add_argument("--collapsed", action="store_true", help="Collapse every nested graph")
    parser.add_argument("--separate-outputs", action="store_true", help="Show output DATA nodes")
    parser.add_argument("--hide-inputs", action="store_true", help="Hide external input nodes")
    parser.add_argument("--show-bounded-inputs", action="store_true", help="Show bound input nodes")
    args = parser.parse_args()

    graph = _load_graph_object(args.module, args.variable)
    report = inspect_scene(
        graph,
        expanded=args.expanded,
        separate_outputs=args.separate_outputs,
        show_inputs=not args.hide_inputs,
        show_bounded_inputs=args.show_bounded_inputs,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
