"""Tests for the Mermaid flowchart exporter."""

import pytest

from hypergraph import END, Graph, ifelse, node, route
from hypergraph.viz.mermaid import to_mermaid

# =============================================================================
# Test Nodes
# =============================================================================


@node(output_name="embedding")
def embed(text: str) -> list:
    return []


@node(output_name="docs")
def retrieve(embedding: list) -> list:
    return []


@node(output_name="answer")
def generate(docs: list, query: str) -> str:
    return ""


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="result")
def add_one(doubled: int) -> int:
    return doubled + 1


@node(output_name="cleaned")
def clean_text(text: str) -> str:
    return text.strip()


@node(output_name="normalized")
def normalize(cleaned: str) -> str:
    return cleaned.lower()


@ifelse(when_true="fast_path", when_false="slow_path")
def check_cache(query: str) -> bool:
    return False


@node(output_name="fast_result")
def fast_path(query: str) -> str:
    return "cached"


@node(output_name="slow_result")
def slow_path(query: str) -> str:
    return "computed"


@route(targets=["retrieve", END])
def should_continue(answer: str) -> str:
    return END


@node(output_name="step1_out")
def step1(x: int) -> int:
    return x + 1


@node(output_name="step2_out")
def step2(step1_out: int) -> int:
    return step1_out * 2


@node(output_name="done", emit="signal")
def emitter(x: int) -> int:
    return x


@node(output_name="waited_result", wait_for="signal")
def waiter(x: int) -> int:
    return x


# =============================================================================
# Basic Tests
# =============================================================================


class TestBasicMermaid:
    """Core Mermaid generation tests."""

    def test_simple_chain(self):
        """Two-node chain produces correct flowchart."""
        graph = Graph(nodes=[double, add_one])
        mermaid = graph.to_mermaid()

        assert mermaid.startswith("flowchart TD")
        assert 'double["double"]' in mermaid
        assert 'add_one["add_one"]' in mermaid
        assert "-->|doubled|" in mermaid

    def test_direction_parameter(self):
        """Direction parameter changes flowchart header."""
        graph = Graph(nodes=[double])
        assert graph.to_mermaid(direction="LR").startswith("flowchart LR")
        assert graph.to_mermaid(direction="BT").startswith("flowchart BT")
        assert graph.to_mermaid(direction="RL").startswith("flowchart RL")

    def test_invalid_direction_raises(self):
        """Invalid direction raises ValueError."""
        graph = Graph(nodes=[double])
        with pytest.raises(ValueError, match="Invalid direction"):
            graph.to_mermaid(direction="XX")

    def test_input_nodes_rendered(self):
        """External inputs appear as stadium-shaped nodes."""
        graph = Graph(nodes=[double])
        mermaid = graph.to_mermaid()

        # Input node for 'x'
        assert 'input_x(["x"])' in mermaid

    def test_input_to_function_edge(self):
        """Edges from input nodes to consuming functions."""
        graph = Graph(nodes=[double])
        mermaid = graph.to_mermaid()

        assert "input_x --> double" in mermaid

    def test_multi_input_graph(self):
        """Graph with multiple inputs creates multiple input nodes."""
        graph = Graph(nodes=[embed, retrieve, generate])
        mermaid = graph.to_mermaid()

        assert "input_text" in mermaid
        assert "input_query" in mermaid

    def test_no_emojis_in_output(self):
        """Output contains no emoji characters."""
        graph = Graph(nodes=[embed, retrieve, generate])
        mermaid = graph.to_mermaid()

        # Check for common emoji ranges
        import re
        emoji_pattern = re.compile(
            "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
            "\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF"
            "\U00002702-\U000027B0\U0001F900-\U0001F9FF]"
        )
        assert not emoji_pattern.search(str(mermaid))


# =============================================================================
# Type Annotations
# =============================================================================


class TestShowTypes:
    """Tests for show_types parameter."""

    def test_types_in_labels(self):
        """show_types=True adds type annotations to node labels."""
        graph = Graph(nodes=[double, add_one])
        mermaid = graph.to_mermaid(show_types=True)

        assert "doubled: int" in mermaid

    def test_input_types(self):
        """Input nodes show type hints when show_types=True."""
        graph = Graph(nodes=[double])
        mermaid = graph.to_mermaid(show_types=True)

        assert "x: int" in mermaid

    def test_no_types_by_default(self):
        """Types are not shown when show_types=False (default)."""
        graph = Graph(nodes=[double])
        mermaid = graph.to_mermaid()

        # Should have plain label without type
        assert 'input_x(["x"])' in mermaid


# =============================================================================
# Node Shapes
# =============================================================================


class TestNodeShapes:
    """Tests for correct Mermaid shape syntax."""

    def test_function_shape(self):
        """FUNCTION nodes use rectangle syntax."""
        graph = Graph(nodes=[double])
        mermaid = graph.to_mermaid()

        assert 'double["double"]' in mermaid

    def test_graph_collapsed_shape(self):
        """Collapsed GraphNode uses subroutine (double-border) syntax."""
        inner = Graph(nodes=[step1, step2], name="pipeline")
        graph = Graph(nodes=[inner.as_node(), add_one])
        mermaid = graph.to_mermaid(depth=0)

        assert 'pipeline[["pipeline"]]' in mermaid

    def test_branch_diamond_shape(self):
        """Branch nodes use diamond syntax."""
        graph = Graph(nodes=[check_cache, fast_path, slow_path])
        mermaid = graph.to_mermaid()

        assert 'check_cache{"check_cache"}' in mermaid

    def test_data_parallelogram_shape(self):
        """DATA nodes use parallelogram syntax in separate_outputs mode."""
        graph = Graph(nodes=[double, add_one])
        mermaid = graph.to_mermaid(separate_outputs=True)

        assert '[/"doubled"/]' in mermaid


# =============================================================================
# Edge Types
# =============================================================================


class TestEdgeTypes:
    """Tests for different edge type rendering."""

    def test_data_edge_label(self):
        """Data edges show the output name as label."""
        graph = Graph(nodes=[double, add_one])
        mermaid = graph.to_mermaid()

        assert "-->|doubled|" in mermaid

    def test_control_edge_true_false(self):
        """IfElse control edges show True/False labels."""
        graph = Graph(nodes=[check_cache, fast_path, slow_path])
        mermaid = graph.to_mermaid()

        assert "-->|True|" in mermaid
        assert "-->|False|" in mermaid

    def test_ordering_edge_dotted(self):
        """Ordering edges use dotted arrow syntax."""

        graph = Graph(nodes=[emitter, waiter])
        mermaid = graph.to_mermaid()

        assert "-.->" in mermaid

    def test_end_node_routing(self):
        """END node appears when a route targets END."""
        graph = Graph(nodes=[embed, retrieve, generate, should_continue])
        mermaid = graph.to_mermaid()

        assert '(["End"])' in mermaid


# =============================================================================
# Nested Graphs / Subgraphs
# =============================================================================


class TestSubgraphs:
    """Tests for nested graph expansion."""

    def test_collapsed_nested(self):
        """Nested graph at depth=0 renders as single subroutine node."""
        inner = Graph(nodes=[step1, step2], name="pipeline")
        graph = Graph(nodes=[inner.as_node(), add_one])
        mermaid = graph.to_mermaid(depth=0)

        assert 'pipeline[["pipeline"]]' in mermaid
        assert "subgraph" not in mermaid

    def test_expanded_nested(self):
        """Nested graph at depth=1 renders as subgraph block."""
        inner = Graph(nodes=[step1, step2], name="pipeline")
        graph = Graph(nodes=[inner.as_node(), add_one])
        mermaid = graph.to_mermaid(depth=1)

        assert 'subgraph pipeline' in mermaid
        assert "end" in mermaid
        assert "step1" in mermaid
        assert "step2" in mermaid


# =============================================================================
# Separate Outputs
# =============================================================================


class TestSeparateOutputs:
    """Tests for separate_outputs mode."""

    def test_data_nodes_created(self):
        """DATA nodes appear when separate_outputs=True."""
        graph = Graph(nodes=[double, add_one])
        mermaid = graph.to_mermaid(separate_outputs=True)

        assert "data_double_doubled" in mermaid

    def test_data_intermediary_edges(self):
        """Edges route through DATA nodes in separate mode."""
        graph = Graph(nodes=[double, add_one])
        mermaid = graph.to_mermaid(separate_outputs=True)

        # double → data node
        assert "double --> data_double_doubled" in mermaid
        # data node → add_one
        assert "data_double_doubled" in mermaid
        assert "add_one" in mermaid

    def test_no_data_nodes_merged(self):
        """DATA nodes do not appear in default merged mode."""
        graph = Graph(nodes=[double, add_one])
        mermaid = graph.to_mermaid(separate_outputs=False)

        assert "data_double_doubled" not in mermaid


# =============================================================================
# Custom Colors
# =============================================================================


class TestCustomColors:
    """Tests for custom color scheme overrides."""

    def test_default_colors(self):
        """Default classDef statements are present."""
        graph = Graph(nodes=[double])
        mermaid = graph.to_mermaid()

        assert "classDef function" in mermaid
        assert "classDef input" in mermaid
        assert "#E8EAF6" in mermaid  # default function fill

    def test_custom_color_override(self):
        """Custom colors override defaults."""
        graph = Graph(nodes=[double])
        mermaid = graph.to_mermaid(
            colors={"function": {"fill": "#FF0000", "stroke": "#CC0000"}}
        )

        assert "#FF0000" in mermaid
        assert "#CC0000" in mermaid

    def test_class_assignments(self):
        """Nodes are assigned to their correct style class."""
        graph = Graph(nodes=[double])
        mermaid = graph.to_mermaid()

        assert "class " in mermaid
        assert "function" in mermaid


# =============================================================================
# Styling Section
# =============================================================================


class TestStyling:
    """Tests for the style section of Mermaid output."""

    def test_ordering_edge_linkstyle(self):
        """Ordering edges get purple linkStyle."""
        graph = Graph(nodes=[emitter, waiter])
        mermaid = graph.to_mermaid()

        assert "linkStyle" in mermaid
        assert "#8b5cf6" in mermaid


# =============================================================================
# Integration: via to_mermaid function
# =============================================================================


class TestToMermaidFunction:
    """Tests for the standalone to_mermaid() function."""

    def test_accepts_flat_graph(self):
        """to_mermaid works with a raw flat graph."""
        graph = Graph(nodes=[double, add_one])
        flat = graph.to_flat_graph()
        mermaid = to_mermaid(flat)

        assert mermaid.startswith("flowchart TD")
        assert "double" in mermaid
        assert "add_one" in mermaid

    def test_all_parameters(self):
        """to_mermaid accepts all keyword parameters."""
        graph = Graph(nodes=[double])
        flat = graph.to_flat_graph()
        mermaid = to_mermaid(
            flat,
            depth=0,
            show_types=True,
            separate_outputs=True,
            direction="LR",
            colors={"function": {"fill": "#FFF"}},
        )

        assert mermaid.startswith("flowchart LR")


# =============================================================================
# MermaidDiagram object
# =============================================================================


class TestMermaidDiagram:
    """Tests for the MermaidDiagram result object."""

    def test_str_returns_source(self):
        """str() returns the raw Mermaid source."""
        graph = Graph(nodes=[double])
        diagram = graph.to_mermaid()

        assert str(diagram).startswith("flowchart TD")

    def test_source_property(self):
        """.source gives direct access to Mermaid markup."""
        graph = Graph(nodes=[double])
        diagram = graph.to_mermaid()

        assert isinstance(diagram.source, str)
        assert "flowchart" in diagram.source

    def test_contains_delegates_to_source(self):
        """'in' operator works on MermaidDiagram."""
        graph = Graph(nodes=[double])
        diagram = graph.to_mermaid()

        assert "double" in diagram
        assert "nonexistent_xyz" not in diagram

    def test_repr_mimebundle_uses_native_mermaid(self):
        """_repr_mimebundle_ returns text/vnd.mermaid for local rendering."""
        graph = Graph(nodes=[double])
        diagram = graph.to_mermaid()
        bundle = diagram._repr_mimebundle_()

        assert "text/vnd.mermaid" in bundle
        assert "flowchart" in bundle["text/vnd.mermaid"]
        assert "text/plain" in bundle
        # No CDN or external service references
        assert "cdn" not in str(bundle).lower()
        assert "http" not in str(bundle).lower()

    def test_repr_shows_summary(self):
        """repr() shows a summary of the diagram."""
        graph = Graph(nodes=[double])
        diagram = graph.to_mermaid()

        assert "MermaidDiagram" in repr(diagram)
        assert "flowchart" in repr(diagram)

    def test_section_comments(self):
        """Output includes section comments for readability."""
        graph = Graph(nodes=[double, add_one])
        diagram = graph.to_mermaid()

        assert "%% Inputs" in diagram
        assert "%% Nodes" in diagram
        assert "%% Edges" in diagram
        assert "%% Styling" in diagram

    def test_text_color_in_classdef(self):
        """classDef includes color property for text."""
        graph = Graph(nodes=[double])
        diagram = graph.to_mermaid()

        assert "color:#283593" in diagram  # function text color
