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
"""

import pytest
from hypergraph import Graph, node
from hypergraph.viz.geometry import (
    NodeGeometry,
    EdgeGeometry,
    EdgeConnectionValidator,
    format_issues,
)

try:
    import playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


# =============================================================================
# Test Graph Definitions
# =============================================================================

@node(output_name="a_out")
def node_a(x: int) -> int:
    return x + 1


@node(output_name="b_out")
def node_b(a_out: int) -> int:
    return a_out * 2


@node(output_name="c_out")
def node_c(b_out: int) -> int:
    return b_out + 10


def make_simple_graph() -> Graph:
    """Simple 2-node graph: a -> b."""
    return Graph(nodes=[node_a, node_b])


def make_chain_graph() -> Graph:
    """3-node chain: a -> b -> c."""
    return Graph(nodes=[node_a, node_b, node_c])


# Nested graph definitions
@node(output_name="cleaned")
def clean_text(text: str) -> str:
    return text.strip()


@node(output_name="normalized")
def normalize_text(cleaned: str) -> str:
    return cleaned.lower()


@node(output_name="result")
def analyze(normalized: str) -> dict:
    return {"length": len(normalized)}


def make_nested_graph() -> Graph:
    """1-level nested graph: preprocess -> analyze."""
    preprocess = Graph(nodes=[clean_text, normalize_text], name="preprocess")
    return Graph(nodes=[preprocess.as_node(), analyze])


# =============================================================================
# Extraction Helper
# =============================================================================

def extract_geometries(page, graph: Graph, depth: int) -> tuple[dict[str, NodeGeometry], list[EdgeGeometry]]:
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
        raw_edges = page.evaluate("window.__hypergraphVizExtractEdgePaths ? window.__hypergraphVizExtractEdgePaths() : []")
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


# =============================================================================
# Tests: Simple Graph (No Nesting)
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestEdgeConnectionsSimple:
    """Simple graph: no nesting, basic edge validation."""

    @pytest.fixture
    def page(self):
        """Create a Playwright page for testing."""
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            yield page
            browser.close()

    def test_edge_connects_to_source_bottom(self, page):
        """Edge should start exactly at source node's center-bottom."""
        graph = make_simple_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        validator = EdgeConnectionValidator(nodes, edges, tolerance=0.0)
        issues = validator.validate_all()

        assert issues == {}, f"Edge connection issues:\n{format_issues(issues)}"

    def test_edge_connects_to_target_top(self, page):
        """Edge should end exactly at target node's center-top."""
        graph = make_simple_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        # Check each edge ends at target's center-top
        for edge in edges:
            tgt = nodes.get(edge.target_id)
            if tgt:
                expected = tgt.center_top
                actual = edge.end_point
                dx = abs(actual[0] - expected[0])
                dy = abs(actual[1] - expected[1])
                assert dx == 0 and dy == 0, (
                    f"Edge {edge.source_id}->{edge.target_id} end point mismatch:\n"
                    f"  Expected: {expected}\n"
                    f"  Actual: {actual}\n"
                    f"  Delta: ({dx:.1f}, {dy:.1f})"
                )

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

    def test_chain_graph_all_edges_valid(self, page):
        """All edges in a 3-node chain should be valid."""
        graph = make_chain_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        validator = EdgeConnectionValidator(nodes, edges, tolerance=0.0)
        issues = validator.validate_all()

        assert issues == {}, f"Edge connection issues:\n{format_issues(issues)}"


# =============================================================================
# Tests: Nested Graph (Edges Crossing Container Boundaries)
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestEdgeConnectionsNested:
    """Nested graph: edges crossing container boundaries."""

    @pytest.fixture
    def page(self):
        """Create a Playwright page for testing."""
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            yield page
            browser.close()

    def test_nested_collapsed_edges_valid(self, page):
        """Edges to/from collapsed containers should have no gap."""
        graph = make_nested_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        validator = EdgeConnectionValidator(nodes, edges, tolerance=0.0)
        issues = validator.validate_all()

        assert issues == {}, f"Edge connection issues:\n{format_issues(issues)}"

    def test_nested_expanded_edges_valid(self, page):
        """Edges into/out of expanded containers should have no gap."""
        graph = make_nested_graph()
        nodes, edges = extract_geometries(page, graph, depth=1)

        validator = EdgeConnectionValidator(nodes, edges, tolerance=0.0)
        issues = validator.validate_all()

        assert issues == {}, f"Edge connection issues:\n{format_issues(issues)}"

    def test_expanded_input_edge_targets_internal_node(self, page):
        """When expanded, input edge should target the actual internal node."""
        graph = make_nested_graph()
        nodes, edges = extract_geometries(page, graph, depth=1)

        # Find edge going into clean_text (first node in preprocess)
        clean_text_node = nodes.get("clean_text")
        if clean_text_node:
            # Find edge targeting clean_text
            input_edge = None
            for edge in edges:
                if edge.target_id == "clean_text":
                    input_edge = edge
                    break

            if input_edge:
                # Edge should end at clean_text's center-top, not container's top
                expected_y = clean_text_node.y
                actual_y = input_edge.end_point[1]
                gap = abs(actual_y - expected_y)

                assert gap == 0, (
                    f"Input edge has gap from internal node:\n"
                    f"  Edge ends at Y={actual_y:.1f}\n"
                    f"  clean_text top: {expected_y:.1f}\n"
                    f"  Gap: {gap:.1f}px (should be 0)"
                )

    def test_expanded_output_edge_sources_internal_node(self, page):
        """When expanded, output edge should source from the actual internal node."""
        graph = make_nested_graph()
        nodes, edges = extract_geometries(page, graph, depth=1)

        # Find normalize_text (last node in preprocess that produces output)
        # The edge from preprocess to analyze should visually start from normalize_text
        normalize_node = nodes.get("normalize_text")
        if normalize_node:
            # Find edge from normalize_text or its data node to analyze
            output_edge = None
            for edge in edges:
                if edge.target_id == "analyze":
                    output_edge = edge
                    break

            if output_edge:
                # Edge should start near normalize_text's bottom
                expected_y = normalize_node.bottom
                actual_y = output_edge.start_point[1]
                gap = abs(actual_y - expected_y)

                # Allow some tolerance since edge may come from data node
                tolerance = 50  # Data nodes are below function nodes
                assert gap <= tolerance, (
                    f"Output edge has large gap from internal node:\n"
                    f"  Edge starts at Y={actual_y:.1f}\n"
                    f"  normalize_text bottom: {expected_y:.1f}\n"
                    f"  Gap: {gap:.1f}px (should be <= {tolerance})"
                )


# =============================================================================
# Tests: Edge Position Precision
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestEdgePositionPrecision:
    """Tests for exact pixel-level edge positioning."""

    @pytest.fixture
    def page(self):
        """Create a Playwright page for testing."""
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            yield page
            browser.close()

    def test_edge_x_centered_on_nodes(self, page):
        """Edge X coordinates should be centered on source and target nodes."""
        graph = make_simple_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        for edge in edges:
            src = nodes.get(edge.source_id)
            tgt = nodes.get(edge.target_id)
            if src and tgt:
                # Check start X is centered on source
                start_x = edge.start_point[0]
                expected_start_x = src.center_x
                dx_start = abs(start_x - expected_start_x)

                # Check end X is centered on target
                end_x = edge.end_point[0]
                expected_end_x = tgt.center_x
                dx_end = abs(end_x - expected_end_x)

                assert dx_start == 0, (
                    f"Edge start not centered on source:\n"
                    f"  Edge starts at X={start_x:.1f}\n"
                    f"  Source center: {expected_start_x:.1f}\n"
                    f"  Delta: {dx_start:.1f}px"
                )
                assert dx_end == 0, (
                    f"Edge end not centered on target:\n"
                    f"  Edge ends at X={end_x:.1f}\n"
                    f"  Target center: {expected_end_x:.1f}\n"
                    f"  Delta: {dx_end:.1f}px"
                )

    def test_no_visible_gap_at_source(self, page):
        """No visible gap should exist between source node and edge start."""
        graph = make_simple_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        for edge in edges:
            src = nodes.get(edge.source_id)
            if src:
                start_y = edge.start_point[1]
                expected_y = src.bottom
                gap = abs(start_y - expected_y)

                assert gap == 0, (
                    f"Visible gap at source of edge {edge.source_id}->{edge.target_id}:\n"
                    f"  Edge starts at Y={start_y:.1f}\n"
                    f"  Source bottom: {expected_y:.1f}\n"
                    f"  Gap: {gap:.1f}px"
                )

    def test_no_visible_gap_at_target(self, page):
        """No visible gap should exist between edge end and target node."""
        graph = make_simple_graph()
        nodes, edges = extract_geometries(page, graph, depth=0)

        for edge in edges:
            tgt = nodes.get(edge.target_id)
            if tgt:
                end_y = edge.end_point[1]
                expected_y = tgt.y
                gap = abs(end_y - expected_y)

                assert gap == 0, (
                    f"Visible gap at target of edge {edge.source_id}->{edge.target_id}:\n"
                    f"  Edge ends at Y={end_y:.1f}\n"
                    f"  Target top: {expected_y:.1f}\n"
                    f"  Gap: {gap:.1f}px"
                )
