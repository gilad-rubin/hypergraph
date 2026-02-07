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

from hypergraph.viz.mermaid import MermaidDiagram, to_mermaid
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
    "MermaidDiagram",
    "RenderedDebugData",
    "RenderedEdge",
    "ScrollablePipelineWidget",
    "VizDebugger",
    "extract_debug_data",
    "find_issues",
    "to_mermaid",
    "validate_graph",
    "visualize",
]
