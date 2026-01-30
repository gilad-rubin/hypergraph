"""Visualization module for hypergraph.

Usage:
    graph.visualize()  # Returns a Jupyter widget
    graph.visualize(depth=2, theme="dark", show_types=True)

Debug:
    from hypergraph.viz import VizDebugger, validate_graph, find_issues

    debugger = graph.debug_viz()  # or VizDebugger(graph)
    debugger.trace_node("my_node")  # "points from" / "points to"
    debugger.find_issues()  # comprehensive diagnostics
"""

from hypergraph.viz.widget import visualize, ScrollablePipelineWidget
from hypergraph.viz.debug import (
    VizDebugger,
    validate_graph,
    find_issues,
    extract_debug_data,
    RenderedDebugData,
    RenderedEdge,
)

__all__ = [
    "visualize",
    "ScrollablePipelineWidget",
    "VizDebugger",
    "validate_graph",
    "find_issues",
    "extract_debug_data",
    "RenderedDebugData",
    "RenderedEdge",
]
