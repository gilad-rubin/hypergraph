"""Cell-context renderer for graph visualization.

Stage 1 of PR #88 keeps a thin iframe-based display object as the in-notebook
output of ``visualize()``. Stage 4 will replace it with an ``anywidget`` shell
that survives save+reopen without a kernel; the public ``visualize()`` signature
should not change again at that point.
"""

from __future__ import annotations

import html as html_module
import warnings
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hypergraph.graph.core import Graph

from hypergraph.viz._common import build_expansion_state
from hypergraph.viz.html import estimate_layout, generate_widget_html
from hypergraph.viz.renderer.ir_builder import build_graph_ir


class _VizCellOutput:
    """Iframe-wrapped HTML for in-notebook display.

    Private — users get this from :func:`visualize` and only ever interact
    with it through Jupyter's ``_repr_html_`` protocol.
    """

    def __init__(self, html_content: str, width: int, height: int):
        self.html_content = html_content
        self.width = width
        self.height = height

    def _repr_html_(self) -> str:
        escaped_html = html_module.escape(self.html_content, quote=True)
        css_fix = """<style>
.cell-output-ipywidget-background {
   background-color: transparent !important;
}
.jp-OutputArea-output {
   background-color: transparent;
}
</style>"""
        return (
            f"{css_fix}"
            f'<iframe srcdoc="{escaped_html}" '
            f'width="{self.width}" height="{self.height}" frameborder="0" '
            f'style="border: none; width: {self.width}px; max-width: 100%; '
            f"height: {self.height}px; display: block; background: transparent; "
            f'margin: 0 auto; border-radius: 8px;" '
            f'sandbox="allow-scripts allow-same-origin allow-popups allow-forms">'
            f"</iframe>"
        )


def visualize(
    graph: Graph,
    *,
    depth: int = 0,
    theme: str = "auto",
    show_types: bool = True,
    separate_outputs: bool = False,
    show_inputs: bool | None = None,
    show_bounded_inputs: bool = False,
    show_external_inputs: bool | None = None,
    filepath: str | None = None,
    _debug_overlays: bool = False,
) -> _VizCellOutput | None:
    """Create a visualization for a graph.

    Args:
        graph: The hypergraph Graph to visualize.
        depth: How many levels of nested graphs to expand (default: 0).
        theme: "dark", "light", or "auto".
        show_types: Whether to show type annotations.
        separate_outputs: Whether to render outputs as separate DATA nodes.
        show_inputs: Whether to show INPUT/INPUT_GROUP nodes.
        show_bounded_inputs: Whether to include bound INPUT/INPUT_GROUP nodes.
        show_external_inputs: Deprecated alias for ``show_inputs``.
        filepath: Path to save standalone HTML (default: display in notebook).
        _debug_overlays: Internal flag to enable debug overlays.

    Returns:
        A cell-output object when displaying in a notebook; ``None`` when
        ``filepath`` is given (the file is written to disk).
    """
    if show_external_inputs is not None:
        if show_inputs is not None and show_inputs != show_external_inputs:
            raise TypeError("Pass either show_inputs or show_external_inputs, not both.")
        warnings.warn(
            "show_external_inputs is deprecated; use show_inputs instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        show_inputs = show_external_inputs
    elif show_inputs is None:
        show_inputs = True

    est_width, est_height = estimate_layout(
        graph,
        separate_outputs=separate_outputs,
        show_types=show_types,
        depth=depth,
    )
    final_width = max(400, est_width)
    final_height = max(200, est_height)

    flat_graph = graph.to_flat_graph()
    ir = build_graph_ir(flat_graph)
    initial_expansion = build_expansion_state(flat_graph, depth)
    graph_data = {
        "nodes": [],
        "edges": [],
        "meta": {
            "ir": asdict(ir),
            "initial_expansion": initial_expansion,
            "theme_preference": theme,
            "show_types": show_types,
            "separate_outputs": separate_outputs,
            "show_inputs": show_inputs,
            "show_bounded_inputs": show_bounded_inputs,
            "debug_overlays": _debug_overlays,
        },
    }

    html_content = generate_widget_html(graph_data)

    if filepath is not None:
        if not filepath.endswith(".html"):
            filepath = filepath + ".html"
        with open(filepath, "w") as f:
            f.write(html_content)
        return None

    return _VizCellOutput(html_content, final_width, final_height)
