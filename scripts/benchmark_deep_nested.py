"""Benchmark: parametrically deep nested graph — IR size and generation time.

Usage:
    uv run python scripts/benchmark_deep_nested.py          # k=8, width=2
    uv run python scripts/benchmark_deep_nested.py 12       # k=12, width=2
    uv run python scripts/benchmark_deep_nested.py 6 3      # k=6, width=3

For k > LEGACY_SKIP_THRESHOLD the legacy render_graph pass is skipped
(it precomputes 2^k states which grows quickly).
"""

import json
import os
import sys
import time
from dataclasses import asdict

from hypergraph import Graph
from hypergraph import node as hnode
from hypergraph.viz.renderer import render_graph
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from hypergraph.viz.widget import visualize

LEGACY_SKIP_THRESHOLD = 10  # skip legacy HTML for k > this (too slow / too large)


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


def main():
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
    print(f"  Expandable nodes    : {expandable}  (legacy enumerates 2^{expandable} = {2**expandable} base states)")

    # --- IR path ---
    ir, ir_ms = bench(lambda: build_graph_ir(flat))
    ir_json = json.dumps(asdict(ir))
    ir_bytes = len(ir_json.encode())

    print("\nIR path:")
    print(f"  build_graph_ir()    : {ir_ms:.2f} ms")
    print(f"  Payload             : {fmt_bytes(ir_bytes)}")
    print(f"  IR nodes            : {len(ir.nodes)}")
    print(f"  IR edges            : {len(ir.edges)}")

    # --- Legacy path ---
    if expandable <= LEGACY_SKIP_THRESHOLD:
        legacy_data, legacy_ms = bench(lambda: render_graph(flat))
        legacy_json = json.dumps(
            {
                "nodes": legacy_data["nodes"],
                "edges": legacy_data["edges"],
                "meta": legacy_data.get("meta", {}),
            }
        )
        legacy_bytes = len(legacy_json.encode())
        state_count = len(legacy_data.get("meta", {}).get("nodesByState", {}))

        print("\nLegacy path:")
        print(f"  render_graph()      : {legacy_ms:.1f} ms")
        print(f"  Payload             : {fmt_bytes(legacy_bytes)}")
        print(f"  Precomputed states  : {state_count}")
        print(f"\nPayload reduction    : {(1 - ir_bytes / legacy_bytes) * 100:.1f}% smaller with IR")
    else:
        print(f"\nLegacy path: SKIPPED (k={k} > threshold {LEGACY_SKIP_THRESHOLD})")
        print(f"  Would enumerate {2**expandable:,} states × sep × ext modes")

    # --- HTML files ---
    os.makedirs("outputs/benchmark", exist_ok=True)
    ir_path = f"outputs/benchmark/deep_nested_k{k}_ir.html"
    _, ir_html_ms = bench(lambda: visualize(graph, use_ir=True, filepath=ir_path))
    ir_html_bytes = os.path.getsize(ir_path)

    print("\nHTML files:")
    print(f"  IR HTML             : {fmt_bytes(ir_html_bytes)}  ({ir_html_ms:.0f} ms)")

    if expandable <= LEGACY_SKIP_THRESHOLD:
        legacy_path = f"outputs/benchmark/deep_nested_k{k}_legacy.html"
        _, legacy_html_ms = bench(lambda: visualize(graph, use_ir=False, filepath=legacy_path))
        legacy_html_bytes = os.path.getsize(legacy_path)
        print(f"  Legacy HTML         : {fmt_bytes(legacy_html_bytes)}  ({legacy_html_ms:.0f} ms)")
        print(f"  HTML reduction      : {(1 - ir_html_bytes / legacy_html_bytes) * 100:.1f}% smaller with IR")
        print(f"\nSaved: {ir_path}")
        print(f"       {legacy_path}")
    else:
        print(f"\nSaved: {ir_path}")


if __name__ == "__main__":
    main()
