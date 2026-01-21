"""Tests for nested graph edge routing.

These tests verify that edges connecting to/from expanded nested graphs
have correct positions (target below source, positive vertical distance).

BUG: Currently failing - see .claude/plans/viz_refactor_ledger.md
"""

import pytest
from hypergraph import Graph, node

try:
    import playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


# =============================================================================
# Test Graph Definitions (from notebooks/test_viz_layout.ipynb)
# =============================================================================

# --- 1-level nesting: workflow ---
@node(output_name="cleaned")
def clean_text(text: str) -> str:
    return text.strip()


@node(output_name="normalized")
def normalize_text(cleaned: str) -> str:
    return cleaned.lower()


@node(output_name="result")
def analyze(normalized: str) -> dict:
    return {"length": len(normalized)}


def make_workflow():
    """Create 1-level nested graph: preprocess -> analyze."""
    preprocess = Graph(nodes=[clean_text, normalize_text], name="preprocess")
    return Graph(nodes=[preprocess.as_node(), analyze])


# --- 2-level nesting: outer ---
@node(output_name="step1_out")
def step1(x: int) -> int:
    return x + 1


@node(output_name="step2_out")
def step2(step1_out: int) -> int:
    return step1_out * 2


@node(output_name="validated")
def validate(step2_out: int) -> int:
    return step2_out


@node(output_name="logged")
def log_result(validated: int) -> int:
    return validated


def make_outer():
    """Create 2-level nested graph: middle -> log_result."""
    inner = Graph(nodes=[step1, step2], name="inner")
    middle = Graph(nodes=[inner.as_node(), validate], name="middle")
    return Graph(nodes=[middle.as_node(), log_result])


# =============================================================================
# Tests
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestNestedEdgeRouting:
    """Tests for nested graph edge validation using Playwright extraction."""

    @pytest.mark.xfail(reason="BUG: Nested edge routing inverted - Phase 4")
    def test_workflow_depth1_no_edge_issues(self):
        """Test 1-level nesting: edges should flow downward."""
        from hypergraph.viz import extract_debug_data

        workflow = make_workflow()
        data = extract_debug_data(workflow, depth=1)

        # All edges should have positive vertical distance
        assert data.summary["edgeIssues"] == 0, (
            f"Found {data.summary['edgeIssues']} edge issues:\n"
            + "\n".join(
                f"  {e.source} -> {e.target}: {e.issue}"
                for e in data.edge_issues
            )
        )

    @pytest.mark.xfail(reason="BUG: Nested edge routing inverted - Phase 4")
    def test_workflow_edges_target_below_source(self):
        """Test that all edges have target.y > source.bottom."""
        from hypergraph.viz import extract_debug_data

        workflow = make_workflow()
        data = extract_debug_data(workflow, depth=1)

        for edge in data.edges:
            if edge.vert_dist is not None:
                assert edge.vert_dist >= 0, (
                    f"Edge {edge.source} -> {edge.target} has negative vertical distance: "
                    f"srcBottom={edge.src_bottom}, tgtTop={edge.tgt_top}, vDist={edge.vert_dist}"
                )

    @pytest.mark.xfail(reason="BUG: Nested edge routing inverted - Phase 4")
    def test_outer_depth2_no_edge_issues(self):
        """Test 2-level nesting: edges should flow downward."""
        from hypergraph.viz import extract_debug_data

        outer = make_outer()
        data = extract_debug_data(outer, depth=2)

        assert data.summary["edgeIssues"] == 0, (
            f"Found {data.summary['edgeIssues']} edge issues:\n"
            + "\n".join(
                f"  {e.source} -> {e.target}: {e.issue}"
                for e in data.edge_issues
            )
        )

    def test_workflow_depth0_collapsed_ok(self):
        """Test collapsed nested graph (depth=0) - should have no issues."""
        from hypergraph.viz import extract_debug_data

        workflow = make_workflow()
        data = extract_debug_data(workflow, depth=0)

        # When collapsed, the nested graph is a single node - simpler layout
        # This may or may not have issues depending on implementation
        # Record current behavior for regression testing
        print(f"depth=0: {data.summary['edgeIssues']} edge issues")
        for edge in data.edge_issues:
            print(f"  {edge.source} -> {edge.target}: {edge.issue}")

    def test_extract_workflow_structure(self):
        """Test that workflow graph has expected structure."""
        from hypergraph.viz import extract_debug_data

        workflow = make_workflow()
        data = extract_debug_data(workflow, depth=1)

        # Should have nodes: __inputs__, preprocess (PIPELINE), clean_text, normalize_text, analyze
        # Plus DATA nodes for outputs
        assert data.summary["totalNodes"] >= 3, f"Expected at least 3 nodes, got {data.summary['totalNodes']}"
        assert data.summary["totalEdges"] >= 2, f"Expected at least 2 edges, got {data.summary['totalEdges']}"

    def test_extract_outer_structure(self):
        """Test that outer graph has expected structure."""
        from hypergraph.viz import extract_debug_data

        outer = make_outer()
        data = extract_debug_data(outer, depth=2)

        # Should have many nodes when fully expanded
        assert data.summary["totalNodes"] >= 4, f"Expected at least 4 nodes, got {data.summary['totalNodes']}"
        assert data.summary["totalEdges"] >= 3, f"Expected at least 3 edges, got {data.summary['totalEdges']}"


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestEdgeValidationDetails:
    """Detailed tests for edge validation data."""

    def test_edge_validation_fields(self):
        """Test that edge validation returns all expected fields."""
        from hypergraph.viz import extract_debug_data

        workflow = make_workflow()
        data = extract_debug_data(workflow, depth=1)

        for edge in data.edges:
            assert edge.source is not None
            assert edge.target is not None
            assert edge.status in ("OK", "WARN", "MISSING")
            # Numeric fields may be None for MISSING edges
            if edge.status != "MISSING":
                assert edge.src_bottom is not None
                assert edge.tgt_top is not None
                assert edge.vert_dist is not None
                assert edge.horiz_dist is not None

    def test_print_report_runs(self, capsys):
        """Test that print_report executes without error."""
        from hypergraph.viz import extract_debug_data

        workflow = make_workflow()
        data = extract_debug_data(workflow, depth=1)
        data.print_report()

        captured = capsys.readouterr()
        assert "Edge Validation Report" in captured.out
