"""Tests for the visualization renderer."""

import pytest
from hypergraph import Graph, node
from hypergraph.viz.renderer import render_graph, _get_node_type


@node(output_name="doubled")
def double(x: int) -> int:
    """Double a number."""
    return x * 2


@node(output_name="result")
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@node(output_name="tripled")
def triple(x: int) -> int:
    """Triple a number."""
    return x * 3


class TestRenderGraph:
    """Tests for render_graph function."""

    def test_render_single_node(self):
        """Test rendering a graph with a single node."""
        graph = Graph(nodes=[double])
        result = render_graph(graph)

        assert "nodes" in result
        assert "edges" in result
        assert "meta" in result

        assert len(result["nodes"]) == 1
        node = result["nodes"][0]
        assert node["id"] == "double"
        assert node["data"]["nodeType"] == "FUNCTION"
        assert node["data"]["label"] == "double"

    def test_render_node_outputs(self):
        """Test that node outputs are captured correctly."""
        graph = Graph(nodes=[double])
        result = render_graph(graph)

        node = result["nodes"][0]
        outputs = node["data"]["outputs"]

        assert len(outputs) == 1
        assert outputs[0]["name"] == "doubled"
        assert outputs[0]["type"] == "int"

    def test_render_node_inputs(self):
        """Test that node inputs are captured correctly."""
        graph = Graph(nodes=[add])
        result = render_graph(graph)

        node = result["nodes"][0]
        inputs = node["data"]["inputs"]

        assert len(inputs) == 2
        input_names = {inp["name"] for inp in inputs}
        assert input_names == {"a", "b"}

    def test_render_multiple_nodes(self):
        """Test rendering a graph with multiple nodes."""
        graph = Graph(nodes=[double, add])
        result = render_graph(graph)

        assert len(result["nodes"]) == 2
        node_ids = {n["id"] for n in result["nodes"]}
        assert node_ids == {"double", "add"}

    def test_render_edges(self):
        """Test that edges are created from output->input connections."""

        @node(output_name="doubled")
        def double_fn(x: int) -> int:
            return x * 2

        @node(output_name="result")
        def use_doubled(doubled: int) -> int:
            return doubled + 1

        graph = Graph(nodes=[double_fn, use_doubled])
        result = render_graph(graph)

        assert len(result["edges"]) == 1
        edge = result["edges"][0]
        assert edge["source"] == "double_fn"
        assert edge["target"] == "use_doubled"

    def test_render_with_bound_inputs(self):
        """Test that bound inputs are marked correctly."""
        graph = Graph(nodes=[add]).bind(a=5)
        result = render_graph(graph)

        node = result["nodes"][0]
        inputs = node["data"]["inputs"]

        a_input = next(inp for inp in inputs if inp["name"] == "a")
        b_input = next(inp for inp in inputs if inp["name"] == "b")

        assert a_input["is_bound"] is True
        assert b_input["is_bound"] is False

    def test_render_options_passthrough(self):
        """Test that options are included in the result."""
        graph = Graph(nodes=[double])
        result = render_graph(graph, theme="dark", show_types=True, depth=2)

        assert result["meta"]["theme_preference"] == "dark"
        assert result["meta"]["show_types"] is True
        assert result["meta"]["initial_depth"] == 2

    def test_render_nested_graph(self):
        """Test rendering a nested graph."""
        inner = Graph(nodes=[double], name="inner")
        outer = Graph(nodes=[inner.as_node(), add])

        result = render_graph(outer, depth=1)

        # Should have nodes from both outer and inner
        node_ids = {n["id"] for n in result["nodes"]}
        assert "inner" in node_ids  # The pipeline node
        assert "double" in node_ids  # Inner node (expanded)
        assert "add" in node_ids  # Outer node

        # Inner nodes should have parentNode set
        double_node = next(n for n in result["nodes"] if n["id"] == "double")
        assert double_node["parentNode"] == "inner"

        # Pipeline node should be expanded
        inner_node = next(n for n in result["nodes"] if n["id"] == "inner")
        assert inner_node["data"]["nodeType"] == "PIPELINE"
        assert inner_node["data"]["isExpanded"] is True

    def test_render_nested_graph_collapsed(self):
        """Test that depth=0 keeps nested graphs collapsed."""
        inner = Graph(nodes=[double], name="inner")
        outer = Graph(nodes=[inner.as_node(), add])

        result = render_graph(outer, depth=0)

        # Should only have outer nodes, inner nodes not expanded
        node_ids = {n["id"] for n in result["nodes"]}
        assert "inner" in node_ids
        assert "add" in node_ids
        # double should not be present at depth=0
        assert "double" not in node_ids

        inner_node = next(n for n in result["nodes"] if n["id"] == "inner")
        assert inner_node["data"]["isExpanded"] is False


class TestGetNodeType:
    """Tests for _get_node_type function."""

    def test_function_node_type(self):
        """Test that FunctionNode maps to FUNCTION."""
        from hypergraph.nodes.function import FunctionNode

        fn = FunctionNode(lambda x: x, output_name="y")
        assert _get_node_type(fn) == "FUNCTION"

    def test_graph_node_type(self):
        """Test that GraphNode maps to PIPELINE."""
        inner = Graph(nodes=[double], name="inner")
        gn = inner.as_node()
        assert _get_node_type(gn) == "PIPELINE"
