#!/usr/bin/env python3
"""Inspect precomputed edges for a specific expansion state key."""

from __future__ import annotations

import argparse
import importlib
import json
import keyword
import sys
from pathlib import Path


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


def _load_graph_object(graph_module: str, graph_var: str):
    _validate_identifier(graph_module, "graph_module")
    _validate_identifier(graph_var, "graph_var")
    _ensure_src_on_path()

    module = importlib.import_module(graph_module)
    obj = module
    for part in graph_var.split("."):
        obj = getattr(obj, part)
    return obj


def render_edges_by_state(
    graph_module: str,
    graph_var: str,
    depth: int,
    separate_outputs: bool,
) -> dict:
    from hypergraph.viz.renderer import render_graph

    graph_obj = _load_graph_object(graph_module, graph_var)

    if hasattr(graph_obj, "graph"):
        graph = graph_obj.graph
    elif hasattr(graph_obj, "to_flat_graph"):
        graph = graph_obj
    else:
        graph = graph_obj

    bound = graph.bind() if hasattr(graph, "bind") else graph

    flat_graph = bound.to_flat_graph()
    result = render_graph(
        flat_graph,
        depth=depth,
        separate_outputs=separate_outputs,
    )

    return result.get("meta", {})


def expansion_state_key(expandable_nodes: list[str], expanded: bool, separate_outputs: bool) -> str:
    sep_key = "sep:1" if separate_outputs else "sep:0"
    if not expandable_nodes:
        return sep_key
    parts = [f"{node_id}:{1 if expanded else 0}" for node_id in expandable_nodes]
    return ",".join(parts) + "|" + sep_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect edgesByState for a graph")
    parser.add_argument("module", help="Module path (e.g., 'myapp.graphs')")
    parser.add_argument("variable", help="Graph variable name")
    parser.add_argument("--depth", type=int, default=0, help="Expansion depth used for render_graph")
    parser.add_argument("--separate-outputs", action="store_true", help="Use sep:1 in key selection")
    parser.add_argument("--list-keys", action="store_true", help="List available edgesByState keys")
    parser.add_argument("--key", help="Explicit edgesByState key to print")
    parser.add_argument("--expanded", action="store_true", help="Use fully-expanded key")
    parser.add_argument("--collapsed", action="store_true", help="Use fully-collapsed key")

    args = parser.parse_args()

    meta = render_edges_by_state(args.module, args.variable, args.depth, args.separate_outputs)
    edges_by_state = meta.get("edgesByState", {})
    expandable_nodes = meta.get("expandableNodes", [])

    if args.list_keys:
        for key in sorted(edges_by_state.keys()):
            print(f"{key} ({len(edges_by_state[key])} edges)")
        return

    if args.key:
        key = args.key
    elif args.expanded or args.collapsed:
        key = expansion_state_key(expandable_nodes, args.expanded, args.separate_outputs)
    else:
        key = expansion_state_key(expandable_nodes, False, args.separate_outputs)

    if key not in edges_by_state:
        available = ", ".join(sorted(edges_by_state.keys()))
        raise SystemExit(f"Key '{key}' not found. Available: {available}")

    edges = edges_by_state[key]
    print(
        json.dumps(
            {
                "key": key,
                "edge_count": len(edges),
                "edges": [
                    {
                        "id": e.get("id"),
                        "source": e.get("source"),
                        "target": e.get("target"),
                        "valueName": (e.get("data") or {}).get("valueName"),
                        "edgeType": (e.get("data") or {}).get("edgeType"),
                    }
                    for e in edges
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
