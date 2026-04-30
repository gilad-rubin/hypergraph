"""Render legacy and IR-based HTML side-by-side for visual parity check.

Generates two HTML files per fixture: one through the legacy
render_graph -> generate_widget_html path, one through
build_graph_ir -> build_initial_scene -> generate_widget_html.
Open both and compare.
"""

from __future__ import annotations

from pathlib import Path

from hypergraph.viz.html import generate_widget_html
from hypergraph.viz.renderer import render_graph
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from hypergraph.viz.scene_builder import build_initial_scene
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
        scene = build_initial_scene(ir)
        ir_data = {
            "nodes": scene["nodes"],
            "edges": scene["edges"],
            "meta": {},
        }
        ir_html = generate_widget_html(ir_data)
        (OUT_DIR / f"{name}_ir.html").write_text(ir_html)

        print(
            f"{name:10} legacy={legacy_data and len(legacy_data['nodes'])}n/{len(legacy_data['edges'])}e  ir={len(scene['nodes'])}n/{len(scene['edges'])}e"
        )

    print(f"\nWrote to {OUT_DIR}/")
    print(f"  open {OUT_DIR}/outer_legacy.html")
    print(f"  open {OUT_DIR}/outer_ir.html")


if __name__ == "__main__":
    main()
