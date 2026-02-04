"""Tests for @node(hide=True) visualization feature."""

import pytest
from hypergraph import Graph, node
from hypergraph.nodes.gate import route, ifelse, END


@node(output_name="a")
def step1(x):
    return x + 1


@node(output_name="b", hide=True)
def hidden_step(a):
    return a * 2


@node(output_name="c")
def step3(b):
    return b + 10


class TestHideParameter:
    """Test that hide parameter is correctly set on nodes."""

    def test_function_node_hide_default_false(self):
        """FunctionNode hide defaults to False."""
        assert step1.hide is False

    def test_function_node_hide_true(self):
        """FunctionNode hide=True is stored."""
        assert hidden_step.hide is True

    def test_hide_in_nx_attrs(self):
        """Hide attribute is included in nx_attrs."""
        assert step1.nx_attrs["hide"] is False
        assert hidden_step.nx_attrs["hide"] is True

    def test_hide_in_flat_graph(self):
        """Hide attribute is preserved in flat graph."""
        g = Graph([step1, hidden_step, step3])
        flat = g.to_flat_graph()

        for node_id, attrs in flat.nodes(data=True):
            if node_id == "hidden_step":
                assert attrs.get("hide") is True
            else:
                assert attrs.get("hide") is False


class TestHideRouteNode:
    """Test that hide works on RouteNode."""

    def test_route_hide_default_false(self):
        """RouteNode hide defaults to False."""

        @route(targets=["a", "b"])
        def visible_route(x):
            return "a" if x > 0 else "b"

        assert visible_route.hide is False

    def test_route_hide_true(self):
        """RouteNode hide=True is stored."""

        @route(targets=["a", "b"], hide=True)
        def hidden_route(x):
            return "a" if x > 0 else "b"

        assert hidden_route.hide is True


class TestHideIfElseNode:
    """Test that hide works on IfElseNode."""

    def test_ifelse_hide_default_false(self):
        """IfElseNode hide defaults to False."""

        @ifelse(when_true="a", when_false="b")
        def visible_gate(x):
            return x > 0

        assert visible_gate.hide is False

    def test_ifelse_hide_true(self):
        """IfElseNode hide=True is stored."""

        @ifelse(when_true="a", when_false="b", hide=True)
        def hidden_gate(x):
            return x > 0

        assert hidden_gate.hide is True


class TestHideVisualization:
    """Test that hidden nodes are filtered from visualization."""

    def test_hidden_node_not_in_viz_nodes(self):
        """Hidden nodes are excluded from rendered visualization nodes."""
        from hypergraph.viz.renderer import render_graph

        g = Graph([step1, hidden_step, step3])
        flat = g.to_flat_graph()
        result = render_graph(flat, depth=0)

        # Get node IDs from the rendered nodes
        node_ids = [n["id"] for n in result["nodes"]]

        # Hidden step should not be in the visualization
        assert "hidden_step" not in node_ids

        # Visible steps should still be present
        assert "step1" in node_ids
        assert "step3" in node_ids

    def test_edges_skip_hidden_nodes(self):
        """Edges referencing hidden nodes are filtered out."""
        from hypergraph.viz.renderer import render_graph

        g = Graph([step1, hidden_step, step3])
        flat = g.to_flat_graph()
        result = render_graph(flat, depth=0)

        # Get all edges
        edges = result["edges"]

        # No edge should reference hidden_step
        for edge in edges:
            assert edge["source"] != "hidden_step", f"Edge source is hidden node: {edge}"
            assert edge["target"] != "hidden_step", f"Edge target is hidden node: {edge}"
