#!/usr/bin/env python3
"""Inspect precomputed edges for a specific expansion state key."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


import re

# Safe identifier pattern for Python module/variable names
_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*$')


def _validate_identifier(value: str, name: str) -> None:
    """Validate that a string is a safe Python identifier."""
    if not _SAFE_IDENTIFIER.match(value):
        raise ValueError(
            f"{name} must be a valid Python identifier, got: {value!r}"
        )
    # Additional safety: reject any suspicious characters
    if any(c in value for c in (';', '(', ')', ' ', '\n', '\t', '#', '=')):
        raise ValueError(
            f"{name} contains invalid characters: {value!r}"
        )


def render_edges_by_state(
    graph_module: str,
    graph_var: str,
    depth: int,
    separate_outputs: bool,
) -> dict:
    # Validate inputs to prevent code injection
    _validate_identifier(graph_module, "graph_module")
    _validate_identifier(graph_var, "graph_var")

    code = f'''
import sys
import json
sys.path.insert(0, "src")

from {graph_module} import {graph_var}
from hypergraph.viz.renderer import render_graph

if hasattr({graph_var}, 'graph'):
    graph = {graph_var}.graph
elif hasattr({graph_var}, 'to_flat_graph'):
    graph = {graph_var}
else:
    graph = {graph_var}

if hasattr(graph, 'bind'):
    bound = graph.bind()
else:
    bound = graph

flat_graph = bound.to_flat_graph()
result = render_graph(
    flat_graph,
    depth={depth},
    separate_outputs={separate_outputs},
)

print(json.dumps(result.get("meta", {{}}), indent=2))
'''

    result = subprocess.run(
        ["uv", "run", "python", "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path.cwd()),
    )

    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(1)

    return json.loads(result.stdout)


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
    print(json.dumps({
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
    }, indent=2))


if __name__ == "__main__":
    main()
