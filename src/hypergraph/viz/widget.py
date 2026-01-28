"""Jupyter widget for graph visualization with VSCode scroll support."""

from __future__ import annotations

import html as html_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hypergraph.graph.core import Graph

from hypergraph.viz.renderer import render_graph
from hypergraph.viz.html_generator import generate_widget_html
from hypergraph.viz.layout_estimator import estimate_layout


class ScrollablePipelineWidget:
    """Widget for visualizing graphs in Jupyter/VSCode notebooks.

    Uses explicit iframe sizing to avoid double scrolling. The iframe
    dimensions are estimated from the graph structure to fit the content.
    """

    def __init__(self, html_content: str, width: int, height: int):
        """Create a scrollable widget.

        Args:
            html_content: Complete HTML document for the visualization
            width: Widget width in pixels
            height: Widget height in pixels
        """
        self.html_content = html_content
        self.width = width
        self.height = height
        self._id = id(self)

    def _repr_html_(self) -> str:
        """Return HTML representation for Jupyter display."""
        # Escape HTML for srcdoc attribute
        escaped_html = html_module.escape(self.html_content, quote=True)

        # CSS fix for VS Code white background on ipywidgets
        css_fix = """<style>
.cell-output-ipywidget-background {
   background-color: transparent !important;
}
.jp-OutputArea-output {
   background-color: transparent;
}
</style>"""

        # Simple iframe with explicit dimensions - no wrapper needed
        # Dimensions are set as both HTML attributes AND CSS for compatibility
        # The JS inside the iframe will resize via window.frameElement if needed
        return (
            f"{css_fix}"
            f'<iframe srcdoc="{escaped_html}" '
            f'width="{self.width}" height="{self.height}" frameborder="0" '
            f'style="border: none; width: {self.width}px; max-width: 100%; '
            f'height: {self.height}px; display: block; background: transparent; '
            f'margin: 0 auto; border-radius: 8px;" '
            f'sandbox="allow-scripts allow-same-origin allow-popups allow-forms">'
            f'</iframe>'
        )


def visualize(
    graph: Graph,
    *,
    depth: int = 1,
    theme: str = "auto",
    show_types: bool = False,
    separate_outputs: bool = False,
    layout_profile: str | None = None,
    filepath: str | None = None,
    _debug_overlays: bool = False,
) -> ScrollablePipelineWidget | None:
    """Create a visualization widget for a graph.

    Args:
        graph: The hypergraph Graph to visualize
        depth: How many levels of nested graphs to expand (default: 1)
        theme: "dark", "light", or "auto" (default: "auto")
        show_types: Whether to show type annotations (default: False)
        separate_outputs: Whether to render outputs as separate nodes (default: False)
        layout_profile: Optional layout profile override (e.g. "classic")
        filepath: Path to save HTML file (default: None, display in notebook)
        _debug_overlays: Internal flag to enable debug overlays (use VizDebugger.visualize())

    Returns:
        ScrollablePipelineWidget if output is None, otherwise None (saves to file)

    Example:
        >>> from hypergraph import Graph, node
        >>> @node(output_name="doubled")
        ... def double(x: int) -> int:
        ...     return x * 2
        >>> graph = Graph(nodes=[double])
        >>> widget = visualize(graph)  # Display in notebook
        >>> visualize(graph, filepath="graph.html")  # Save to HTML file
    """
    # Estimate dimensions if not provided
    est_width, est_height = estimate_layout(
        graph,
        separate_outputs=separate_outputs,
        show_types=show_types,
        depth=depth,
    )

    # Use estimated dimensions, applying minimums
    final_width = max(400, est_width)
    final_height = max(200, est_height)

    # Create flattened graph and render to React Flow format
    flat_graph = graph.to_flat_graph()
    graph_data = render_graph(
        flat_graph,
        depth=depth,
        theme=theme,
        show_types=show_types,
        separate_outputs=separate_outputs,
        layout_profile=layout_profile,
        debug_overlays=_debug_overlays,
    )

    # Generate HTML
    html_content = generate_widget_html(graph_data)

    # If output path specified, save to HTML file
    if filepath is not None:
        # Ensure .html extension
        if not filepath.endswith(".html"):
            filepath = filepath + ".html"
        with open(filepath, "w") as f:
            f.write(html_content)
        return None

    return ScrollablePipelineWidget(html_content, final_width, final_height)
