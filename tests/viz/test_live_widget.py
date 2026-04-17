"""Tests for the live, kernel-backed HypergraphWidget.

Covers the Python half of the expand/collapse/toggle round-trip:
- Initial payload is a single-state render (no nodesByState/edgesByState).
- A `display_state_request` trait change triggers a recompute and
  writes the new graph_data + display_state_response.
- The kernel-free `filepath=` path still uses the precomputed renderer
  so saved HTML stays interactive without a kernel.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypergraph import Graph, node
from hypergraph.viz.live_widget import HypergraphWidget
from hypergraph.viz.renderer import render_graph_single_state
from hypergraph.viz.widget import ScrollablePipelineWidget, visualize


@node(output_name="y")
def _a(x: int) -> int:
    return x + 1


@node(output_name="z")
def _b(y: int) -> int:
    return y * 2


def _nested_graph() -> Graph:
    inner = Graph(nodes=[_a, _b], name="inner")
    return Graph(nodes=[inner.as_node()])


def test_render_graph_single_state_has_no_by_state_maps():
    g = _nested_graph()
    data = render_graph_single_state(g.to_flat_graph(), depth=0)

    assert "nodesByState" not in data["meta"]
    assert "edgesByState" not in data["meta"]
    assert data["meta"]["liveMode"] is True
    assert isinstance(data["meta"]["expansionState"], dict)
    assert data["nodes"]
    assert data["meta"]["expandableNodes"]


def test_widget_initial_payload_is_single_state():
    g = _nested_graph()
    w = HypergraphWidget(g.to_flat_graph(), depth=0)

    assert w.graph_data["meta"]["liveMode"] is True
    assert "nodesByState" not in w.graph_data["meta"]
    assert "edgesByState" not in w.graph_data["meta"]


def test_widget_recomputes_on_display_state_request():
    g = _nested_graph()
    w = HypergraphWidget(g.to_flat_graph(), depth=0)

    assert w.graph_data["meta"]["separate_outputs"] is False

    expandable = w.graph_data["meta"]["expandableNodes"]
    assert expandable, "Expected at least one expandable container in fixture"
    target = expandable[0]

    w.display_state_request = {
        "requestId": 42,
        "displayState": {
            "expansion": {target: True},
            "separate_outputs": True,
            "show_inputs": True,
        },
    }

    assert w.graph_data["meta"]["separate_outputs"] is True
    assert w.graph_data["meta"]["expansionState"][target] is True
    assert w.display_state_response["requestId"] == 42
    assert w.display_state_response["graphData"] is w.graph_data


def test_widget_payload_shrinks_vs_static():
    """Live widget payload should be dramatically smaller than the
    precomputed-state payload on a graph with several expandable
    containers."""
    import json

    from hypergraph.viz.renderer import render_graph

    @node(output_name="s0_out")
    def s0(x0: int) -> int:
        return x0

    @node(output_name="s1_out")
    def s1(x1: int) -> int:
        return x1

    @node(output_name="s2_out")
    def s2(x2: int) -> int:
        return x2

    @node(output_name="s3_out")
    def s3(x3: int) -> int:
        return x3

    @node(output_name="s4_out")
    def s4(x4: int) -> int:
        return x4

    subs = [
        Graph(nodes=[s0], name="sub_0"),
        Graph(nodes=[s1], name="sub_1"),
        Graph(nodes=[s2], name="sub_2"),
        Graph(nodes=[s3], name="sub_3"),
        Graph(nodes=[s4], name="sub_4"),
    ]
    flat = Graph(nodes=[s.as_node() for s in subs], name="root").to_flat_graph()

    live = render_graph_single_state(flat, depth=0)
    static = render_graph(flat, depth=0)

    live_bytes = len(json.dumps(live))
    static_bytes = len(json.dumps(static))
    assert live_bytes < static_bytes / 5, (live_bytes, static_bytes)


def test_visualize_defaults_to_live_widget():
    g = _nested_graph()
    w = visualize(g)
    assert isinstance(w, HypergraphWidget)


def test_visualize_live_false_returns_static_widget():
    g = _nested_graph()
    w = visualize(g, live=False)
    assert isinstance(w, ScrollablePipelineWidget)


def test_visualize_filepath_still_emits_standalone_html_with_by_state():
    """The standalone HTML path must keep precomputed state maps so the
    saved file stays interactive without a Python kernel."""
    g = _nested_graph()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "g.html"
        result = visualize(g, filepath=str(path))
        assert result is None
        assert path.exists()
        content = path.read_text()
        assert '"nodesByState"' in content
        assert '"edgesByState"' in content
