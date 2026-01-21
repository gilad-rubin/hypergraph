"""Geometric verification tests for edge routing.

These tests verify that edges don't cross nodes using Shapely for geometric
intersection detection. Tests require Playwright browsers to be installed.
"""

from typing import Any

import pytest

# Import optional dependencies with graceful fallback
try:
    from playwright.sync_api import Page
    from shapely.geometry import LineString, Polygon

    PLAYWRIGHT_AVAILABLE = True
    SHAPELY_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    SHAPELY_AVAILABLE = False

# Skip all tests if dependencies unavailable
pytestmark = [
    pytest.mark.skipif(
        not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed or browsers missing"
    ),
    pytest.mark.skipif(not SHAPELY_AVAILABLE, reason="Shapely not installed"),
    pytest.mark.slow,
]


def extract_coordinates_from_page(page: Page) -> dict[str, Any]:
    """Extract node bounding boxes and edge paths from rendered graph.

    Uses JavaScript DOM APIs to get actual rendered positions and paths.

    Args:
        page: Playwright page with rendered graph

    Returns:
        Dict with:
        - nodes: List of {id, bbox: {x, y, width, height}}
        - edges: List of {id, path: [(x, y), ...]}
    """
    js_code = """
    () => {
        const nodes = [];
        const edges = [];

        // Extract node bounding boxes
        document.querySelectorAll('.react-flow__node').forEach(nodeEl => {
            const id = nodeEl.getAttribute('data-id');
            const rect = nodeEl.getBoundingClientRect();
            nodes.push({
                id: id,
                bbox: {
                    x: rect.x,
                    y: rect.y,
                    width: rect.width,
                    height: rect.height
                }
            });
        });

        // Extract edge paths
        document.querySelectorAll('.react-flow__edge').forEach(edgeEl => {
            // Get edge ID from data-testid (format: rf__edge-{id})
            const testId = edgeEl.getAttribute('data-testid') || '';
            const idMatch = testId.match(/rf__edge-(.+)/);
            const id = idMatch ? idMatch[1] : 'unknown';

            const pathEl = edgeEl.querySelector('path');
            if (!pathEl) return;

            // Parse SVG path to points
            const d = pathEl.getAttribute('d');
            const points = [];

            // Simple path parser for M/L/C commands
            const commands = d.match(/[MLCmlc][^MLCmlc]*/g) || [];
            let currentX = 0, currentY = 0;

            commands.forEach(cmd => {
                const type = cmd[0];
                const coords = cmd.slice(1).trim().split(/[,\\s]+/).map(parseFloat);

                if (type === 'M' || type === 'm') {
                    currentX = type === 'M' ? coords[0] : currentX + coords[0];
                    currentY = type === 'M' ? coords[1] : currentY + coords[1];
                    points.push([currentX, currentY]);
                } else if (type === 'L' || type === 'l') {
                    for (let i = 0; i < coords.length; i += 2) {
                        currentX = type === 'L' ? coords[i] : currentX + coords[i];
                        currentY = type === 'L' ? coords[i+1] : currentY + coords[i+1];
                        points.push([currentX, currentY]);
                    }
                } else if (type === 'C' || type === 'c') {
                    // For cubic bezier, sample the curve
                    for (let i = 0; i < coords.length; i += 6) {
                        const x1 = type === 'C' ? coords[i] : currentX + coords[i];
                        const y1 = type === 'C' ? coords[i+1] : currentY + coords[i+1];
                        const x2 = type === 'C' ? coords[i+2] : currentX + coords[i+2];
                        const y2 = type === 'C' ? coords[i+3] : currentY + coords[i+3];
                        const x3 = type === 'C' ? coords[i+4] : currentX + coords[i+4];
                        const y3 = type === 'C' ? coords[i+5] : currentY + coords[i+5];

                        // Sample curve at intervals
                        for (let t = 0.1; t <= 1; t += 0.1) {
                            const mt = 1 - t;
                            const x = mt*mt*mt*currentX + 3*mt*mt*t*x1 + 3*mt*t*t*x2 + t*t*t*x3;
                            const y = mt*mt*mt*currentY + 3*mt*mt*t*y1 + 3*mt*t*t*y2 + t*t*t*y3;
                            points.push([x, y]);
                        }

                        currentX = x3;
                        currentY = y3;
                    }
                }
            });

            if (points.length > 0) {
                edges.push({id: id, path: points});
            }
        });

        return {nodes, edges};
    }
    """
    return page.evaluate(js_code)


def verify_no_edge_node_intersections(coords: dict[str, Any]) -> dict[str, Any]:
    """Verify that edges don't cross nodes using Shapely geometric checks.

    Args:
        coords: Output from extract_coordinates_from_page

    Returns:
        Dict with:
        - passed: bool - whether verification passed
        - intersections: list of {edge_id, node_id, intersection_type}
    """
    nodes = coords["nodes"]
    edges = coords["edges"]
    intersections = []

    # Create Shapely geometries for nodes
    node_polygons = {}
    for node in nodes:
        bbox = node["bbox"]
        # Create polygon from bounding box
        poly = Polygon(
            [
                (bbox["x"], bbox["y"]),
                (bbox["x"] + bbox["width"], bbox["y"]),
                (bbox["x"] + bbox["width"], bbox["y"] + bbox["height"]),
                (bbox["x"], bbox["y"] + bbox["height"]),
            ]
        )
        node_polygons[node["id"]] = poly

    # Check each edge against each node
    for edge in edges:
        if len(edge["path"]) < 2:
            continue

        # Create LineString from edge path
        edge_line = LineString(edge["path"])

        for node_id, node_poly in node_polygons.items():
            # Check if edge crosses node (excluding touches at endpoints)
            if edge_line.crosses(node_poly):
                intersections.append(
                    {
                        "edge_id": edge["id"],
                        "node_id": node_id,
                        "intersection_type": "crosses",
                    }
                )
            elif edge_line.intersects(node_poly):
                # Check if it's more than just endpoint touching
                intersection = edge_line.intersection(node_poly)
                # If intersection is more than a point, it's problematic
                if not intersection.is_empty and intersection.geom_type != "Point":
                    intersections.append(
                        {
                            "edge_id": edge["id"],
                            "node_id": node_id,
                            "intersection_type": "overlaps",
                        }
                    )

    return {"passed": len(intersections) == 0, "intersections": intersections}


# =============================================================================
# Test Functions
# =============================================================================


def test_complex_rag_no_edge_node_intersections(page, page_with_graph, complex_rag_graph):
    """Test that complex RAG pipeline has no edge-node intersections."""
    # Load the graph
    page_with_graph(page, complex_rag_graph, depth=1)

    # Extract coordinates
    coords = extract_coordinates_from_page(page)

    # Verify no intersections
    result = verify_no_edge_node_intersections(coords)

    # Assert
    if not result["passed"]:
        intersections_summary = "\n".join(
            f"  - Edge {i['edge_id']} {i['intersection_type']} node {i['node_id']}"
            for i in result["intersections"]
        )
        pytest.fail(f"Found edge-node intersections:\n{intersections_summary}")


def test_nested_collapsed_no_edge_node_intersections(page, page_with_graph, nested_graph):
    """Test that nested graph (collapsed) has no edge-node intersections."""
    page_with_graph(page, nested_graph, depth=0)
    coords = extract_coordinates_from_page(page)
    result = verify_no_edge_node_intersections(coords)

    if not result["passed"]:
        intersections_summary = "\n".join(
            f"  - Edge {i['edge_id']} {i['intersection_type']} node {i['node_id']}"
            for i in result["intersections"]
        )
        pytest.fail(f"Found edge-node intersections:\n{intersections_summary}")


def test_nested_expanded_no_edge_node_intersections(page, page_with_graph, nested_graph):
    """Test that nested graph (expanded) has no edge-node intersections."""
    page_with_graph(page, nested_graph, depth=1)
    coords = extract_coordinates_from_page(page)
    result = verify_no_edge_node_intersections(coords)

    if not result["passed"]:
        intersections_summary = "\n".join(
            f"  - Edge {i['edge_id']} {i['intersection_type']} node {i['node_id']}"
            for i in result["intersections"]
        )
        pytest.fail(f"Found edge-node intersections:\n{intersections_summary}")


def test_double_nested_no_edge_node_intersections(
    page, page_with_graph, double_nested_graph
):
    """Test that double nested graph has no edge-node intersections."""
    page_with_graph(page, double_nested_graph, depth=2)
    coords = extract_coordinates_from_page(page)
    result = verify_no_edge_node_intersections(coords)

    if not result["passed"]:
        intersections_summary = "\n".join(
            f"  - Edge {i['edge_id']} {i['intersection_type']} node {i['node_id']}"
            for i in result["intersections"]
        )
        pytest.fail(f"Found edge-node intersections:\n{intersections_summary}")
