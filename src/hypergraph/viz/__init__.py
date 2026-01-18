"""Visualization module for hypergraph.

Usage:
    graph.visualize()  # Returns a Jupyter widget
    graph.visualize(depth=2, theme="dark", show_types=True)
"""

from hypergraph.viz.widget import visualize, ScrollablePipelineWidget

__all__ = ["visualize", "ScrollablePipelineWidget"]
