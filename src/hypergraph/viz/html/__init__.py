"""HTML output pipeline for hypergraph visualization.

Assembles the final HTML document from renderer output:
- generator: builds the standalone HTML with embedded assets
- estimator: estimates iframe dimensions from graph structure
"""

from hypergraph.viz.html.estimator import LayoutEstimator, estimate_layout
from hypergraph.viz.html.generator import generate_widget_html

__all__ = [
    "LayoutEstimator",
    "estimate_layout",
    "generate_widget_html",
]
