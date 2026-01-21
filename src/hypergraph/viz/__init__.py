"""Visualization module for hypergraph.

Usage:
    graph.visualize()  # Returns a Jupyter widget
    graph.visualize(depth=2, theme="dark", show_types=True)
"""

from hypergraph.viz.widget import visualize, ScrollablePipelineWidget
from hypergraph.viz.traversal import (
    get_children,
    traverse_to_leaves,
    build_expansion_predicate,
)
from hypergraph.viz.coordinates import Point, CoordinateSpace, layout_to_absolute

__all__ = [
    "visualize",
    "ScrollablePipelineWidget",
    "get_children",
    "traverse_to_leaves",
    "build_expansion_predicate",
    "Point",
    "CoordinateSpace",
    "layout_to_absolute",
]
