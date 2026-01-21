"""Characterization tests for renderer output structure.

These tests document the CURRENT behavior of the renderer. They assert on
structural properties (node types, edge connections, hierarchy) not positions.

Purpose: Create a safety net for future refactoring by capturing the expected
output structure for various graph types.
"""

import pytest

from hypergraph.viz.renderer import render_graph


class TestSimpleGraphCharacterization:
    """Characterize output structure for single-node graph."""

    def test_single_node_creates_three_node_types(self, simple_graph):
        """Single node graph creates INPUT_GROUP, FUNCTION, and DATA nodes."""
        result = render_graph(simple_graph.to_viz_graph())

        node_types = {n["data"]["nodeType"] for n in result["nodes"]}
        assert node_types == {"INPUT_GROUP", "FUNCTION", "DATA"}

    def test_single_node_function_attributes(self, simple_graph):
        """FUNCTION node has correct id and label."""
        result = render_graph(simple_graph.to_viz_graph())

        fn_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "FUNCTION"]
        assert len(fn_nodes) == 1

        fn_node = fn_nodes[0]
        assert fn_node["id"] == "double"
        assert fn_node["data"]["label"] == "double"
        assert fn_node["type"] == "custom"

    def test_single_node_data_output(self, simple_graph):
        """DATA node is created for function output."""
        result = render_graph(simple_graph.to_viz_graph())

        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        assert len(data_nodes) == 1

        data_node = data_nodes[0]
        assert data_node["data"]["label"] == "doubled"
        assert data_node["data"]["typeHint"] == "int"
        assert data_node["data"]["sourceId"] == "double"

    def test_single_node_input_group(self, simple_graph):
        """INPUT_GROUP node is created for external input."""
        result = render_graph(simple_graph.to_viz_graph())

        input_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "INPUT_GROUP"]
        assert len(input_nodes) == 1

        input_node = input_nodes[0]
        assert input_node["data"]["params"] == ["x"]
        assert input_node["data"]["isBound"] is False

    def test_single_node_edges(self, simple_graph):
        """Edges connect INPUT_GROUP → FUNCTION → DATA."""
        result = render_graph(simple_graph.to_viz_graph())

        edges = result["edges"]

        # Find input edge
        input_edges = [e for e in edges if e["data"]["edgeType"] == "input"]
        assert len(input_edges) == 1
        assert input_edges[0]["target"] == "double"

        # Find output edge
        output_edges = [e for e in edges if e["data"]["edgeType"] == "output"]
        assert len(output_edges) == 1
        assert output_edges[0]["source"] == "double"
        assert output_edges[0]["target"] == "data_double_doubled"


class TestLinearGraphCharacterization:
    """Characterize output for linear data flow graph."""

    def test_linear_graph_has_three_functions(self, linear_graph):
        """Linear graph has three FUNCTION nodes."""
        result = render_graph(linear_graph.to_viz_graph())

        fn_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "FUNCTION"]
        assert len(fn_nodes) == 3

        fn_ids = {n["id"] for n in fn_nodes}
        assert fn_ids == {"double_fn", "triple_fn", "add_fn"}

    def test_linear_graph_data_flow(self, linear_graph):
        """DATA nodes connect functions in sequence."""
        result = render_graph(linear_graph.to_viz_graph())

        # Should have DATA nodes for intermediate outputs
        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        data_labels = {n["data"]["label"] for n in data_nodes}
        assert "doubled" in data_labels
        assert "tripled" in data_labels

        # Check data flow edges exist
        data_edges = [e for e in result["edges"] if e["data"]["edgeType"] == "data"]
        # doubled flows to triple_fn
        assert any(
            e["source"] == "data_double_fn_doubled" and e["target"] == "triple_fn"
            for e in data_edges
        )
        # tripled flows to add_fn
        assert any(
            e["source"] == "data_triple_fn_tripled" and e["target"] == "add_fn"
            for e in data_edges
        )

    def test_linear_graph_external_inputs(self, linear_graph):
        """External inputs (x, y) have INPUT_GROUP nodes."""
        result = render_graph(linear_graph.to_viz_graph())

        input_groups = [n for n in result["nodes"] if n["data"]["nodeType"] == "INPUT_GROUP"]
        all_params = []
        for group in input_groups:
            all_params.extend(group["data"]["params"])

        assert "x" in all_params
        assert "y" in all_params


class TestBranchingGraphCharacterization:
    """Characterize output for graphs with branch nodes."""

    def test_branching_graph_has_branch_node(self, branching_graph):
        """Graph with ifelse has BRANCH node type."""
        result = render_graph(branching_graph.to_viz_graph())

        branch_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "BRANCH"]
        assert len(branch_nodes) == 1

        branch_node = branch_nodes[0]
        assert branch_node["id"] == "is_even"

    def test_ifelse_branch_data(self, branching_graph):
        """IfElse branch node stores targets as a list."""
        result = render_graph(branching_graph.to_viz_graph())

        branch_node = next(
            n for n in result["nodes"] if n["data"]["nodeType"] == "BRANCH"
        )

        # Current behavior: all gate nodes use 'targets' list, not when_true/when_false
        assert "targets" in branch_node["data"]
        assert branch_node["data"]["targets"] == ["double", "triple"]

    def test_branch_control_edges(self, branching_graph):
        """Branch node has control edges to targets."""
        result = render_graph(branching_graph.to_viz_graph())

        control_edges = [e for e in result["edges"] if e["data"]["edgeType"] == "control"]

        # Should have control edges from is_even to both targets
        sources = {e["source"] for e in control_edges}
        assert "is_even" in sources

        targets = {e["target"] for e in control_edges if e["source"] == "is_even"}
        assert targets == {"double", "triple"}

    def test_branch_edge_labels(self, branching_graph):
        """Current behavior: control edges don't have True/False labels."""
        result = render_graph(branching_graph.to_viz_graph())

        control_edges = [
            e for e in result["edges"]
            if e["data"]["edgeType"] == "control" and e["source"] == "is_even"
        ]

        # Current behavior: labels not set for IfElse edges
        # (renderer checks for 'when_true' in branch_data but it stores 'targets')
        labels = {e["data"].get("label") for e in control_edges}
        assert labels == {None}


class TestNestedGraphCharacterization:
    """Characterize output for nested graphs."""

    def test_nested_graph_has_pipeline_node(self, nested_graph):
        """Nested graph creates PIPELINE node."""
        result = render_graph(nested_graph.to_viz_graph(), depth=1)

        pipeline_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "PIPELINE"]
        assert len(pipeline_nodes) == 1
        assert pipeline_nodes[0]["id"] == "inner"

    def test_nested_graph_expansion_state(self, nested_graph):
        """PIPELINE node is expanded when depth=1."""
        result = render_graph(nested_graph.to_viz_graph(), depth=1)

        pipeline_node = next(
            n for n in result["nodes"] if n["data"]["nodeType"] == "PIPELINE"
        )
        assert pipeline_node["data"]["isExpanded"] is True

    def test_nested_graph_collapsed_state(self, nested_graph):
        """PIPELINE node is collapsed when depth=0."""
        result = render_graph(nested_graph.to_viz_graph(), depth=0)

        pipeline_node = next(
            n for n in result["nodes"] if n["data"]["nodeType"] == "PIPELINE"
        )
        assert pipeline_node["data"]["isExpanded"] is False

    def test_nested_graph_children_have_parent(self, nested_graph):
        """Child nodes have parentNode reference."""
        result = render_graph(nested_graph.to_viz_graph(), depth=1)

        # Find the double node (child of inner)
        double_node = next(n for n in result["nodes"] if n["id"] == "double")
        assert double_node["parentNode"] == "inner"
        assert double_node.get("extent") == "parent"

    def test_nested_graph_both_levels_present(self, nested_graph):
        """Both outer and inner nodes present when expanded."""
        result = render_graph(nested_graph.to_viz_graph(), depth=1)

        fn_and_pipeline = [
            n for n in result["nodes"]
            if n["data"]["nodeType"] in ("FUNCTION", "PIPELINE")
        ]
        ids = {n["id"] for n in fn_and_pipeline}
        assert "inner" in ids  # Pipeline
        assert "double" in ids  # Inner function
        assert "add" in ids  # Outer function

    def test_nested_graph_pipeline_type(self, nested_graph):
        """Expanded pipeline uses pipelineGroup type."""
        result = render_graph(nested_graph.to_viz_graph(), depth=1)

        pipeline_node = next(
            n for n in result["nodes"] if n["data"]["nodeType"] == "PIPELINE"
        )
        assert pipeline_node["type"] == "pipelineGroup"


class TestDoubleNestedGraphCharacterization:
    """Characterize output for double-nested graphs."""

    def test_double_nested_depth_0(self, double_nested_graph):
        """Depth=0 shows only outermost level collapsed."""
        result = render_graph(double_nested_graph.to_viz_graph(), depth=0)

        pipeline_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "PIPELINE"]
        # middle is the outermost pipeline
        assert any(n["id"] == "middle" for n in pipeline_nodes)

        # Check collapsed
        middle_node = next(n for n in result["nodes"] if n["id"] == "middle")
        assert middle_node["data"]["isExpanded"] is False

    def test_double_nested_depth_1(self, double_nested_graph):
        """Current behavior: depth>0 expands ALL pipeline nodes."""
        result = render_graph(double_nested_graph.to_viz_graph(), depth=1)

        # Should see middle and innermost
        pipeline_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "PIPELINE"]
        pipeline_ids = {n["id"] for n in pipeline_nodes}
        assert "middle" in pipeline_ids
        assert "innermost" in pipeline_ids

        # Current behavior: depth>0 expands all (doesn't track nesting level)
        middle_node = next(n for n in result["nodes"] if n["id"] == "middle")
        innermost_node = next(n for n in result["nodes"] if n["id"] == "innermost")
        assert middle_node["data"]["isExpanded"] is True
        assert innermost_node["data"]["isExpanded"] is True

    def test_double_nested_depth_2(self, double_nested_graph):
        """Depth=2 expands all levels."""
        result = render_graph(double_nested_graph.to_viz_graph(), depth=2)

        # All pipelines expanded
        pipeline_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "PIPELINE"]
        for node in pipeline_nodes:
            assert node["data"]["isExpanded"] is True

    def test_double_nested_parent_chain(self, double_nested_graph):
        """Nested children have correct parent references."""
        result = render_graph(double_nested_graph.to_viz_graph(), depth=2)

        # innermost (pipeline) is child of middle
        innermost_node = next(n for n in result["nodes"] if n["id"] == "innermost")
        assert innermost_node.get("parentNode") == "middle"

        # double (function) is child of innermost
        double_node = next(n for n in result["nodes"] if n["id"] == "double")
        assert double_node.get("parentNode") == "innermost"

        # triple (function) is child of middle
        triple_node = next(n for n in result["nodes"] if n["id"] == "triple")
        assert triple_node.get("parentNode") == "middle"


class TestBoundGraphCharacterization:
    """Characterize output for graphs with bound inputs."""

    def test_bound_input_marked(self, bound_graph):
        """Bound inputs have is_bound=True."""
        result = render_graph(bound_graph.to_viz_graph())

        fn_node = next(n for n in result["nodes"] if n["data"]["nodeType"] == "FUNCTION")
        inputs = fn_node["data"]["inputs"]

        a_input = next(inp for inp in inputs if inp["name"] == "a")
        b_input = next(inp for inp in inputs if inp["name"] == "b")

        assert a_input["is_bound"] is True
        assert b_input["is_bound"] is False

    def test_bound_input_group_separate(self, bound_graph):
        """Bound inputs get separate INPUT_GROUP."""
        result = render_graph(bound_graph.to_viz_graph())

        input_groups = [n for n in result["nodes"] if n["data"]["nodeType"] == "INPUT_GROUP"]
        # Should have two groups: one bound (a), one unbound (b)
        assert len(input_groups) == 2

        bound_groups = [g for g in input_groups if g["data"]["isBound"]]
        unbound_groups = [g for g in input_groups if not g["data"]["isBound"]]

        assert len(bound_groups) == 1
        assert len(unbound_groups) == 1

        assert bound_groups[0]["data"]["params"] == ["a"]
        assert unbound_groups[0]["data"]["params"] == ["b"]


class TestMetaCharacterization:
    """Characterize meta output options."""

    def test_meta_theme_preference(self, simple_graph):
        """Meta includes theme preference."""
        result = render_graph(simple_graph.to_viz_graph(), theme="dark")
        assert result["meta"]["theme_preference"] == "dark"

    def test_meta_initial_depth(self, simple_graph):
        """Meta includes initial depth."""
        result = render_graph(simple_graph.to_viz_graph(), depth=2)
        assert result["meta"]["initial_depth"] == 2

    def test_meta_show_types(self, simple_graph):
        """Meta includes show_types flag."""
        result = render_graph(simple_graph.to_viz_graph(), show_types=True)
        assert result["meta"]["show_types"] is True

    def test_meta_separate_outputs(self, simple_graph):
        """Meta includes separate_outputs flag."""
        result = render_graph(simple_graph.to_viz_graph(), separate_outputs=True)
        assert result["meta"]["separate_outputs"] is True

    def test_meta_defaults(self, simple_graph):
        """Meta has correct default values."""
        result = render_graph(simple_graph.to_viz_graph())

        assert result["meta"]["theme_preference"] == "auto"
        assert result["meta"]["initial_depth"] == 1
        assert result["meta"]["show_types"] is False
        assert result["meta"]["separate_outputs"] is False
