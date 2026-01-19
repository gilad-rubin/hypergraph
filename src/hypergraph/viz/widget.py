"""Jupyter widget for graph visualization with VSCode scroll support."""

from __future__ import annotations

import html as html_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hypergraph.graph.core import Graph

from hypergraph.viz.renderer import render_graph
from hypergraph.viz.html_generator import generate_widget_html


class ScrollablePipelineWidget:
    """Widget with scroll passthrough for VSCode notebooks.

    Uses an overlay pattern to handle scroll events:
    - By default, overlay blocks iframe -> scroll passes through to notebook
    - Click on overlay -> overlay becomes transparent -> iframe is interactive
    - Mouse leaves widget -> overlay blocks again -> scroll restored
    - ESC key -> overlay blocks again -> scroll restored
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
        # Use html.escape but preserve quotes since we'll wrap in double quotes
        escaped_html = self.html_content.replace('"', '&quot;')

        return f'''<div id="viz-wrapper-{self._id}" style="
    position: relative;
    width: {self.width}px;
    height: {self.height}px;
    margin: 0 auto;
    display: block;
">
    <iframe
        id="viz-iframe-{self._id}"
        srcdoc="{escaped_html}"
        style="
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            border: none;
            border-radius: 8px;
        "
    ></iframe>

    <div id="overlay-{self._id}" style="
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        z-index: 10;
        cursor: default;
        background: rgba(0, 0, 0, 0.02);
        border-radius: 8px;
        display: flex;
        align-items: flex-end;
        justify-content: flex-end;
        padding: 8px;
    ">
        <span id="hint-{self._id}" style="
            font-size: 11px;
            color: #94a3b8;
            opacity: 0;
            transition: opacity 0.3s;
            font-family: system-ui, -apple-system, sans-serif;
        ">Click to interact</span>
    </div>
</div>

<style>
    #viz-wrapper-{self._id}:hover #hint-{self._id} {{
        opacity: 1;
    }}
    #overlay-{self._id}.interactive {{
        pointer-events: none;
        background: transparent;
    }}
    #overlay-{self._id}.interactive #hint-{self._id} {{
        display: none;
    }}
</style>

<script>
(function() {{
    var wrapper = document.getElementById("viz-wrapper-{self._id}");
    var overlay = document.getElementById("overlay-{self._id}");

    if (!wrapper || !overlay) return;

    // Click to enable interaction
    overlay.addEventListener("click", function() {{
        overlay.classList.add("interactive");
    }});

    // Mouse leave to restore scroll
    wrapper.addEventListener("mouseleave", function() {{
        overlay.classList.remove("interactive");
    }});

    // ESC to restore scroll
    document.addEventListener("keydown", function(e) {{
        if (e.key === "Escape") {{
            overlay.classList.remove("interactive");
        }}
    }});

    // Listen for resize messages from iframe to auto-fit content
    window.addEventListener("message", function(e) {{
        if (e.data && e.data.type === "hypergraph-viz-resize") {{
            var newHeight = e.data.height;
            var newWidth = e.data.width;
            if (newHeight && newHeight > 0) {{
                wrapper.style.height = newHeight + "px";
            }}
            if (newWidth && newWidth > 0) {{
                wrapper.style.width = Math.max(newWidth, {self.width}) + "px";
            }}
        }}
    }});
}})();
</script>'''


def visualize(
    graph: Graph,
    *,
    width: int = 800,
    height: int = 600,
    depth: int = 1,
    theme: str = "auto",
    show_types: bool = False,
    separate_outputs: bool = False,
) -> ScrollablePipelineWidget:
    """Create a visualization widget for a graph.

    Args:
        graph: The hypergraph Graph to visualize
        width: Widget width in pixels (default: 800)
        height: Widget height in pixels (default: 600)
        depth: How many levels of nested graphs to expand (default: 1)
        theme: "dark", "light", or "auto" (default: "auto")
        show_types: Whether to show type annotations (default: False)
        separate_outputs: Whether to render outputs as separate nodes (default: False)

    Returns:
        ScrollablePipelineWidget that can be displayed in Jupyter/VSCode notebooks

    Example:
        >>> from hypergraph import Graph, node
        >>> @node(output_name="doubled")
        ... def double(x: int) -> int:
        ...     return x * 2
        >>> graph = Graph(nodes=[double])
        >>> widget = visualize(graph)  # Display in notebook
    """
    # Render graph to React Flow format
    graph_data = render_graph(
        graph,
        depth=depth,
        theme=theme,
        show_types=show_types,
        separate_outputs=separate_outputs,
    )

    # Generate HTML
    html_content = generate_widget_html(graph_data)

    return ScrollablePipelineWidget(html_content, width, height)
