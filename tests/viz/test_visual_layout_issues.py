"""Tests for visual layout issues.

These tests verify:
1. Input nodes are positioned ABOVE their target nodes (edges flow downward)
2. No visible gaps between edges and nodes
3. Edges connect to actual nodes, not container boundaries
4. Multiple INPUT nodes feeding the same target are side-by-side (horizontal spread)
"""

import pytest

# Import shared fixtures and helpers from conftest
from tests.viz.conftest import (
    HAS_PLAYWRIGHT,
    make_workflow,
    make_outer,
)


# =============================================================================
# Test: Input nodes should be ABOVE their targets (edges flow downward)
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestInputNodePosition:
    """Tests that input nodes are positioned above their target nodes."""

    def test_outer_depth2_input_above_step1(self):
        """Input x should be positioned ABOVE step1, not below.

        The edge from input_x to step1 should flow DOWNWARD.
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
                    "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0 && window.__hypergraphVizReady === true",
                    timeout=10000,
                )

                result = page.evaluate("""() => {
                    const debug = window.__hypergraphVizDebug;

                    // Find input node and step1 node
                    const inputNode = debug.nodes.find(n =>
                        n.id.includes('input') || n.id === '__inputs__'
                    );
                    const step1Node = debug.nodes.find(n => n.id === 'step1');

                    if (!inputNode || !step1Node) {
                        return {
                            error: 'Nodes not found',
                            inputNode: inputNode ? inputNode.id : null,
                            step1Node: step1Node ? step1Node.id : null,
                            allNodes: debug.nodes.map(n => n.id)
                        };
                    }

                    // Input should be ABOVE step1 (smaller Y)
                    const inputBottom = inputNode.y + inputNode.height;
                    const step1Top = step1Node.y;

                    return {
                        inputId: inputNode.id,
                        inputY: inputNode.y,
                        inputBottom: inputBottom,
                        step1Y: step1Node.y,
                        step1Top: step1Top,
                        inputAboveStep1: inputBottom < step1Top,
                        verticalDistance: step1Top - inputBottom,
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        if "error" in result:
            pytest.fail(f"Setup error: {result}")

        assert result["inputAboveStep1"], (
            f"Input node should be ABOVE step1 (edges flow downward)!\n"
            f"Input '{result['inputId']}' bottom: {result['inputBottom']}px\n"
            f"step1 top: {result['step1Top']}px\n"
            f"Vertical distance: {result['verticalDistance']}px\n"
            f"Expected: input.bottom < step1.top (positive distance)\n"
            f"Actual: Input is BELOW step1 (edge flows upward)"
        )

    def test_workflow_depth1_input_above_clean_text(self):
        """Input text should be positioned ABOVE clean_text."""
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
                    "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0 && window.__hypergraphVizReady === true",
                    timeout=10000,
                )

                result = page.evaluate("""() => {
                    const debug = window.__hypergraphVizDebug;

                    const inputNode = debug.nodes.find(n =>
                        n.id.includes('input') || n.id === '__inputs__'
                    );
                    const cleanTextNode = debug.nodes.find(n => n.id === 'clean_text');

                    if (!inputNode || !cleanTextNode) {
                        return {
                            error: 'Nodes not found',
                            allNodes: debug.nodes.map(n => n.id)
                        };
                    }

                    const inputBottom = inputNode.y + inputNode.height;
                    const cleanTextTop = cleanTextNode.y;

                    return {
                        inputId: inputNode.id,
                        inputBottom: inputBottom,
                        cleanTextTop: cleanTextTop,
                        inputAboveCleanText: inputBottom < cleanTextTop,
                        verticalDistance: cleanTextTop - inputBottom,
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        if "error" in result:
            pytest.fail(f"Setup error: {result}")

        assert result["inputAboveCleanText"], (
            f"Input should be ABOVE clean_text!\n"
            f"Input bottom: {result['inputBottom']}px\n"
            f"clean_text top: {result['cleanTextTop']}px\n"
            f"Vertical distance: {result['verticalDistance']}px"
        )


# =============================================================================
# Test: No visible gaps between edges and nodes
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestEdgeGaps:
    """Tests that edges connect to nodes without visible gaps."""

    def test_outer_depth2_input_edge_no_gap(self):
        """Edge from input to step1 should have no gap at start or end."""
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
                    "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0 && window.__hypergraphVizReady === true",
                    timeout=10000,
                )

                result = page.evaluate("""() => {
                    const debug = window.__hypergraphVizDebug;

                    // Find the input edge
                    const inputEdge = debug.edges.find(e =>
                        e.source.includes('input') || e.source === '__inputs__'
                    );

                    if (!inputEdge) {
                        return { error: 'Input edge not found', edges: debug.edges };
                    }

                    // Find source and target nodes
                    const srcNode = debug.nodes.find(n => n.id === inputEdge.source);
                    const tgtNode = debug.nodes.find(n => n.id === inputEdge.target);

                    if (!srcNode || !tgtNode) {
                        return {
                            error: 'Source or target not found',
                            source: inputEdge.source,
                            target: inputEdge.target,
                            nodes: debug.nodes.map(n => n.id)
                        };
                    }

                    // Find the SVG path for this edge
                    const edgeGroups = document.querySelectorAll('.react-flow__edge');
                    let pathStartY = null;
                    let pathEndY = null;
                    let pathD = null;

                    for (const group of edgeGroups) {
                        const id = group.getAttribute('data-testid') || '';
                        if (id.includes(inputEdge.source)) {
                            const path = group.querySelector('path');
                            if (path) {
                                pathD = path.getAttribute('d');
                                // Parse start Y (M x y)
                                const startMatch = pathD.match(/M\\s*[\\d.]+\\s+([\\d.]+)/);
                                if (startMatch) pathStartY = parseFloat(startMatch[1]);
                                // Parse end Y (last two numbers)
                                const coords = pathD.match(/[\\d.]+/g);
                                if (coords && coords.length >= 2) {
                                    pathEndY = parseFloat(coords[coords.length - 1]);
                                }
                                break;
                            }
                        }
                    }

                    const srcBottom = srcNode.y + srcNode.height;
                    const tgtTop = tgtNode.y;

                    return {
                        srcId: srcNode.id,
                        tgtId: tgtNode.id,
                        srcBottom: srcBottom,
                        tgtTop: tgtTop,
                        pathStartY: pathStartY,
                        pathEndY: pathEndY,
                        startGap: pathStartY ? Math.abs(pathStartY - srcBottom) : null,
                        endGap: pathEndY ? Math.abs(pathEndY - tgtTop) : null,
                        pathD: pathD ? pathD.substring(0, 100) : null,
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        if "error" in result:
            pytest.fail(f"Setup error: {result}")

        max_gap = 10  # pixels

        if result["startGap"] is not None:
            assert result["startGap"] <= max_gap, (
                f"Edge has gap at START!\n"
                f"Source '{result['srcId']}' bottom: {result['srcBottom']}px\n"
                f"Path starts at Y: {result['pathStartY']}px\n"
                f"Gap: {result['startGap']}px (max allowed: {max_gap}px)"
            )

        if result["endGap"] is not None:
            assert result["endGap"] <= max_gap, (
                f"Edge has gap at END!\n"
                f"Target '{result['tgtId']}' top: {result['tgtTop']}px\n"
                f"Path ends at Y: {result['pathEndY']}px\n"
                f"Gap: {result['endGap']}px (max allowed: {max_gap}px)"
            )


    def test_workflow_depth1_all_edges_no_gap(self):
        """All edges in workflow should have no visible gaps.

        This tests the screenshot issue where:
        - 7px gap below 'text' input node
        - 19px gap above 'analyze' node
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
                    "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0 && window.__hypergraphVizReady === true",
                    timeout=10000,
                )

                result = page.evaluate("""() => {
                    const debug = window.__hypergraphVizDebug;
                    const gaps = [];

                    // Check each edge for gaps
                    for (const edge of debug.edges) {
                        // Use actualSource/actualTarget when available (re-routed edges)
                        const actualSrcId = (edge.data && edge.data.actualSource) || edge.source;
                        const actualTgtId = (edge.data && edge.data.actualTarget) || edge.target;

                        const srcNode = debug.nodes.find(n => n.id === actualSrcId);
                        const tgtNode = debug.nodes.find(n => n.id === actualTgtId);

                        if (!srcNode || !tgtNode) continue;

                        // Find the SVG path for this edge
                        const edgeGroups = document.querySelectorAll('.react-flow__edge');
                        let pathStartY = null;
                        let pathEndY = null;
                        let pathD = null;

                        for (const group of edgeGroups) {
                            const id = group.getAttribute('data-testid') || '';
                            // Edge IDs typically contain source or source-target pattern
                            if (id.includes(edge.source) && id.includes(edge.target)) {
                                const path = group.querySelector('path');
                                if (path) {
                                    pathD = path.getAttribute('d');
                                    break;
                                }
                            }
                        }

                        // Try alternate search if not found
                        if (!pathD) {
                            for (const group of edgeGroups) {
                                const id = group.getAttribute('data-testid') || '';
                                if (id.includes(edge.source)) {
                                    const path = group.querySelector('path');
                                    if (path) {
                                        pathD = path.getAttribute('d');
                                        break;
                                    }
                                }
                            }
                        }

                        if (pathD) {
                            // Parse start Y (M x y)
                            const startMatch = pathD.match(/M\\s*([\\d.]+)\\s+([\\d.]+)/);
                            if (startMatch) {
                                pathStartY = parseFloat(startMatch[2]);
                            }
                            // Parse end Y (last two numbers)
                            const coords = pathD.match(/[\\d.]+/g);
                            if (coords && coords.length >= 2) {
                                pathEndY = parseFloat(coords[coords.length - 1]);
                            }
                        }

                        const srcBottom = srcNode.y + srcNode.height;
                        const tgtTop = tgtNode.y;

                        const startGap = pathStartY !== null ? Math.abs(pathStartY - srcBottom) : null;
                        const endGap = pathEndY !== null ? Math.abs(pathEndY - tgtTop) : null;

                        gaps.push({
                            edge: edge.source + ' -> ' + edge.target,
                            actualEdge: actualSrcId + ' -> ' + actualTgtId,
                            srcBottom: srcBottom,
                            tgtTop: tgtTop,
                            pathStartY: pathStartY,
                            pathEndY: pathEndY,
                            startGap: startGap,
                            endGap: endGap,
                            pathD: pathD ? pathD.substring(0, 80) : null,
                        });
                    }

                    return { gaps: gaps };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        max_gap = 5  # pixels - strict threshold for visible gaps
        issues = []

        for gap_info in result["gaps"]:
            if gap_info["startGap"] is not None and gap_info["startGap"] > max_gap:
                issues.append(
                    f"{gap_info['edge']}: START gap of {gap_info['startGap']:.1f}px "
                    f"(src.bottom={gap_info['srcBottom']:.1f}, path.start={gap_info['pathStartY']:.1f})"
                )
            if gap_info["endGap"] is not None and gap_info["endGap"] > max_gap:
                issues.append(
                    f"{gap_info['edge']}: END gap of {gap_info['endGap']:.1f}px "
                    f"(tgt.top={gap_info['tgtTop']:.1f}, path.end={gap_info['pathEndY']:.1f})"
                )

        assert len(issues) == 0, (
            f"Found {len(issues)} edge gaps (max allowed: {max_gap}px):\n" +
            "\n".join(f"  - {issue}" for issue in issues)
        )


# =============================================================================
# Test: Edges connect to actual nodes, not container boundaries
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestEdgeConnectsToActualNode:
    """Tests that edges connect to actual internal nodes, not container boundaries."""

    def test_outer_depth2_edge_to_step1_not_inner(self):
        """Edge should connect to step1's position, not inner container's boundary."""
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
                    "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0 && window.__hypergraphVizReady === true",
                    timeout=10000,
                )

                result = page.evaluate("""() => {
                    const debug = window.__hypergraphVizDebug;

                    // Find nodes
                    const innerNode = debug.nodes.find(n => n.id === 'inner');
                    const step1Node = debug.nodes.find(n => n.id === 'step1');

                    if (!step1Node) {
                        return {
                            error: 'step1 not found',
                            nodes: debug.nodes.map(n => n.id)
                        };
                    }

                    // Find the input edge
                    const inputEdge = debug.edges.find(e =>
                        e.source.includes('input') || e.source === '__inputs__'
                    );

                    if (!inputEdge) {
                        return { error: 'Input edge not found' };
                    }

                    // Find the SVG path end Y
                    const edgeGroups = document.querySelectorAll('.react-flow__edge');
                    let pathEndY = null;
                    let pathEndX = null;

                    for (const group of edgeGroups) {
                        const id = group.getAttribute('data-testid') || '';
                        if (id.includes(inputEdge.source)) {
                            const path = group.querySelector('path');
                            if (path) {
                                const pathD = path.getAttribute('d');
                                const coords = pathD.match(/[\\d.]+/g);
                                if (coords && coords.length >= 2) {
                                    pathEndX = parseFloat(coords[coords.length - 2]);
                                    pathEndY = parseFloat(coords[coords.length - 1]);
                                }
                                break;
                            }
                        }
                    }

                    // Calculate distances to inner container vs step1
                    const step1CenterX = step1Node.x + step1Node.width / 2;
                    const step1Top = step1Node.y;

                    const innerTop = innerNode ? innerNode.y : null;
                    const innerCenterX = innerNode ? innerNode.x + innerNode.width / 2 : null;

                    return {
                        pathEndX: pathEndX,
                        pathEndY: pathEndY,
                        step1Top: step1Top,
                        step1CenterX: step1CenterX,
                        innerTop: innerTop,
                        innerCenterX: innerCenterX,
                        distToStep1Y: pathEndY ? Math.abs(pathEndY - step1Top) : null,
                        distToInnerY: innerTop && pathEndY ? Math.abs(pathEndY - innerTop) : null,
                        distToStep1X: pathEndX ? Math.abs(pathEndX - step1CenterX) : null,
                        distToInnerX: innerCenterX && pathEndX ? Math.abs(pathEndX - innerCenterX) : null,
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        if "error" in result:
            pytest.fail(f"Setup error: {result}")

        # Edge should end closer to step1 than to inner container
        tolerance = 20  # pixels

        dist_to_step1 = result["distToStep1Y"]
        dist_to_inner = result["distToInnerY"]

        if dist_to_step1 is not None and dist_to_inner is not None:
            assert dist_to_step1 < dist_to_inner or dist_to_step1 <= tolerance, (
                f"Edge connects to container boundary, not actual node!\n"
                f"Path ends at Y: {result['pathEndY']}px\n"
                f"step1 top: {result['step1Top']}px (distance: {dist_to_step1}px)\n"
                f"inner top: {result['innerTop']}px (distance: {dist_to_inner}px)\n"
                f"Edge should end closer to step1 than to inner container"
            )


# =============================================================================
# Test: Comprehensive edge validation
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestEdgeValidation:
    """Comprehensive tests for edge validation at all depths."""

    def test_outer_depth2_all_edges_flow_downward(self):
        """All edges should flow downward (source.bottom < target.top)."""
        from hypergraph.viz import extract_debug_data

        outer = make_outer()
        data = extract_debug_data(outer, depth=2)

        # Check all edges have positive vertical distance
        issues = []
        for edge in data.edges:
            if edge.vert_dist is not None and edge.vert_dist < 0:
                issues.append(
                    f"{edge.source} -> {edge.target}: "
                    f"flows upward ({edge.vert_dist}px)"
                )

        assert len(issues) == 0, (
            f"Edges flow upward instead of downward:\n" +
            "\n".join(f"  - {issue}" for issue in issues)
        )

    def test_outer_depth2_no_edge_issues(self):
        """Should have zero edge issues at depth=2."""
        from hypergraph.viz import extract_debug_data

        outer = make_outer()
        data = extract_debug_data(outer, depth=2)

        assert data.summary["edgeIssues"] == 0, (
            f"Expected 0 edge issues, found {data.summary['edgeIssues']}:\n" +
            "\n".join(
                f"  - {e.source} -> {e.target}: {e.issue}"
                for e in data.edge_issues
            )
        )

    def test_workflow_depth1_no_edge_issues(self):
        """Should have zero edge issues at depth=1."""
        from hypergraph.viz import extract_debug_data

        workflow = make_workflow()
        data = extract_debug_data(workflow, depth=1)

        assert data.summary["edgeIssues"] == 0, (
            f"Expected 0 edge issues, found {data.summary['edgeIssues']}:\n" +
            "\n".join(
                f"  - {e.source} -> {e.target}: {e.issue}"
                for e in data.edge_issues
            )
        )


# =============================================================================
# Test: Multiple INPUT nodes feeding the same target should be side-by-side
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestInputNodeHorizontalSpread:
    """Tests that multiple INPUT nodes feeding the same target are spread horizontally."""

    def test_multiple_inputs_same_target_horizontal_spread(self):
        """When two INPUT nodes feed the same target, they should be side-by-side.

        This tests the bug where system_prompt and max_tokens in complex_rag
        were stacking vertically instead of being positioned horizontally.
        """
        from playwright.sync_api import sync_playwright
        from hypergraph.viz.widget import visualize
        from hypergraph import Graph, node
        import tempfile
        import os

        # Create a graph with two inputs feeding the same function
        @node(output_name="response")
        def generate(system_prompt: str, max_tokens: int) -> str:
            return f"{system_prompt} {max_tokens}"

        graph = Graph(nodes=[generate])

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name
        visualize(graph, depth=1, output=temp_path, _debug_overlays=True)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(f"file://{temp_path}")

                page.wait_for_function(
                    "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0 && window.__hypergraphVizReady === true",
                    timeout=10000,
                )

                result = page.evaluate("""() => {
                    const debug = window.__hypergraphVizDebug;

                    // Find INPUT nodes - they have nodeType 'INPUT' or are input group nodes
                    const inputNodes = debug.nodes.filter(n =>
                        n.nodeType === 'INPUT' ||
                        n.nodeType === 'INPUT_GROUP' ||
                        n.id.includes('__inputs__')
                    );

                    // Also look for leaf nodes that have no incoming edges (INPUT behavior)
                    const nodeIds = new Set(debug.nodes.map(n => n.id));
                    const hasIncoming = new Set();
                    const hasOutgoing = new Set();

                    for (const edge of debug.edges) {
                        if (nodeIds.has(edge.target)) hasIncoming.add(edge.target);
                        if (nodeIds.has(edge.source)) hasOutgoing.add(edge.source);
                    }

                    // Leaf nodes: have outgoing edges, no incoming edges
                    const leafNodes = debug.nodes.filter(n =>
                        hasOutgoing.has(n.id) && !hasIncoming.has(n.id)
                    );

                    // Get the X coordinates of all leaf nodes
                    const leafXCoords = leafNodes.map(n => ({
                        id: n.id,
                        x: n.x,
                        y: n.y,
                        centerX: n.x + n.width / 2
                    }));

                    // If there are multiple leaf nodes, check if they have different X coordinates
                    let hasHorizontalSpread = false;
                    let minXDiff = Infinity;

                    if (leafXCoords.length >= 2) {
                        for (let i = 0; i < leafXCoords.length; i++) {
                            for (let j = i + 1; j < leafXCoords.length; j++) {
                                const xDiff = Math.abs(leafXCoords[i].x - leafXCoords[j].x);
                                if (xDiff > 10) {
                                    hasHorizontalSpread = true;
                                }
                                if (xDiff < minXDiff) minXDiff = xDiff;
                            }
                        }
                    }

                    return {
                        inputNodes: inputNodes.map(n => ({ id: n.id, x: n.x, y: n.y })),
                        leafNodes: leafXCoords,
                        hasHorizontalSpread: hasHorizontalSpread,
                        minXDiff: minXDiff,
                        allNodes: debug.nodes.map(n => ({ id: n.id, x: n.x, y: n.y, nodeType: n.nodeType }))
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        # We expect at least 2 leaf nodes (the two input parameters)
        assert len(result["leafNodes"]) >= 2, (
            f"Expected at least 2 leaf nodes (input parameters), found {len(result['leafNodes'])}:\n"
            f"Leaf nodes: {result['leafNodes']}\n"
            f"All nodes: {result['allNodes']}"
        )

        # Check that they have different X coordinates (horizontal spread)
        assert result["hasHorizontalSpread"], (
            f"INPUT nodes should be spread horizontally (side-by-side), not stacked vertically!\n"
            f"Leaf nodes: {result['leafNodes']}\n"
            f"Minimum X difference: {result['minXDiff']}px (should be > 10px for horizontal spread)\n"
            f"The fixOverlappingNodes function should shift leaf nodes horizontally, not vertically."
        )
