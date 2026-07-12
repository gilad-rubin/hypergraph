"""Benchmark the current visualization pipeline on a deeply nested graph.

Usage:
    uv run python scripts/benchmark_deep_nested.py          # k=8, width=2
    uv run python scripts/benchmark_deep_nested.py 12       # k=12, width=2
    uv run python scripts/benchmark_deep_nested.py 6 3      # k=6, width=3
"""

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from hypergraph import Graph
from hypergraph import node as hnode
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from hypergraph.viz.widget import visualize

# ---------------------------------------------------------------------------
# Dynamic node factory
# ---------------------------------------------------------------------------


def _make_node(func_name: str, input_param: str, output_name: str):
    """Create a passthrough node with a runtime-determined parameter name."""
    ns = {"hnode": hnode}
    exec(
        f"@hnode(output_name={output_name!r})\ndef {func_name}({input_param}: str) -> str:\n    return {input_param}\n",
        ns,
    )
    return ns[func_name]


# ---------------------------------------------------------------------------
# Parametric graph factory
# ---------------------------------------------------------------------------


def make_parametric_graph(k: int, width: int = 2) -> Graph:
    """Build a k-level nested graph with `width` pass-through nodes per level.

    Data flow:
      data_in → level_0[step_0..step_{width-1}] → val_0_out
      val_0_out → level_1[level_0, step_0..step_{width-1}] → val_1_out
      ...
      val_{k-2}_out → level_{k-1}[level_{k-2}, ...] → val_{k-1}_out
      top = Graph([level_{k-1}.as_node()])
    """
    # --- innermost level (level 0): takes external input 'data_in' ---
    prev_out = "data_in"
    nodes = []
    for step in range(width):
        out = f"val_0_{step}" if step < width - 1 else "val_0_out"
        nodes.append(_make_node(f"lvl0_step{step}", prev_out, out))
        prev_out = out
    current = Graph(nodes=nodes, name="level_0")

    # --- levels 1..k-1: each wraps the previous graph + adds `width` nodes ---
    for lvl in range(1, k):
        prev_out = f"val_{lvl - 1}_out"
        lvl_nodes = [current.as_node()]
        for step in range(width):
            out = f"val_{lvl}_{step}" if step < width - 1 else f"val_{lvl}_out"
            lvl_nodes.append(_make_node(f"lvl{lvl}_step{step}", prev_out, out))
            prev_out = out
        current = Graph(nodes=lvl_nodes, name=f"level_{lvl}")

    return Graph(nodes=[current.as_node()], name=f"pipeline_k{k}_w{width}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024**2:.2f} MB"


def bench(fn):
    t0 = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - t0) * 1000


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 2

    print(f"\nBuilding k={k} levels, width={width} nodes/level …")
    graph = make_parametric_graph(k, width)
    flat = graph.to_flat_graph()
    expandable = sum(1 for _, d in flat.nodes(data=True) if d.get("node_type") == "GRAPH")

    print("\nGraph stats:")
    print(f"  Nesting levels      : {k}")
    print(f"  Nodes per level     : {width} + 1 sub-graph container")
    print(f"  Flat graph nodes    : {flat.number_of_nodes()}")
    print(f"  Flat graph edges    : {flat.number_of_edges()}")
    print(f"  Expandable nodes    : {expandable}")

    # --- Compact GraphIR ---
    ir, ir_ms = bench(lambda: build_graph_ir(flat))
    ir_json = json.dumps(asdict(ir))
    ir_bytes = len(ir_json.encode())

    print("\nGraphIR:")
    print(f"  build_graph_ir()    : {ir_ms:.2f} ms")
    print(f"  Payload             : {fmt_bytes(ir_bytes)}")
    print(f"  IR nodes            : {len(ir.nodes)}")
    print(f"  IR edges            : {len(ir.edges)}")

    # --- Current HTML widget ---
    output_dir = Path("outputs/benchmark")
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / f"deep_nested_k{k}.html"
    _, html_ms = bench(lambda: visualize(graph, filepath=str(html_path)))
    html_bytes = html_path.stat().st_size

    print("\nCurrent HTML widget:")
    print(f"  Generation          : {html_ms:.0f} ms")
    print(f"  File size           : {fmt_bytes(html_bytes)}")
    print(f"\nSaved: {html_path}")


if __name__ == "__main__":
    main()
