"""Tests for the visualization debug utilities."""

import pytest

from hypergraph import Graph, node
from hypergraph.viz.debug import (
    IssueReport,
    ValidationResult,
    VizDebugger,
    find_issues,
    validate_graph,
)
from tests.viz.conftest import HAS_PLAYWRIGHT


@node(output_name="doubled")
def double(x: int) -> int:
    """Double a number."""
    return x * 2


@node(output_name="result")
def add_one(doubled: int) -> int:
    """Add one."""
    return doubled + 1


@node(output_name="tripled")
def triple(x: int) -> int:
    """Triple a number."""
    return x * 3


@node(output_name="standalone")
def standalone() -> int:
    """A standalone node with no inputs or connections."""
    return 42


class TestValidation:
    """Tests for graph validation."""

    def test_validate_valid_graph(self):
        """Test that a valid graph passes validation."""
        graph = Graph(nodes=[double, add_one])
        result = validate_graph(graph)

        assert result.valid is True
        assert result.errors == []

    def test_validate_single_node(self):
        """Test validation of a single node graph."""
        graph = Graph(nodes=[double])
        result = validate_graph(graph)

        assert result.valid is True

    def test_validate_nested_graph(self):
        """Test validation of a nested graph."""
        inner = Graph(nodes=[double], name="inner")
        outer = Graph(nodes=[inner.as_node(), add_one])

        result = validate_graph(outer)

        assert result.valid is True

    def test_validation_result_dataclass(self):
        """Test ValidationResult structure."""
        result = ValidationResult(valid=True, errors=[], warnings=["test warning"])

        assert result.valid is True
        assert result.errors == []
        assert result.warnings == ["test warning"]


class TestTraceNode:
    """Tests for node tracing."""

    def test_trace_node_found(self):
        """Test tracing a node that exists."""
        graph = Graph(nodes=[double, add_one])
        debugger = VizDebugger(graph)

        info = debugger.trace_node("double")

        assert info.status == "FOUND"
        assert info.node_id == "double"
        assert info.node_type == "FUNCTION"
        assert info.parent is None
        assert "doubled" in info.details["outputs"]
        assert "x" in info.details["inputs"]

    def test_trace_node_incoming_edges(self):
        """Test that incoming edges are traced correctly."""
        graph = Graph(nodes=[double, add_one])
        debugger = VizDebugger(graph)

        info = debugger.trace_node("add_one")

        # add_one receives 'doubled' from double
        assert len(info.incoming_edges) == 1
        assert info.incoming_edges[0]["from"] == "double"
        assert info.incoming_edges[0]["value"] == "doubled"

    def test_trace_node_outgoing_edges(self):
        """Test that outgoing edges are traced correctly."""
        graph = Graph(nodes=[double, add_one])
        debugger = VizDebugger(graph)

        info = debugger.trace_node("double")

        # double sends 'doubled' to add_one
        assert len(info.outgoing_edges) == 1
        assert info.outgoing_edges[0]["to"] == "add_one"
        assert info.outgoing_edges[0]["value"] == "doubled"

    def test_trace_node_not_found(self):
        """Test tracing a node that doesn't exist."""
        graph = Graph(nodes=[double])
        debugger = VizDebugger(graph)

        info = debugger.trace_node("nonexistent")

        assert info.status == "NOT_FOUND"
        assert info.node_id == "nonexistent"

    def test_trace_node_partial_matches(self):
        """Test that partial matches are found for typos."""
        graph = Graph(nodes=[double, add_one])
        debugger = VizDebugger(graph)

        info = debugger.trace_node("doub")  # Partial match

        assert info.status == "NOT_FOUND"
        assert "double" in info.partial_matches

    def test_trace_nested_node(self):
        """Test tracing a node inside a nested graph."""
        inner = Graph(nodes=[double], name="inner")
        outer = Graph(nodes=[inner.as_node(), add_one])
        debugger = VizDebugger(outer)

        # Use hierarchical ID to trace nested node
        info = debugger.trace_node("inner/double")

        assert info.status == "FOUND"
        assert info.parent == "inner"

    def test_trace_graph_node_children(self):
        """Test that GRAPH nodes include their children."""
        inner = Graph(nodes=[double], name="inner")
        outer = Graph(nodes=[inner.as_node()])
        debugger = VizDebugger(outer)

        info = debugger.trace_node("inner")

        assert info.node_type == "GRAPH"
        assert "children" in info.details
        # Children now have hierarchical IDs
        assert "inner/double" in info.details["children"]


class TestTraceEdge:
    """Tests for edge tracing."""

    def test_trace_edge_found(self):
        """Test tracing an edge that exists."""
        graph = Graph(nodes=[double, add_one])
        debugger = VizDebugger(graph)

        edge = debugger.trace_edge("double", "add_one")

        assert edge.edge_found is True
        assert edge.edge_query == "double -> add_one"
        assert edge.source_info["found"] is True
        assert edge.target_info["found"] is True

    def test_trace_edge_missing(self):
        """Test tracing an edge that doesn't exist."""
        graph = Graph(nodes=[double, triple])  # No connection between them
        debugger = VizDebugger(graph)

        edge = debugger.trace_edge("double", "triple")

        assert edge.edge_found is False
        assert "suggestion" in edge.analysis

    def test_trace_edge_source_not_found(self):
        """Test tracing with missing source node."""
        graph = Graph(nodes=[double])
        debugger = VizDebugger(graph)

        edge = debugger.trace_edge("nonexistent", "double")

        assert edge.edge_found is False
        assert edge.source_info["found"] is False

    def test_trace_edge_analysis(self):
        """Test that edge analysis provides useful info."""
        graph = Graph(nodes=[double, add_one])
        debugger = VizDebugger(graph)

        # Edge exists, so we trace a different pair
        edge = debugger.trace_edge("add_one", "double")  # Wrong direction

        assert edge.edge_found is False
        # Analysis shows what edges actually exist from add_one
        assert "edges_from_source" in edge.analysis


class TestFindIssues:
    """Tests for comprehensive issue finding."""

    def test_find_issues_clean_graph(self):
        """Test that a clean graph has no issues."""
        graph = Graph(nodes=[double, add_one])
        issues = find_issues(graph)

        assert issues.has_issues is False
        assert issues.validation_errors == []
        assert issues.orphan_edges == []
        assert issues.self_loops == []

    def test_find_issues_disconnected_nodes(self):
        """Test detection of disconnected nodes."""
        # standalone has no inputs and no consumers
        graph = Graph(nodes=[standalone])
        issues = find_issues(graph)

        # standalone is disconnected (no edges in/out, not a parent)
        assert "standalone" in issues.disconnected_nodes

    def test_issue_report_has_issues(self):
        """Test has_issues property."""
        report = IssueReport(validation_errors=["test error"])
        assert report.has_issues is True

        empty_report = IssueReport()
        assert empty_report.has_issues is False


class TestDebugDump:
    """Tests for debug dump functionality."""

    def test_debug_dump_structure(self):
        """Test that debug dump contains expected structure."""
        graph = Graph(nodes=[double, add_one])
        debugger = VizDebugger(graph)

        dump = debugger.debug_dump()

        assert "nodes" in dump
        assert "edges" in dump
        assert "metadata" in dump
        assert "validation" in dump
        assert "stats" in dump

    def test_debug_dump_nodes(self):
        """Test that nodes are correctly serialized."""
        graph = Graph(nodes=[double])
        debugger = VizDebugger(graph)

        dump = debugger.debug_dump()

        node_ids = {n["id"] for n in dump["nodes"]}
        assert "double" in node_ids

    def test_debug_dump_edges_by_source(self):
        """Test edges_by_source map."""
        graph = Graph(nodes=[double, add_one])
        debugger = VizDebugger(graph)

        dump = debugger.debug_dump()

        edges_by_source = dump["metadata"]["edges_by_source"]
        assert "double" in edges_by_source
        assert "add_one" in edges_by_source["double"]

    def test_debug_dump_edges_by_target(self):
        """Test edges_by_target map ("points from")."""
        graph = Graph(nodes=[double, add_one])
        debugger = VizDebugger(graph)

        dump = debugger.debug_dump()

        edges_by_target = dump["metadata"]["edges_by_target"]
        assert "add_one" in edges_by_target
        assert "double" in edges_by_target["add_one"]

    def test_debug_dump_stats(self):
        """Test stats in debug dump."""
        graph = Graph(nodes=[double, add_one])
        debugger = VizDebugger(graph)

        dump = debugger.debug_dump()

        assert dump["stats"]["total_nodes"] == 2
        assert dump["stats"]["total_edges"] == 1
        assert dump["stats"]["has_cycles"] is False
        assert "FUNCTION" in dump["stats"]["node_types"]


class TestGraphDebugViz:
    """Tests for the Graph.debug_viz() convenience method."""

    def test_debug_viz_returns_debugger(self):
        """Test that debug_viz() returns a VizDebugger."""
        graph = Graph(nodes=[double])
        debugger = graph.debug_viz()

        assert isinstance(debugger, VizDebugger)
        assert debugger.graph is graph

    def test_debug_viz_can_trace(self):
        """Test that debug_viz() can trace nodes."""
        graph = Graph(nodes=[double, add_one])
        debugger = graph.debug_viz()

        info = debugger.trace_node("double")
        assert info.status == "FOUND"


class TestDebugVisualize:
    """Tests for VizDebugger.visualize() method."""

    def test_visualize_returns_widget(self, capsys):
        """Test that visualize() returns a widget with debug overlays."""
        from hypergraph.viz.widget import ScrollablePipelineWidget

        graph = Graph(nodes=[double, add_one])
        debugger = VizDebugger(graph)

        widget = debugger.visualize()

        assert isinstance(widget, ScrollablePipelineWidget)
        # Check debug info was printed
        captured = capsys.readouterr()
        assert "Debug Visualization" in captured.out
        assert "Nodes:" in captured.out

    def test_visualize_prints_issues(self, capsys):
        """Test that visualize() prints issues when found."""
        graph = Graph(nodes=[standalone])
        debugger = VizDebugger(graph)

        debugger.visualize()

        captured = capsys.readouterr()
        assert "Disconnected nodes" in captured.out

    def test_visualize_enables_debug_overlays(self):
        """Test that visualize() enables debug overlays in the HTML."""
        graph = Graph(nodes=[double])
        debugger = VizDebugger(graph)

        widget = debugger.visualize()

        # The HTML should contain debug_overlays: true in the meta
        assert '"debug_overlays": true' in widget.html_content


class TestCacheInvalidation:
    """Tests for cache invalidation."""

    def test_invalidate_cache(self):
        """Test that invalidate_cache clears the cached graph."""
        graph = Graph(nodes=[double])
        debugger = VizDebugger(graph)

        # Access flat_graph to cache it
        _ = debugger.flat_graph
        assert debugger._flat_graph is not None

        # Invalidate
        debugger.invalidate_cache()
        assert debugger._flat_graph is None

        # Recompute on next access
        _ = debugger.flat_graph
        assert debugger._flat_graph is not None


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestExtractDebugData:
    """Tests for Playwright-based debug data extraction."""

    def test_extract_debug_data(self):
        """Test extracting debug data via Playwright."""
        from hypergraph.viz.debug import extract_debug_data

        graph = Graph(nodes=[double, add_one])
        data = extract_debug_data(graph, depth=1)

        # Check structure
        assert data.version > 0
        assert data.timestamp > 0
        assert len(data.nodes) >= 2
        assert len(data.edges) >= 1

        # Check summary
        assert data.summary["totalNodes"] >= 2
        assert data.summary["totalEdges"] >= 1

    def test_extract_debug_data_print_report(self, capsys):
        """Test that print_report works."""
        from hypergraph.viz.debug import extract_debug_data

        graph = Graph(nodes=[double, add_one])
        data = extract_debug_data(graph)
        data.print_report()

        captured = capsys.readouterr()
        assert "Edge Validation Report" in captured.out
        assert "Nodes:" in captured.out
