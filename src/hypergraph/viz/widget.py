"""Cell-context renderer for graph visualization.

Stage 1 of PR #88 keeps a thin iframe-based display object as the in-notebook
output of ``visualize()``. Stage 4 will replace it with an ``anywidget`` shell
that survives save+reopen without a kernel; the public ``visualize()`` signature
should not change again at that point.
"""

from __future__ import annotations

import html as html_module
import os
import sys
import warnings
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import FrameType

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


# The hypergraph package directory: frames under it are library frames.
_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_input_visibility(
    show_inputs: bool | None,
    show_bounded_inputs: bool | None,
    show_external_inputs: bool | None,
) -> tuple[bool, bool]:
    """Return ``(show_inputs, show_bounded_inputs)`` for a visualize call.

    The ONE place the input-visibility defaults live (ruling D56); every
    public entry point (``Graph.visualize``, ``hypergraph.viz.visualize``,
    ``HyperTable.visualize``, ``extract_debug_data``) passes its raw arguments
    here:

    - ``show_inputs`` defaults to False: the graph shows steps only, and a
      step's inputs appear as ghost pills when it is hovered or tapped;
    - ``show_bounded_inputs`` defaults to True: bound tools appear, faded and
      dashed, as input boxes or ghosts; False leaves them out of both;
    - ``show_external_inputs`` is the deprecated alias for ``show_inputs``.
      Its warning points at the first caller outside hypergraph.

    Already-resolved booleans pass through unchanged, so an entry point that
    hands its result to another one decides nothing twice.
    """
    if show_external_inputs is not None:
        if show_inputs is not None and show_inputs != show_external_inputs:
            raise TypeError("Pass either show_inputs or show_external_inputs, not both.")
        warnings.warn(
            "show_external_inputs is deprecated; use show_inputs instead.",
            DeprecationWarning,
            stacklevel=_caller_stacklevel(),
        )
        show_inputs = show_external_inputs
    return (
        False if show_inputs is None else bool(show_inputs),
        True if show_bounded_inputs is None else bool(show_bounded_inputs),
    )


def _caller_stacklevel() -> int:
    """``stacklevel`` for a warning raised here that names the user's line.

    Counts the frames between ``warnings.warn`` in the resolver and the first
    frame outside the hypergraph package, however many entry points the call
    passed through (``HyperTable.visualize`` goes through two).
    """
    level = 2  # 1 is the resolver itself
    frame: FrameType | None = sys._getframe(2)
    while frame is not None and os.path.abspath(frame.f_code.co_filename).startswith(_PACKAGE_DIR + os.sep):
        frame = frame.f_back
        level += 1
    return level


def visualize(
    graph: Graph,
    *,
    depth: int = 0,
    theme: str = "auto",
    show_types: bool = True,
    separate_outputs: bool = False,
    show_inputs: bool | None = None,
    show_bounded_inputs: bool | None = None,
    simplify: bool = True,
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
        show_inputs: Whether to draw input boxes (default: False). Hidden
            inputs appear on demand: hovering or tapping a step shows its
            inputs as ghost pills. Toggleable in the widget toolbar.
        show_bounded_inputs: Whether bound inputs (tools bound with
            ``Graph.bind``) appear, faded and dashed, as input boxes and in
            the ghosts (default: True).
        simplify: Hide data and input edges a longer path already implies —
            with ``A → B → C``, a direct ``A → C`` is dropped, and an input
            feeding the whole chain keeps only its earliest consumer
            (default: True).
            Toggleable in the widget toolbar.
        show_external_inputs: Deprecated alias for ``show_inputs``.
        filepath: Path to save standalone HTML (default: display in notebook).
        _debug_overlays: Internal metadata-only diagnostic flag; it does not
            enable visible overlays.

    Returns:
        A cell-output object when displaying in a notebook; ``None`` when
        ``filepath`` is given (the file is written to disk).
    """
    show_inputs, show_bounded_inputs = _resolve_input_visibility(show_inputs, show_bounded_inputs, show_external_inputs)

    flat_graph = graph.to_flat_graph()
    return render_flat_graph(
        flat_graph,
        graph,
        depth=depth,
        theme=theme,
        show_types=show_types,
        separate_outputs=separate_outputs,
        show_inputs=show_inputs,
        show_bounded_inputs=show_bounded_inputs,
        simplify=simplify,
        filepath=filepath,
        _debug_overlays=_debug_overlays,
    )


def render_flat_graph(
    flat_graph,
    graph: Graph,
    *,
    depth: int = 0,
    theme: str = "auto",
    show_types: bool = True,
    separate_outputs: bool = False,
    show_inputs: bool | None = None,
    show_bounded_inputs: bool | None = None,
    simplify: bool = True,
    filepath: str | None = None,
    _debug_overlays: bool = False,
) -> _VizCellOutput | None:
    """Render a pre-built flat graph to the notebook widget / HTML file.

    Split out of :func:`visualize` so callers that must augment the flat graph
    before rendering (e.g. ``HyperTable.visualize`` injecting the parent→child
    fan-out edge) reuse the exact same IR + HTML pipeline instead of
    special-casing the renderer. ``graph`` is still needed for layout estimation.
    """
    show_inputs, show_bounded_inputs = _resolve_input_visibility(show_inputs, show_bounded_inputs, None)
    est_width, est_height = estimate_layout(
        graph,
        separate_outputs=separate_outputs,
        show_types=show_types,
        show_inputs=show_inputs,
        show_bounded_inputs=show_bounded_inputs,
        depth=depth,
    )
    final_width = max(400, est_width)
    final_height = max(200, est_height)

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
            "simplify": simplify,
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
