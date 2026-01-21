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
        - nodes: List of {id, bbox: {x, y, width, height}, is_expanded_container: bool}
        - edges: List of {id, path: [(x, y), ...]}
    """
    js_code = """
    () => {
        const nodes = [];
        const edges = [];

        // Get viewport transform to convert SVG coords to DOM coords
        const viewport = document.querySelector('.react-flow__viewport');
        let offsetX = 0, offsetY = 0, scale = 1;
        if (viewport) {
            const transform = viewport.style.transform;
            const translateMatch = transform.match(/translate\\(([\\d.-]+)px,\\s*([\\d.-]+)px\\)/);
            const scaleMatch = transform.match(/scale\\(([\\d.-]+)\\)/);
            if (translateMatch) {
                offsetX = parseFloat(translateMatch[1]);
                offsetY = parseFloat(translateMatch[2]);
            }
            if (scaleMatch) {
                scale = parseFloat(scaleMatch[1]);
            }
        }

        // Extract node bounding boxes
        document.querySelectorAll('.react-flow__node').forEach(nodeEl => {
            const id = nodeEl.getAttribute('data-id');
            const rect = nodeEl.getBoundingClientRect();

            // Check if this is an expanded container (PIPELINE node with children)
            // Expanded containers have child nodes rendered inside them
            const isExpandedContainer = nodeEl.classList.contains('react-flow__node-group') ||
                nodeEl.querySelector('.react-flow__node') !== null ||
                (nodeEl.style.width && parseInt(nodeEl.style.width) > 200);  // Large nodes are likely containers

            nodes.push({
                id: id,
                bbox: {
                    x: rect.x,
                    y: rect.y,
                    width: rect.width,
                    height: rect.height
                },
                is_expanded_container: isExpandedContainer
            });
        });

        // Extract edge paths (converting SVG coords to DOM coords)
        document.querySelectorAll('.react-flow__edge').forEach(edgeEl => {
            // Get edge ID from data-testid (format: rf__edge-{id})
            const testId = edgeEl.getAttribute('data-testid') || '';
            const idMatch = testId.match(/rf__edge-(.+)/);
            const id = idMatch ? idMatch[1] : 'unknown';

            // Get source and target from aria attributes or data attributes
            const source = edgeEl.getAttribute('data-source') ||
                          (edgeEl.getAttribute('aria-label') || '').match(/from (\\S+) to/)?.[1] || null;
            const target = edgeEl.getAttribute('data-target') ||
                          (edgeEl.getAttribute('aria-label') || '').match(/to (\\S+)/)?.[1] || null;

            const pathEl = edgeEl.querySelector('path');
            if (!pathEl) return;

            // Parse SVG path to points
            const d = pathEl.getAttribute('d');
            const points = [];

            // Helper to transform SVG coord to DOM coord
            const toDOM = (x, y) => [x * scale + offsetX, y * scale + offsetY];

            // Simple path parser for M/L/C commands
            const commands = d.match(/[MLCmlc][^MLCmlc]*/g) || [];
            let currentX = 0, currentY = 0;

            commands.forEach(cmd => {
                const type = cmd[0];
                const coords = cmd.slice(1).trim().split(/[,\\s]+/).map(parseFloat);

                if (type === 'M' || type === 'm') {
                    currentX = type === 'M' ? coords[0] : currentX + coords[0];
                    currentY = type === 'M' ? coords[1] : currentY + coords[1];
                    points.push(toDOM(currentX, currentY));
                } else if (type === 'L' || type === 'l') {
                    for (let i = 0; i < coords.length; i += 2) {
                        currentX = type === 'L' ? coords[i] : currentX + coords[i];
                        currentY = type === 'L' ? coords[i+1] : currentY + coords[i+1];
                        points.push(toDOM(currentX, currentY));
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
                            points.push(toDOM(x, y));
                        }

                        currentX = x3;
                        currentY = y3;
                    }
                }
            });

            if (points.length > 0) {
                edges.push({id: id, path: points, source: source, target: target});
            }
        });

        return {nodes, edges};
    }
    """
    return page.evaluate(js_code)


def verify_no_edge_node_intersections(coords: dict[str, Any]) -> dict[str, Any]:
    """Verify that edges don't cross nodes using Shapely geometric checks.

    Expanded containers are excluded from intersection checks because edges
    connecting to internal nodes necessarily cross container boundaries.

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

    # Create Shapely geometries for nodes (excluding expanded containers)
    node_polygons = {}
    for node in nodes:
        # Skip expanded containers - edges to internal nodes cross container bounds
        if node.get("is_expanded_container", False):
            continue

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

        # Get source and target - prefer DOM attributes, fall back to ID parsing
        source_node = edge.get("source")
        target_node = edge.get("target")

        # If not available from DOM, try parsing from edge ID
        if not source_node or not target_node:
            edge_id = edge["id"]
            # Try _to_ format first (e.g., e___inputs_0___to_middle)
            if "_to_" in edge_id:
                parts = edge_id.split("_to_")
                if len(parts) == 2:
                    source_node = source_node or (
                        parts[0][2:] if parts[0].startswith("e_") else parts[0]
                    )
                    target_node = target_node or parts[1]
            # Otherwise, try to match known node IDs in the edge ID
            elif not source_node or not target_node:
                clean_id = edge_id[2:] if edge_id.startswith("e_") else edge_id
                # Sort by length (longest first) to avoid substring issues
                all_node_ids = sorted(node_polygons.keys(), key=len, reverse=True)
                for src_id in all_node_ids:
                    if clean_id.startswith(src_id + "_"):
                        remainder = clean_id[len(src_id) + 1:]
                        for tgt_id in all_node_ids:
                            if remainder == tgt_id or remainder.endswith(tgt_id):
                                source_node = source_node or src_id
                                target_node = target_node or tgt_id
                                break
                        if source_node and target_node:
                            break

        # Create LineString from edge path
        edge_line = LineString(edge["path"])

        for node_id, node_poly in node_polygons.items():
            # Skip source and target nodes - edges legitimately start/end inside them
            if node_id == source_node or node_id == target_node:
                continue

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
