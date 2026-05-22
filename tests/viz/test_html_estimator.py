"""Tests for Python-side visualization layout estimation."""

from hypergraph import Graph, node
from hypergraph.viz.html import estimate_layout


@node(output_name="total")
def combine_inputs(a: int, b: int, c: int, d: int, e: int, f: int) -> int:
    return a + b + c + d + e + f


def test_estimate_layout_respects_show_inputs_false():
    """Hidden external inputs should not inflate iframe height estimates."""
    graph = Graph([combine_inputs])

    _shown_width, shown_height = estimate_layout(graph, show_inputs=True)
    _hidden_width, hidden_height = estimate_layout(graph, show_inputs=False)

    assert hidden_height < shown_height


def test_estimate_layout_respects_show_bounded_inputs_false():
    """Bound external inputs should only count when explicitly shown."""
    graph = Graph([combine_inputs]).bind(a=1)

    _shown_width, shown_height = estimate_layout(graph, show_inputs=True, show_bounded_inputs=True)
    _hidden_width, hidden_height = estimate_layout(graph, show_inputs=True, show_bounded_inputs=False)

    assert hidden_height < shown_height
