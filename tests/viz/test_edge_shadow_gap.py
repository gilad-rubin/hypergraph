"""Tests for edge-to-shadow gap detection.

These tests verify that edges connect to VISIBLE node boundaries,
not shadow/wrapper boundaries. Existing tests pass but may be WRONG
because they compare edge positions to wrapper bounds.

This test queries the INNER DOM element (.group.rounded-lg) directly
to get true visible bounds, which should reveal 6-14px gaps if edges
are connecting to wrapper bounds instead of visible element bounds.

The core bug hypothesis: Existing tests use `window.__hypergraphVizDebug.nodes`
which may report wrapper bounds (including shadow). We need to compare
edge positions to the actual INNER element bounds (visible node boundary).
"""

import pytest
from hypergraph import Graph, node

try:
    import playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


# =============================================================================
# Test Graph Definitions (same as test_visual_layout_issues.py)
# =============================================================================

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
    """Create 2-level nested graph: outer > middle > inner."""
    inner = Graph(nodes=[step1, step2], name="inner")
    middle = Graph(nodes=[inner.as_node(), validate], name="middle")
    return Graph(nodes=[middle.as_node(), log_result])


# =============================================================================
# Helper: Extract inner element bounds (excludes shadow)
# =============================================================================

def extract_inner_bounds_and_edge_paths(page):
    """Extract INNER element bounds and edge path Y coordinates.

    Returns dict with:
    - innerBounds: {nodeId: {top, bottom, centerX}} for inner elements
    - wrapperBounds: {nodeId: {top, bottom}} for wrapper elements (for comparison)
    - shadowOffsets: {nodeId: {topOffset, bottomOffset}} difference between wrapper and inner
    - edgePaths: [{source, target, startY, endY}] from SVG paths
    - viewportTransform: {x, y, zoom} for coordinate conversion

    Note: Uses window.__hypergraphVizDebug.edges for source/target info since
    the ReactFlow data-testid format doesn't contain hyphen-separated source-target.
    """
    return page.evaluate("""() => {
        const result = {
            innerBounds: {},
            wrapperBounds: {},
            shadowOffsets: {},
            edgePaths: [],
            viewportTransform: null,
            errors: []
        };

        // Get viewport transform from ReactFlow
        const viewport = document.querySelector('.react-flow__viewport');
        if (viewport) {
            const transform = viewport.style.transform;
            const match = transform.match(/translate\\(([\\d.-]+)px,\\s*([\\d.-]+)px\\)\\s*scale\\(([\\d.-]+)\\)/);
            if (match) {
                result.viewportTransform = {
                    x: parseFloat(match[1]),
                    y: parseFloat(match[2]),
                    zoom: parseFloat(match[3])
                };
            }
        }

        // Get all node wrappers
        const nodeWrappers = document.querySelectorAll('.react-flow__node');

        for (const wrapper of nodeWrappers) {
            // Get node ID from data attribute
            const nodeId = wrapper.getAttribute('data-id');
            if (!nodeId) continue;

            // Get wrapper bounds (includes shadow area)
            const wrapperRect = wrapper.getBoundingClientRect();
            result.wrapperBounds[nodeId] = {
                top: wrapperRect.top,
                bottom: wrapperRect.bottom,
                left: wrapperRect.left,
                right: wrapperRect.right,
                centerX: (wrapperRect.left + wrapperRect.right) / 2
            };

            // Get inner element bounds (excludes shadow)
            // Look for the actual visible node element
            const innerNode = wrapper.querySelector('.group.rounded-lg') ||
                              wrapper.querySelector('.rounded-lg') ||
                              wrapper.firstElementChild;

            if (innerNode) {
                const innerRect = innerNode.getBoundingClientRect();
                result.innerBounds[nodeId] = {
                    top: innerRect.top,
                    bottom: innerRect.bottom,
                    left: innerRect.left,
                    right: innerRect.right,
                    centerX: (innerRect.left + innerRect.right) / 2
                };

                // Calculate shadow offset (how much wrapper extends beyond inner)
                result.shadowOffsets[nodeId] = {
                    topOffset: innerRect.top - wrapperRect.top,  // positive if inner is lower
                    bottomOffset: wrapperRect.bottom - innerRect.bottom  // positive if wrapper extends below
                };
            }
        }

        // Get debug edges which have proper source/target fields
        const debugEdges = window.__hypergraphVizDebug ? window.__hypergraphVizDebug.edges : [];

        // Get edge paths from SVG and match with debug edges
        const edgeGroups = document.querySelectorAll('.react-flow__edge');
        for (const group of edgeGroups) {
            const path = group.querySelector('path.react-flow__edge-path');
            if (!path) continue;

            const pathD = path.getAttribute('d');
            if (!pathD) continue;

            // Get edge ID from data-testid (format: rf__edge-{edgeId})
            const testId = group.getAttribute('data-testid') || '';

            // Parse start Y from "M x y" pattern
            const startMatch = pathD.match(/M\\s*([\\d.-]+)[,\\s]+([\\d.-]+)/);
            let startX = null, startY = null;
            if (startMatch) {
                startX = parseFloat(startMatch[1]);
                startY = parseFloat(startMatch[2]);
            }

            // Parse end Y - last coordinate pair in path
            const allCoords = pathD.match(/[\\d.-]+/g);
            let endX = null, endY = null;
            if (allCoords && allCoords.length >= 2) {
                endX = parseFloat(allCoords[allCoords.length - 2]);
                endY = parseFloat(allCoords[allCoords.length - 1]);
            }

            // Find matching debug edge by checking if testId contains the edge id
            // Debug edges have format like {id: "e_source_target", source: "source", target: "target"}
            let source = null, target = null;
            for (const debugEdge of debugEdges) {
                if (testId.includes(debugEdge.id)) {
                    // Use actualSource/actualTarget if available (for re-routed edges)
                    source = (debugEdge.data && debugEdge.data.actualSource) || debugEdge.source;
                    target = (debugEdge.data && debugEdge.data.actualTarget) || debugEdge.target;
                    break;
                }
            }

            result.edgePaths.push({
                testId: testId,
                source: source,
                target: target,
                startX: startX,
                startY: startY,
                endX: endX,
                endY: endY,
                pathD: pathD.substring(0, 100)
            });
        }

        return result;
    }""")


def convert_layout_to_screen(layout_y, viewport_transform):
    """Convert layout Y coordinate to screen Y coordinate."""
    if viewport_transform is None:
        return layout_y
    return layout_y * viewport_transform['zoom'] + viewport_transform['y']


# =============================================================================
# Test Class
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestEdgeShadowGap:
    """Tests that edges connect to VISIBLE node boundaries, not shadow boundaries.

    The hypothesis is that existing tests pass because they compare edge positions
    to wrapper bounds. If edges actually connect to wrapper bounds but we measure
    against wrapper bounds, the test passes despite a visible gap.

    This test measures the INNER element (the visible node) and compares edge
    positions to that, which should reveal the shadow gap if it exists.
    """

    def test_workflow_depth1_edge_to_visible_bounds(self):
        """Edge endpoints should touch VISIBLE node boundaries (0px gap).

        This test compares edge Y coordinates to INNER element bounds,
        not wrapper bounds. If there's a shadow gap, this test will fail
        showing the 6-14px gap.
        """
        from playwright.sync_api import sync_playwright
        from hypergraph.viz.widget import visualize
        import tempfile
        import os

        workflow = make_workflow()

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name
        visualize(workflow, depth=1, output=temp_path, _debug_overlays=True)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(f"file://{temp_path}")

                page.wait_for_function(
                    "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0",
                    timeout=10000,
                )

                # Wait a bit more for layout to fully settle
                page.wait_for_timeout(500)

                # Extract data using our custom method
                data = extract_inner_bounds_and_edge_paths(page)

                # Also get the debug data for comparison
                debug_nodes = page.evaluate("window.__hypergraphVizDebug.nodes")
                debug_edges = page.evaluate("window.__hypergraphVizDebug.edges")

                browser.close()
        finally:
            os.unlink(temp_path)

        # Build node lookup from debug data (these are layout coordinates)
        debug_node_map = {n['id']: n for n in debug_nodes}

        # Analyze the results
        issues = []
        gap_details = []

        for edge_path in data['edgePaths']:
            source_id = edge_path['source']
            target_id = edge_path['target']

            if not source_id or not target_id:
                continue

            # Get inner bounds (screen coordinates)
            source_inner = data['innerBounds'].get(source_id)
            target_inner = data['innerBounds'].get(target_id)

            # Get wrapper bounds for comparison
            source_wrapper = data['wrapperBounds'].get(source_id)
            target_wrapper = data['wrapperBounds'].get(target_id)

            # Get shadow offsets
            source_shadow = data['shadowOffsets'].get(source_id, {})
            target_shadow = data['shadowOffsets'].get(target_id, {})

            if source_inner and target_inner:
                # Edge coordinates are in layout space, need to convert to screen
                transform = data['viewportTransform']

                if transform and edge_path['startY'] is not None:
                    edge_start_screen_y = convert_layout_to_screen(edge_path['startY'], transform)
                    edge_end_screen_y = convert_layout_to_screen(edge_path['endY'], transform)

                    # Compare edge Y to INNER element bounds
                    # Edge start should be at source inner bottom
                    start_gap = abs(edge_start_screen_y - source_inner['bottom'])
                    # Edge end should be at target inner top
                    end_gap = abs(edge_end_screen_y - target_inner['top'])

                    gap_details.append({
                        'edge': f"{source_id} -> {target_id}",
                        'edge_start_y': edge_start_screen_y,
                        'edge_end_y': edge_end_screen_y,
                        'source_inner_bottom': source_inner['bottom'],
                        'target_inner_top': target_inner['top'],
                        'start_gap': start_gap,
                        'end_gap': end_gap,
                        'source_shadow_bottom_offset': source_shadow.get('bottomOffset', 0),
                        'target_shadow_top_offset': target_shadow.get('topOffset', 0),
                    })

                    # Allow small visual gaps due to varying shadow sizes
                    # shadow-lg (function nodes) = 14px, shadow-sm (data/input) = 6px
                    # With SHADOW_OFFSET=10, we get +/-4px variance
                    tolerance = 5.0
                    if start_gap > tolerance:
                        issues.append(
                            f"{source_id} -> {target_id}: "
                            f"START gap of {start_gap:.1f}px "
                            f"(edge_y={edge_start_screen_y:.1f}, inner_bottom={source_inner['bottom']:.1f}, "
                            f"shadow_offset={source_shadow.get('bottomOffset', 0):.1f})"
                        )
                    if end_gap > tolerance:
                        issues.append(
                            f"{source_id} -> {target_id}: "
                            f"END gap of {end_gap:.1f}px "
                            f"(edge_y={edge_end_screen_y:.1f}, inner_top={target_inner['top']:.1f}, "
                            f"shadow_offset={target_shadow.get('topOffset', 0):.1f})"
                        )

        # Report all gap details for debugging
        detail_lines = []
        for d in gap_details:
            detail_lines.append(
                f"  {d['edge']}:\n"
                f"    Edge start Y: {d['edge_start_y']:.1f}px, Source inner bottom: {d['source_inner_bottom']:.1f}px, "
                f"Gap: {d['start_gap']:.1f}px (shadow offset: {d['source_shadow_bottom_offset']:.1f}px)\n"
                f"    Edge end Y: {d['edge_end_y']:.1f}px, Target inner top: {d['target_inner_top']:.1f}px, "
                f"Gap: {d['end_gap']:.1f}px (shadow offset: {d['target_shadow_top_offset']:.1f}px)"
            )

        assert len(issues) == 0, (
            f"Found {len(issues)} edge-to-visible-boundary gaps!\n"
            f"This indicates edges connect to shadow/wrapper bounds, not visible element bounds.\n\n"
            f"Issues:\n" + "\n".join(f"  - {issue}" for issue in issues) + "\n\n"
            f"Gap Details:\n" + "\n".join(detail_lines)
        )

    def test_outer_depth2_edge_to_visible_bounds(self):
        """Edge endpoints should touch VISIBLE node boundaries in nested graph.

        Tests the 2-level nested graph at full expansion depth.
        """
        from playwright.sync_api import sync_playwright
        from hypergraph.viz.widget import visualize
        import tempfile
        import os

        outer = make_outer()

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name
        visualize(outer, depth=2, output=temp_path, _debug_overlays=True)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(f"file://{temp_path}")

                page.wait_for_function(
                    "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0",
                    timeout=10000,
                )

                page.wait_for_timeout(500)

                data = extract_inner_bounds_and_edge_paths(page)

                browser.close()
        finally:
            os.unlink(temp_path)

        issues = []
        gap_details = []

        for edge_path in data['edgePaths']:
            source_id = edge_path['source']
            target_id = edge_path['target']

            if not source_id or not target_id:
                continue

            source_inner = data['innerBounds'].get(source_id)
            target_inner = data['innerBounds'].get(target_id)
            source_shadow = data['shadowOffsets'].get(source_id, {})
            target_shadow = data['shadowOffsets'].get(target_id, {})

            if source_inner and target_inner:
                transform = data['viewportTransform']

                if transform and edge_path['startY'] is not None:
                    edge_start_screen_y = convert_layout_to_screen(edge_path['startY'], transform)
                    edge_end_screen_y = convert_layout_to_screen(edge_path['endY'], transform)

                    start_gap = abs(edge_start_screen_y - source_inner['bottom'])
                    end_gap = abs(edge_end_screen_y - target_inner['top'])

                    gap_details.append({
                        'edge': f"{source_id} -> {target_id}",
                        'start_gap': start_gap,
                        'end_gap': end_gap,
                        'source_shadow_offset': source_shadow.get('bottomOffset', 0),
                        'target_shadow_offset': target_shadow.get('topOffset', 0),
                    })

                    tolerance = 5.0  # Allow variance due to different shadow sizes
                    if start_gap > tolerance:
                        issues.append(
                            f"{source_id} -> {target_id}: START gap of {start_gap:.1f}px"
                        )
                    if end_gap > tolerance:
                        issues.append(
                            f"{source_id} -> {target_id}: END gap of {end_gap:.1f}px"
                        )

        detail_lines = [
            f"  {d['edge']}: start_gap={d['start_gap']:.1f}px, end_gap={d['end_gap']:.1f}px "
            f"(shadow offsets: src={d['source_shadow_offset']:.1f}, tgt={d['target_shadow_offset']:.1f})"
            for d in gap_details
        ]

        assert len(issues) == 0, (
            f"Found {len(issues)} edge-to-visible-boundary gaps in outer depth=2!\n\n"
            f"Issues:\n" + "\n".join(f"  - {issue}" for issue in issues) + "\n\n"
            f"Gap Details:\n" + "\n".join(detail_lines)
        )

    def test_shadow_offset_detection(self):
        """Verify we can detect shadow offsets between wrapper and inner elements.

        This is a diagnostic test to confirm the test methodology works.
        If shadow offsets are non-zero, it confirms nodes have shadows that
        extend beyond the visible element.
        """
        from playwright.sync_api import sync_playwright
        from hypergraph.viz.widget import visualize
        import tempfile
        import os

        workflow = make_workflow()

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name
        visualize(workflow, depth=1, output=temp_path, _debug_overlays=True)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(f"file://{temp_path}")

                page.wait_for_function(
                    "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0",
                    timeout=10000,
                )

                page.wait_for_timeout(500)

                data = extract_inner_bounds_and_edge_paths(page)

                browser.close()
        finally:
            os.unlink(temp_path)

        # Report shadow offsets for all nodes
        shadow_info = []
        has_shadow = False

        for node_id, offsets in data['shadowOffsets'].items():
            top_offset = offsets.get('topOffset', 0)
            bottom_offset = offsets.get('bottomOffset', 0)
            shadow_info.append(
                f"  {node_id}: top_offset={top_offset:.1f}px, bottom_offset={bottom_offset:.1f}px"
            )
            if abs(top_offset) > 0.5 or abs(bottom_offset) > 0.5:
                has_shadow = True

        # This test documents the shadow situation - it passes either way
        # but the output helps understand the node structure
        print("\n\nShadow Offset Analysis:")
        print("\n".join(shadow_info))
        print(f"\nHas significant shadow offsets: {has_shadow}")

        # If there are no shadow offsets, the other tests should pass
        # If there are shadow offsets, the other tests reveal whether edges
        # connect to wrapper bounds (bug) or inner bounds (correct)
