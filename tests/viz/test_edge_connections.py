"""Tests for edge connection accuracy - edges must connect to node boundaries.

These tests validate that edges in the rendered visualization connect precisely
to node boundaries with 0px tolerance. Any gap between an edge endpoint and
its corresponding node boundary is a bug.

Validation Rules:
1. Edge start -> center-bottom of source node (0px tolerance)
2. Edge end -> center-top of target node (0px tolerance)
3. Source above target -> source node's bottom Y < target node's top Y

Measurement Rules:
- Node boundaries exclude shadow/glow - measure the actual box element
- 0px tolerance - any gap is a bug, no "acceptable" deviation

Test Graphs:
- simple: 2-node chain (a -> b)
- chain: 3-node chain (a -> b -> c)
- workflow: 1-level nesting (preprocess[clean_text, normalize] -> analyze)
- outer: 2-level nesting (middle[inner[step1, step2], validate] -> log_result)
"""

import pytest
from hypergraph import Graph
from hypergraph.viz.geometry import (
    NodeGeometry,
    EdgeGeometry,
    EdgeConnectionValidator,
    format_issues,
)

# Import shared fixtures and helpers from conftest
from tests.viz.conftest import (
    HAS_PLAYWRIGHT,
    make_simple_graph,
    make_chain_graph,
    make_workflow,
    make_outer,
    extract_inner_bounds_and_edge_paths,
    convert_layout_to_screen,
)


# =============================================================================
# Extraction Helper
# =============================================================================

def extract_geometries(page, graph, depth: int) -> tuple[dict[str, NodeGeometry], list[EdgeGeometry]]:
    """Extract node and edge geometry from rendered visualization.

    Args:
        page: Playwright page object
        graph: The graph to render
        depth: Expansion depth for nested graphs

    Returns:
        Tuple of (nodes dict, edges list) with geometry data
    """
    from hypergraph.viz.widget import visualize
    import tempfile
    import os

    # Render to temp file
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        temp_path = f.name
    visualize(graph, depth=depth, output=temp_path, _debug_overlays=True)

    try:
        page.goto(f"file://{temp_path}")

        # Wait for layout to complete
        page.wait_for_function(
            "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0",
            timeout=10000,
        )

        # Extract node geometry
        raw_nodes = page.evaluate("window.__hypergraphVizDebug.nodes")
        nodes = {
            n["id"]: NodeGeometry(
                id=n["id"],
                x=n["x"],
                y=n["y"],
                width=n["width"],
                height=n["height"],
            )
            for n in raw_nodes
        }

        # Extract edge geometry from SVG paths (call function directly for fresh data)
        raw_edges = page.evaluate(
            "window.__hypergraphVizExtractEdgePaths ? window.__hypergraphVizExtractEdgePaths() : []"
        )

        edges = []
        for e in raw_edges:
            if e.get("source") and e.get("target"):
                edges.append(
                    EdgeGeometry(
                        source_id=e["source"],
                        target_id=e["target"],
                        start_point=(e["pathStart"]["x"], e["pathStart"]["y"]),
                        end_point=(e["pathEnd"]["x"], e["pathEnd"]["y"]),
                    )
                )

        return nodes, edges

    finally:
        os.unlink(temp_path)


# Note: page fixture is provided by conftest.py


# =============================================================================
# Tests: Simple Graph (No Nesting)
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestEdgeConnectionsSimple:
    """Simple graph: no nesting, basic edge validation."""

    def test_simple_graph_edges_valid(self, page):
        """All edges in a 2-node graph should be valid with 0px tolerance."""
        graph = make_simple_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        validator = EdgeConnectionValidator(nodes, edges, tolerance=0.0)
        issues = validator.validate_all()

        assert issues == {}, f"Edge connection issues:\n{format_issues(issues)}"

    def test_chain_graph_edges_valid(self, page):
        """All edges in a 3-node chain should be valid with 0px tolerance."""
        graph = make_chain_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        validator = EdgeConnectionValidator(nodes, edges, tolerance=0.0)
        issues = validator.validate_all()

        assert issues == {}, f"Edge connection issues:\n{format_issues(issues)}"

    def test_source_above_target(self, page):
        """Source node must be positioned above target node."""
        graph = make_simple_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        for edge in edges:
            src = nodes.get(edge.source_id)
            tgt = nodes.get(edge.target_id)
            if src and tgt:
                assert src.bottom < tgt.y, (
                    f"Source '{src.id}' not above target '{tgt.id}': "
                    f"src.bottom={src.bottom:.1f} >= tgt.y={tgt.y:.1f}"
                )


# =============================================================================
# Tests: 1-Level Nesting (workflow graph)
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestWorkflowDepth0:
    """Workflow graph at depth=0 (preprocess collapsed)."""

    def test_workflow_depth0_edges_valid(self, page):
        """Edges with collapsed preprocess should have 0px tolerance."""
        graph = make_workflow()
        nodes, edges = extract_geometries(page, graph, depth=0)

        validator = EdgeConnectionValidator(nodes, edges, tolerance=0.0)
        issues = validator.validate_all()

        assert issues == {}, f"Edge connection issues:\n{format_issues(issues)}"

    def test_workflow_depth0_structure(self, page):
        """Collapsed workflow should have expected node count."""
        graph = make_workflow()
        nodes, edges = extract_geometries(page, graph, depth=0)

        # At depth=0: input_text, preprocess (collapsed), analyze
        # Plus possibly data nodes
        assert len(nodes) >= 3, f"Expected at least 3 nodes, got {len(nodes)}: {list(nodes.keys())}"
        assert len(edges) >= 2, f"Expected at least 2 edges, got {len(edges)}"


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestWorkflowDepth1:
    """Workflow graph at depth=1 (preprocess expanded)."""

    def test_workflow_depth1_edges_valid(self, page):
        """Edges with expanded preprocess should have 0px tolerance."""
        graph = make_workflow()
        nodes, edges = extract_geometries(page, graph, depth=1)

        validator = EdgeConnectionValidator(nodes, edges, tolerance=0.0)
        issues = validator.validate_all()

        assert issues == {}, f"Edge connection issues:\n{format_issues(issues)}"

    def test_workflow_depth1_structure(self, page):
        """Expanded workflow should show internal nodes."""
        graph = make_workflow()
        nodes, edges = extract_geometries(page, graph, depth=1)

        # At depth=1: input_text, preprocess, clean_text, normalize_text, analyze
        node_ids = set(nodes.keys())
        assert "clean_text" in node_ids or any("clean" in n for n in node_ids), (
            f"Expected clean_text in expanded nodes: {node_ids}"
        )

    def test_workflow_depth1_input_edge_to_internal(self, page):
        """Input edge should connect to clean_text, not preprocess container."""
        graph = make_workflow()
        nodes, edges = extract_geometries(page, graph, depth=1)

        # Find clean_text node
        clean_text_node = nodes.get("clean_text")
        if clean_text_node:
            # Find edge targeting clean_text
            for edge in edges:
                if edge.target_id == "clean_text":
                    # Edge should end exactly at clean_text's top
                    expected_y = clean_text_node.y
                    actual_y = edge.end_point[1]
                    gap = abs(actual_y - expected_y)
                    assert gap == 0, (
                        f"Input edge has {gap:.1f}px gap from clean_text top:\n"
                        f"  Expected Y: {expected_y:.1f}\n"
                        f"  Actual Y: {actual_y:.1f}"
                    )

    def test_workflow_depth1_output_edge_from_internal(self, page):
        """Output edge should connect from normalize_text, not preprocess container."""
        graph = make_workflow()
        nodes, edges = extract_geometries(page, graph, depth=1)

        # Find edge to analyze
        for edge in edges:
            if edge.target_id == "analyze":
                src = nodes.get(edge.source_id)
                if src:
                    # Edge should start exactly at source's bottom
                    expected_y = src.bottom
                    actual_y = edge.start_point[1]
                    gap = abs(actual_y - expected_y)
                    assert gap == 0, (
                        f"Output edge has {gap:.1f}px gap from {edge.source_id} bottom:\n"
                        f"  Expected Y: {expected_y:.1f}\n"
                        f"  Actual Y: {actual_y:.1f}"
                    )


# =============================================================================
# Tests: 2-Level Nesting (outer graph)
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestOuterDepth0:
    """Outer graph at depth=0 (middle collapsed, inner collapsed)."""

    def test_outer_depth0_edges_valid(self, page):
        """Edges with all collapsed should have 0px tolerance."""
        graph = make_outer()
        nodes, edges = extract_geometries(page, graph, depth=0)

        validator = EdgeConnectionValidator(nodes, edges, tolerance=0.0)
        issues = validator.validate_all()

        assert issues == {}, f"Edge connection issues:\n{format_issues(issues)}"

    def test_outer_depth0_structure(self, page):
        """All-collapsed outer should have minimal nodes."""
        graph = make_outer()
        nodes, edges = extract_geometries(page, graph, depth=0)

        # At depth=0: input_x, middle (collapsed), log_result
        assert len(nodes) >= 3, f"Expected at least 3 nodes, got {len(nodes)}: {list(nodes.keys())}"


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestOuterDepth1:
    """Outer graph at depth=1 (middle expanded, inner collapsed)."""

    def test_outer_depth1_edges_valid(self, page):
        """Edges with middle expanded should have 0px tolerance."""
        graph = make_outer()
        nodes, edges = extract_geometries(page, graph, depth=1)

        validator = EdgeConnectionValidator(nodes, edges, tolerance=0.0)
        issues = validator.validate_all()

        assert issues == {}, f"Edge connection issues:\n{format_issues(issues)}"

    def test_outer_depth1_structure(self, page):
        """Depth=1 should show inner (collapsed) and validate."""
        graph = make_outer()
        nodes, edges = extract_geometries(page, graph, depth=1)

        node_ids = set(nodes.keys())
        # Should have inner (collapsed) and validate visible
        assert "inner" in node_ids or "validate" in node_ids, (
            f"Expected inner or validate in nodes: {node_ids}"
        )

    def test_outer_depth1_input_routes_to_inner(self, page):
        """Input edge should route to inner container, not middle."""
        graph = make_outer()
        nodes, edges = extract_geometries(page, graph, depth=1)

        inner_node = nodes.get("inner")
        if inner_node:
            # Find edge targeting inner
            for edge in edges:
                if edge.target_id == "inner":
                    expected_y = inner_node.y
                    actual_y = edge.end_point[1]
                    gap = abs(actual_y - expected_y)
                    assert gap == 0, (
                        f"Input edge has {gap:.1f}px gap from inner top:\n"
                        f"  Expected Y: {expected_y:.1f}\n"
                        f"  Actual Y: {actual_y:.1f}"
                    )


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestOuterDepth2:
    """Outer graph at depth=2 (fully expanded)."""

    def test_outer_depth2_edges_valid(self, page):
        """Edges with full expansion should have 0px tolerance."""
        graph = make_outer()
        nodes, edges = extract_geometries(page, graph, depth=2)

        validator = EdgeConnectionValidator(nodes, edges, tolerance=0.0)
        issues = validator.validate_all()

        assert issues == {}, f"Edge connection issues:\n{format_issues(issues)}"

    def test_outer_depth2_structure(self, page):
        """Fully expanded should show all internal nodes."""
        graph = make_outer()
        nodes, edges = extract_geometries(page, graph, depth=2)

        node_ids = set(nodes.keys())
        # Should have step1, step2, validate visible
        assert "step1" in node_ids or any("step1" in n for n in node_ids), (
            f"Expected step1 in fully expanded nodes: {node_ids}"
        )

    def test_outer_depth2_input_routes_to_step1(self, page):
        """Input edge should route to step1, the deepest internal node."""
        graph = make_outer()
        nodes, edges = extract_geometries(page, graph, depth=2)

        step1_node = nodes.get("step1")
        if step1_node:
            # Find edge targeting step1
            for edge in edges:
                if edge.target_id == "step1":
                    expected_y = step1_node.y
                    actual_y = edge.end_point[1]
                    gap = abs(actual_y - expected_y)
                    assert gap == 0, (
                        f"Input edge has {gap:.1f}px gap from step1 top:\n"
                        f"  Expected Y: {expected_y:.1f}\n"
                        f"  Actual Y: {actual_y:.1f}"
                    )

    def test_outer_depth2_output_routes_from_validate(self, page):
        """Output edge to log_result should come from validate's output."""
        graph = make_outer()
        nodes, edges = extract_geometries(page, graph, depth=2)

        # Find edge to log_result
        for edge in edges:
            if edge.target_id == "log_result":
                src = nodes.get(edge.source_id)
                if src:
                    expected_y = src.bottom
                    actual_y = edge.start_point[1]
                    gap = abs(actual_y - expected_y)
                    assert gap == 0, (
                        f"Output edge has {gap:.1f}px gap from {edge.source_id} bottom:\n"
                        f"  Expected Y: {expected_y:.1f}\n"
                        f"  Actual Y: {actual_y:.1f}"
                    )


# =============================================================================
# Tests: Edge Position Precision (applies to all graphs)
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestEdgePositionPrecision:
    """Tests for exact pixel-level edge positioning."""

    def test_simple_edge_x_centered(self, page):
        """Edge X coordinates should be centered on nodes."""
        graph = make_simple_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        for edge in edges:
            src = nodes.get(edge.source_id)
            tgt = nodes.get(edge.target_id)
            if src and tgt:
                dx_start = abs(edge.start_point[0] - src.center_x)
                dx_end = abs(edge.end_point[0] - tgt.center_x)

                assert dx_start == 0, (
                    f"Edge start X not centered: delta={dx_start:.1f}px"
                )
                assert dx_end == 0, (
                    f"Edge end X not centered: delta={dx_end:.1f}px"
                )

    def test_simple_no_gap_at_source(self, page):
        """No gap between source bottom and edge start."""
        graph = make_simple_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        for edge in edges:
            src = nodes.get(edge.source_id)
            if src:
                gap = abs(edge.start_point[1] - src.bottom)
                assert gap == 0, f"Gap at source: {gap:.1f}px"

    def test_simple_no_gap_at_target(self, page):
        """No gap between edge end and target top."""
        graph = make_simple_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        for edge in edges:
            tgt = nodes.get(edge.target_id)
            if tgt:
                gap = abs(edge.end_point[1] - tgt.y)
                assert gap == 0, f"Gap at target: {gap:.1f}px"

    def test_workflow_expanded_no_gap_at_source(self, page):
        """No gap at source for expanded workflow edges."""
        graph = make_workflow()
        nodes, edges = extract_geometries(page, graph, depth=1)

        for edge in edges:
            src = nodes.get(edge.source_id)
            if src:
                gap = abs(edge.start_point[1] - src.bottom)
                assert gap == 0, (
                    f"Gap at source for {edge.source_id}->{edge.target_id}: {gap:.1f}px"
                )

    def test_workflow_expanded_no_gap_at_target(self, page):
        """No gap at target for expanded workflow edges."""
        graph = make_workflow()
        nodes, edges = extract_geometries(page, graph, depth=1)

        for edge in edges:
            tgt = nodes.get(edge.target_id)
            if tgt:
                gap = abs(edge.end_point[1] - tgt.y)
                assert gap == 0, (
                    f"Gap at target for {edge.source_id}->{edge.target_id}: {gap:.1f}px"
                )

    def test_outer_depth2_no_gap_at_source(self, page):
        """No gap at source for fully expanded outer edges."""
        graph = make_outer()
        nodes, edges = extract_geometries(page, graph, depth=2)

        for edge in edges:
            src = nodes.get(edge.source_id)
            if src:
                gap = abs(edge.start_point[1] - src.bottom)
                assert gap == 0, (
                    f"Gap at source for {edge.source_id}->{edge.target_id}: {gap:.1f}px"
                )

    def test_outer_depth2_no_gap_at_target(self, page):
        """No gap at target for fully expanded outer edges."""
        graph = make_outer()
        nodes, edges = extract_geometries(page, graph, depth=2)

        for edge in edges:
            tgt = nodes.get(edge.target_id)
            if tgt:
                gap = abs(edge.end_point[1] - tgt.y)
                assert gap == 0, (
                    f"Gap at target for {edge.source_id}->{edge.target_id}: {gap:.1f}px"
                )
