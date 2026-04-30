"""Render legacy and IR-based HTML side-by-side for visual parity check.

Generates two HTML files per fixture:
  - <name>_legacy.html: legacy 2^N precompute path (render_graph)
  - <name>_ir.html:     compact IR shipped raw; viz.js derives the scene
                        client-side via assets/scene_builder.js

Open both and compare.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from hypergraph.viz.html import generate_widget_html
from hypergraph.viz.renderer import render_graph
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from tests.viz.conftest import make_outer, make_simple_graph, make_workflow

FIXTURES = {
    "simple": make_simple_graph,
    "workflow": make_workflow,
    "outer": make_outer,
}

OUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "ir_parity"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, factory in FIXTURES.items():
        flat = factory().to_flat_graph()

        legacy_data = render_graph(flat)
        legacy_html = generate_widget_html(legacy_data)
        (OUT_DIR / f"{name}_legacy.html").write_text(legacy_html)

        ir = build_graph_ir(flat)
        # Ship raw IR — the JS scene_builder derives scene client-side.
        # nodes/edges are placeholders that viz.js replaces on init.
        ir_data = {
            "nodes": [],
            "edges": [],
            "meta": {"ir": asdict(ir)},
        }
        ir_html = generate_widget_html(ir_data)
        (OUT_DIR / f"{name}_ir.html").write_text(ir_html)

        legacy_size = len(legacy_html)
        ir_size = len(ir_html)
        print(f"{name:10} legacy={legacy_size:>9,} B   ir={ir_size:>9,} B   delta={(ir_size - legacy_size) / legacy_size:+.1%}")

    print(f"\nWrote to {OUT_DIR}/")
    print(f"  open {OUT_DIR}/outer_legacy.html")
    print(f"  open {OUT_DIR}/outer_ir.html")


if __name__ == "__main__":
    main()
