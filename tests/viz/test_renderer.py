"""Tests for the visualization renderer."""

import pytest
from hypergraph import Graph, node
from hypergraph.viz.renderer import render_graph


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
        result = render_graph(graph.to_flat_graph())

        assert "nodes" in result
        assert "edges" in result
        assert "meta" in result

        # Now always creates: INPUT, FUNCTION, DATA nodes (individual inputs, not grouped)
        node_types = {n["data"]["nodeType"] for n in result["nodes"]}
        assert "FUNCTION" in node_types
        assert "INPUT" in node_types  # For external input 'x' (individual, not grouped)
        assert "DATA" in node_types  # For output 'doubled'

        fn_node = next(n for n in result["nodes"] if n["data"]["nodeType"] == "FUNCTION")
        assert fn_node["id"] == "double"
        assert fn_node["data"]["label"] == "double"

    def test_render_node_outputs(self):
        """Test that node outputs are captured as DATA nodes."""
        graph = Graph(nodes=[double])
        result = render_graph(graph.to_flat_graph())

        # Outputs are now separate DATA nodes (always created)
        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        assert len(data_nodes) == 1
        assert data_nodes[0]["data"]["label"] == "doubled"
        assert data_nodes[0]["data"]["typeHint"] == "int"
        assert data_nodes[0]["data"]["sourceId"] == "double"

    def test_render_node_inputs(self):
        """Test that node inputs are captured correctly."""
        graph = Graph(nodes=[add])
        result = render_graph(graph.to_flat_graph())

        fn_node = next(n for n in result["nodes"] if n["data"]["nodeType"] == "FUNCTION")
        inputs = fn_node["data"]["inputs"]

        assert len(inputs) == 2
        input_names = {inp["name"] for inp in inputs}
        assert input_names == {"a", "b"}

    def test_render_multiple_nodes(self):
        """Test rendering a graph with multiple nodes."""
        graph = Graph(nodes=[double, add])
        result = render_graph(graph.to_flat_graph())

        # Check FUNCTION nodes specifically
        fn_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "FUNCTION"]
        assert len(fn_nodes) == 2
        node_ids = {n["id"] for n in fn_nodes}
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
        result = render_graph(graph.to_flat_graph())

        # Edges now flow: INPUT → func, func → DATA, DATA → func
        # Check that the data flow edges exist
        data_edges = [e for e in result["edges"] if e.get("data", {}).get("edgeType") == "data"]
        assert len(data_edges) == 1
        # Data edge goes from DATA node to consumer
        assert data_edges[0]["source"] == "data_double_fn_doubled"
        assert data_edges[0]["target"] == "use_doubled"

        # Check output edges (function → DATA node)
        output_edges = [e for e in result["edges"] if e.get("data", {}).get("edgeType") == "output"]
        assert len(output_edges) == 2  # One for each function's output

    def test_render_with_bound_inputs(self):
        """Test that bound inputs are marked correctly."""
        graph = Graph(nodes=[add]).bind(a=5)
        result = render_graph(graph.to_flat_graph())

        fn_node = next(n for n in result["nodes"] if n["data"]["nodeType"] == "FUNCTION")
        inputs = fn_node["data"]["inputs"]

        a_input = next(inp for inp in inputs if inp["name"] == "a")
        b_input = next(inp for inp in inputs if inp["name"] == "b")

        assert a_input["is_bound"] is True
        assert b_input["is_bound"] is False

    def test_render_options_passthrough(self):
        """Test that options are included in the result."""
        graph = Graph(nodes=[double])
        result = render_graph(graph.to_flat_graph(), theme="dark", show_types=True, depth=2)

        assert result["meta"]["theme_preference"] == "dark"
        assert result["meta"]["show_types"] is True
        assert result["meta"]["initial_depth"] == 2

    def test_render_nested_graph(self):
        """Test rendering a nested graph."""
        inner = Graph(nodes=[double], name="inner")
        outer = Graph(nodes=[inner.as_node(), add])

        result = render_graph(outer.to_flat_graph(), depth=1)

        # Should have FUNCTION/PIPELINE nodes from both outer and inner
        fn_and_pipeline_nodes = [
            n for n in result["nodes"]
            if n["data"]["nodeType"] in ("FUNCTION", "PIPELINE")
        ]
        node_ids = {n["id"] for n in fn_and_pipeline_nodes}
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

        result = render_graph(outer.to_flat_graph(), depth=0)

        # All nodes should be present (children included for click-to-expand)
        # Visibility is controlled by JS based on expansion state
        fn_and_pipeline_nodes = [
            n for n in result["nodes"]
            if n["data"]["nodeType"] in ("FUNCTION", "PIPELINE")
        ]
        node_ids = {n["id"] for n in fn_and_pipeline_nodes}
        assert "inner" in node_ids
        assert "add" in node_ids
        # double is now always included (visibility controlled by JS)
        assert "double" in node_ids

        # Inner graph should be marked as collapsed
        inner_node = next(n for n in result["nodes"] if n["id"] == "inner")
        assert inner_node["data"]["isExpanded"] is False

        # Child node should have parentNode reference
        double_node = next(n for n in result["nodes"] if n["id"] == "double")
        assert double_node.get("parentNode") == "inner"


class TestNodeType:
    """Tests for node_type property on HyperNode subclasses."""

    def test_function_node_type(self):
        """Test that FunctionNode has node_type='FUNCTION'."""
        from hypergraph.nodes.function import FunctionNode

        fn = FunctionNode(lambda x: x, output_name="y")
        assert fn.node_type == "FUNCTION"

    def test_graph_node_type(self):
        """Test that GraphNode has node_type='GRAPH'."""
        inner = Graph(nodes=[double], name="inner")
        gn = inner.as_node()
        assert gn.node_type == "GRAPH"
