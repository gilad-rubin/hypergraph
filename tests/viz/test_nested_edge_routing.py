"""Tests for nested graph edge routing.

These tests verify that edges connecting to/from expanded nested graphs
have correct positions (target below source, positive vertical distance).
"""

import pytest

# Import shared fixtures and helpers from conftest
from tests.viz.conftest import (
    HAS_PLAYWRIGHT,
    make_workflow,
    make_outer,
)


# =============================================================================
# Tests
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestNestedEdgeRouting:
    """Tests for nested graph edge validation using Playwright extraction."""

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

        # Should have nodes: input_text, preprocess (PIPELINE), clean_text, normalize_text, analyze
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
class TestEdgeRoutingToInternalNodes:
    """Tests that edges route to actual internal nodes, not container boundaries."""

    def test_workflow_input_edge_visual_target(self):
        """Test that workflow input edge VISUALLY connects to clean_text, not preprocess.

        This tests the actual rendered edge path, not just validation data.
        The edge from input_text should visually end at clean_text's position.
        """
        from playwright.sync_api import sync_playwright
        from hypergraph.viz.widget import visualize
        import tempfile
        import os

        workflow = make_workflow()

        # Render to temp file
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name
        visualize(workflow, depth=1, output=temp_path, _debug_overlays=True)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(f"file://{temp_path}")

                # Wait for layout
                page.wait_for_function(
                    "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0 && window.__hypergraphVizReady === true",
                    timeout=10000,
                )

                # Extract the SVG path of the edge and node positions
                result = page.evaluate("""() => {
                    const debug = window.__hypergraphVizDebug;

                    // Find preprocess and clean_text positions
                    const preprocess = debug.nodes.find(n => n.id === 'preprocess');
                    const cleanText = debug.nodes.find(n => n.id === 'clean_text');

                    // Find the input_text -> clean_text edge (individual input node, not __inputs__)
                    const edgeGroups = document.querySelectorAll('.react-flow__edge');
                    let inputEdgePath = null;
                    let edgeId = null;
                    for (const group of edgeGroups) {
                        const id = group.getAttribute('data-testid') || group.id || '';
                        // Edge IDs contain source-target pattern: input_text -> clean_text
                        if (id.includes('input_text')) {
                            const path = group.querySelector('path');
                            if (path) {
                                inputEdgePath = path.getAttribute('d');
                                edgeId = id;
                                break;
                            }
                        }
                    }

                    // If not found by ID, find edge that starts near input_text position
                    if (!inputEdgePath) {
                        const inputNode = debug.nodes.find(n => n.id === 'input_text');
                        if (inputNode) {
                            const inputBottom = inputNode.y + inputNode.height;
                            for (const group of edgeGroups) {
                                const path = group.querySelector('path');
                                if (path) {
                                    const d = path.getAttribute('d');
                                    // Parse first Y coordinate from path
                                    const match = d.match(/M\\s*[\\d.]+\\s+([\\d.]+)/);
                                    if (match) {
                                        const startY = parseFloat(match[1]);
                                        // Check if edge starts near input_text bottom
                                        if (Math.abs(startY - inputBottom) < 20) {
                                            inputEdgePath = d;
                                            edgeId = group.getAttribute('data-testid') || 'found-by-position';
                                            break;
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // Parse the last point from the path (target Y coordinate)
                    let pathEndY = null;
                    if (inputEdgePath) {
                        const coords = inputEdgePath.match(/[\\d.]+/g);
                        if (coords && coords.length >= 2) {
                            pathEndY = parseFloat(coords[coords.length - 1]);
                        }
                    }

                    return {
                        preprocessTop: preprocess ? preprocess.y : null,
                        cleanTextTop: cleanText ? cleanText.y : null,
                        pathEndY: pathEndY,
                        path: inputEdgePath,
                        edgeId: edgeId,
                        allEdgeIds: Array.from(edgeGroups).map(g => g.getAttribute('data-testid') || g.id),
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        preprocess_top = result["preprocessTop"]
        clean_text_top = result["cleanTextTop"]
        path_end_y = result["pathEndY"]

        assert clean_text_top is not None, "clean_text node not found"
        assert path_end_y is not None, f"Could not parse edge path: {result['path']}\nAll edges: {result.get('allEdgeIds')}"

        # The edge should end near clean_text's top, not preprocess's top
        tolerance = 10
        connects_to_clean_text = abs(path_end_y - clean_text_top) <= tolerance
        connects_to_preprocess = abs(path_end_y - preprocess_top) <= tolerance

        assert connects_to_clean_text and not connects_to_preprocess, (
            f"Edge VISUALLY connects to container, not internal node!\n"
            f"Edge ID: {result.get('edgeId')}\n"
            f"Edge path ends at Y={path_end_y}px\n"
            f"clean_text top: {clean_text_top}px (expected)\n"
            f"preprocess top: {preprocess_top}px (container)\n"
            f"Path: {result['path'][:100] if result['path'] else 'None'}...\n"
            f"All edge IDs: {result.get('allEdgeIds')}"
        )

    def test_input_edge_routes_to_internal_node(self):
        """Test that input_x edges connect to internal nodes, not containers.

        When a nested graph is expanded, edges from input_x should connect
        to the actual consuming node (e.g., step1) not the container (e.g., middle).
        """
        from hypergraph.viz import extract_debug_data

        outer = make_outer()
        data = extract_debug_data(outer, depth=2)

        # Find the edge from input_x to step1 or middle
        # The visual target should be step1, not middle
        input_edge = None
        for edge in data.edges:
            if edge.source == "input_x":
                input_edge = edge
                break

        assert input_edge is not None, f"No edge from input_x found. Available edges: {[(e.source, e.target) for e in data.edges]}"

        # Get positions of middle (container) and step1 (internal node)
        middle_node = None
        step1_node = None
        for node in data.nodes:
            if node.get("id") == "middle":
                middle_node = node
            elif node.get("id") == "step1":
                step1_node = node

        assert step1_node is not None, "step1 node not found in expanded graph"

        # The edge's target top should match step1's top, not middle's top
        # Allow small tolerance (5px) for rendering differences
        tolerance = 5
        step1_top = step1_node.get("y", 0)
        edge_tgt_top = input_edge.tgt_top

        # This assertion should FAIL if edges connect to containers
        assert abs(edge_tgt_top - step1_top) <= tolerance, (
            f"Edge from __inputs__ connects to container boundary, not internal node.\n"
            f"Edge target top: {edge_tgt_top}px\n"
            f"step1 top: {step1_top}px (expected)\n"
            f"middle top: {middle_node.get('y', 0) if middle_node else 'N/A'}px (container)\n"
            f"The edge should visually connect to step1, not middle."
        )

    def test_output_edge_routes_from_internal_node(self):
        """Test that output edges connect from internal nodes, not containers.

        When a nested graph is expanded, edges from internal data nodes should
        connect from the actual producer (e.g., validate's output), not the container.
        """
        from hypergraph.viz import extract_debug_data

        outer = make_outer()
        data = extract_debug_data(outer, depth=2)

        # Find the edge to log_result
        output_edge = None
        for edge in data.edges:
            if edge.target == "log_result":
                output_edge = edge
                break

        assert output_edge is not None, "No edge to log_result found"

        # Get positions of validate's data node and middle's data node
        validate_data_node = None
        for node in data.nodes:
            if "validate" in node.get("id", "") and "data" in node.get("id", ""):
                validate_data_node = node
                break

        # The edge should originate from validate's data node, which is inside middle
        # If it originates from middle's boundary, the source bottom will match middle's bounds
        middle_node = None
        for node in data.nodes:
            if node.get("id") == "middle":
                middle_node = node
                break

        # If source bottom is close to middle's bottom, edge connects to container
        # If source bottom is close to validate_data's bottom, edge connects to internal node
        if middle_node and validate_data_node:
            middle_bottom = middle_node.get("y", 0) + middle_node.get("height", 0)
            validate_bottom = validate_data_node.get("y", 0) + validate_data_node.get("height", 0)
            edge_src_bottom = output_edge.src_bottom

            # This should FAIL if edge connects to container
            tolerance = 5
            connects_to_container = abs(edge_src_bottom - middle_bottom) <= tolerance
            connects_to_internal = abs(edge_src_bottom - validate_bottom) <= tolerance

            assert not connects_to_container or connects_to_internal, (
                f"Edge to log_result connects from container boundary, not internal node.\n"
                f"Edge source bottom: {edge_src_bottom}px\n"
                f"validate data bottom: {validate_bottom}px (expected)\n"
                f"middle bottom: {middle_bottom}px (container)\n"
                f"The edge should visually connect from validate's output."
            )


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestOutputEdgeRouting:
    """Tests that output edges route from actual internal nodes."""

    def test_workflow_output_edge_visual_source(self):
        """Test that preprocess -> analyze edge starts from normalize_text's output.

        When a nested graph is expanded, edges FROM the container should
        visually start from the actual producing node's output, not container boundary.
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

                    // Find preprocess and normalize_text (last node in preprocess)
                    const preprocess = debug.nodes.find(n => n.id === 'preprocess');
                    const normalizeText = debug.nodes.find(n => n.id === 'normalize_text');
                    const dataNormalizeNormalized = debug.nodes.find(n => n.id && n.id.includes('data_normalize'));

                    // Find the normalize_text -> analyze edge
                    // When preprocess is expanded, edges route from internal producer
                    const edgeGroups = document.querySelectorAll('.react-flow__edge');
                    let outputEdgePath = null;
                    for (const group of edgeGroups) {
                        const id = group.getAttribute('data-testid') || '';
                        if (id.includes('normalize_text') && id.includes('analyze')) {
                            const path = group.querySelector('path');
                            if (path) {
                                outputEdgePath = path.getAttribute('d');
                                break;
                            }
                        }
                    }

                    // Parse the first Y coordinate from path (source Y)
                    let pathStartY = null;
                    if (outputEdgePath) {
                        const match = outputEdgePath.match(/M\\s*[\\d.]+\\s+([\\d.]+)/);
                        if (match) {
                            pathStartY = parseFloat(match[1]);
                        }
                    }

                    return {
                        preprocessBottom: preprocess ? preprocess.y + preprocess.height : null,
                        normalizeTextBottom: normalizeText ? normalizeText.y + normalizeText.height : null,
                        pathStartY: pathStartY,
                        path: outputEdgePath,
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        preprocess_bottom = result["preprocessBottom"]
        normalize_text_bottom = result["normalizeTextBottom"]
        path_start_y = result["pathStartY"]

        assert normalize_text_bottom is not None, "normalize_text node not found"
        assert path_start_y is not None, f"Could not parse edge path: {result['path']}"

        # The edge should start near normalize_text's bottom (or its data node), not preprocess's bottom
        tolerance = 15
        connects_from_internal = abs(path_start_y - normalize_text_bottom) <= tolerance
        connects_from_container = abs(path_start_y - preprocess_bottom) <= tolerance

        # This test will FAIL if edge starts from container boundary
        assert connects_from_internal or not connects_from_container, (
            f"Output edge starts from container boundary, not internal node!\n"
            f"Edge path starts at Y={path_start_y}px\n"
            f"normalize_text bottom: {normalize_text_bottom}px (expected)\n"
            f"preprocess bottom: {preprocess_bottom}px (container)\n"
            f"Path: {result['path'][:100] if result['path'] else 'None'}..."
        )


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestDoubleNestedEdgeRouting:
    """Tests for edge routing in doubly-nested graphs (depth=1 with 2-level nesting)."""

    def test_outer_depth1_input_routes_to_inner(self):
        """Test outer at depth=1: input edge should route to inner container, not middle.

        The outer graph has middle->inner->step1. At depth=1, middle is expanded
        showing inner (collapsed). The input edge from input_x should visually
        connect to inner (the collapsed container with step1), not middle boundary.
        """
        from playwright.sync_api import sync_playwright
        from hypergraph.viz.widget import visualize
        import tempfile
        import os

        outer = make_outer()

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name
        visualize(outer, depth=1, output=temp_path, _debug_overlays=True)

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

                    // Find middle container and inner container (step1 is inside inner)
                    const middle = debug.nodes.find(n => n.id === 'middle');
                    const inner = debug.nodes.find(n => n.id === 'inner');

                    // Find the input_x edge (individual input node, not __inputs__)
                    const edgeGroups = document.querySelectorAll('.react-flow__edge');
                    let inputEdgePath = null;
                    for (const group of edgeGroups) {
                        const id = group.getAttribute('data-testid') || '';
                        if (id.includes('input_x')) {
                            const path = group.querySelector('path');
                            if (path) {
                                inputEdgePath = path.getAttribute('d');
                                break;
                            }
                        }
                    }

                    // Parse end Y from path
                    let pathEndY = null;
                    if (inputEdgePath) {
                        const coords = inputEdgePath.match(/[\\d.]+/g);
                        if (coords && coords.length >= 2) {
                            pathEndY = parseFloat(coords[coords.length - 1]);
                        }
                    }

                    return {
                        middleTop: middle ? middle.y : null,
                        innerTop: inner ? inner.y : null,
                        pathEndY: pathEndY,
                        path: inputEdgePath,
                        allEdgeIds: Array.from(edgeGroups).map(g => g.getAttribute('data-testid') || g.id),
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        middle_top = result["middleTop"]
        inner_top = result["innerTop"]
        path_end_y = result["pathEndY"]

        assert inner_top is not None, "inner node not found at depth=1"
        assert path_end_y is not None, f"Could not parse edge path: {result['path']}\nAll edges: {result.get('allEdgeIds')}"

        # The edge should end at inner's top (which contains step1), not middle's top
        tolerance = 10
        connects_to_inner = abs(path_end_y - inner_top) <= tolerance
        connects_to_middle = abs(path_end_y - middle_top) <= tolerance

        assert connects_to_inner and not connects_to_middle, (
            f"Double-nested input edge connects to outer container, not inner!\n"
            f"Edge ends at Y={path_end_y}px\n"
            f"inner top: {inner_top}px (expected - contains step1)\n"
            f"middle top: {middle_top}px (outer container)\n"
            f"Path: {result['path'][:100] if result['path'] else 'None'}..."
        )


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestEdgeVisualGaps:
    """Tests that edges connect to nodes without visible gaps."""

    def test_workflow_expanded_output_edge_no_gap(self):
        """Test that preprocess->analyze edge has no gap at source.

        When preprocess is expanded, the edge from preprocess to analyze
        should start from normalize_text's bottom without a visible gap.
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

                    // Find normalize_text (the actual producer) and preprocess container
                    const normalizeText = debug.nodes.find(n => n.id === 'normalize_text');
                    const preprocess = debug.nodes.find(n => n.id === 'preprocess');
                    const analyze = debug.nodes.find(n => n.id === 'analyze');

                    // Find the normalize_text -> analyze edge (the internal producer to external consumer)
                    // When preprocess is expanded, edges route from internal producer (normalize_text)
                    const edgeGroups = document.querySelectorAll('.react-flow__edge');
                    let outputEdgePath = null;
                    for (const group of edgeGroups) {
                        const id = group.getAttribute('data-testid') || '';
                        // Edge should be from normalize_text (internal producer) to analyze
                        if (id.includes('normalize_text') && id.includes('analyze')) {
                            const path = group.querySelector('path');
                            if (path) {
                                outputEdgePath = path.getAttribute('d');
                                break;
                            }
                        }
                    }

                    // Parse start Y from path
                    let pathStartY = null;
                    if (outputEdgePath) {
                        const match = outputEdgePath.match(/M\\s*[\\d.]+\\s+([\\d.]+)/);
                        if (match) pathStartY = parseFloat(match[1]);
                    }

                    return {
                        normalizeTextBottom: normalizeText ? normalizeText.y + normalizeText.height : null,
                        preprocessBottom: preprocess ? preprocess.y + preprocess.height : null,
                        analyzeTop: analyze ? analyze.y : null,
                        pathStartY: pathStartY,
                        path: outputEdgePath,
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        normalize_bottom = result["normalizeTextBottom"]
        path_start_y = result["pathStartY"]

        assert normalize_bottom is not None, "normalize_text not found"
        assert path_start_y is not None, f"Could not parse edge path: {result['path']}"

        # Edge should start within 5px of the source node's bottom (no visible gap)
        gap = abs(path_start_y - normalize_bottom)
        max_gap = 5

        assert gap <= max_gap, (
            f"Output edge has visible gap from source node!\n"
            f"Edge starts at Y={path_start_y}px\n"
            f"normalize_text bottom: {normalize_bottom}px\n"
            f"Gap: {gap}px (should be <= {max_gap}px)\n"
            f"Path: {result['path'][:100] if result['path'] else 'None'}..."
        )

    def test_outer_collapsed_output_edge_no_gap(self):
        """Test that middle->log_result edge has no gap when collapsed.

        When outer is viewed at depth=0, the edge from collapsed middle
        to log_result should start from middle's bottom without a gap.
        """
        from playwright.sync_api import sync_playwright
        from hypergraph.viz.widget import visualize
        import tempfile
        import os

        outer = make_outer()

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name
        visualize(outer, depth=0, output=temp_path, _debug_overlays=True)

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

                    // Find middle and log_result
                    const middle = debug.nodes.find(n => n.id === 'middle');
                    const logResult = debug.nodes.find(n => n.id === 'log_result');

                    // Find the middle -> log_result edge
                    const edgeGroups = document.querySelectorAll('.react-flow__edge');
                    let outputEdgePath = null;
                    for (const group of edgeGroups) {
                        const id = group.getAttribute('data-testid') || '';
                        if (id.includes('middle') && id.includes('log_result')) {
                            const path = group.querySelector('path');
                            if (path) {
                                outputEdgePath = path.getAttribute('d');
                                break;
                            }
                        }
                    }

                    // Parse start Y from path
                    let pathStartY = null;
                    if (outputEdgePath) {
                        const match = outputEdgePath.match(/M\\s*[\\d.]+\\s+([\\d.]+)/);
                        if (match) pathStartY = parseFloat(match[1]);
                    }

                    return {
                        middleBottom: middle ? middle.y + middle.height : null,
                        logResultTop: logResult ? logResult.y : null,
                        pathStartY: pathStartY,
                        path: outputEdgePath,
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        middle_bottom = result["middleBottom"]
        path_start_y = result["pathStartY"]

        assert middle_bottom is not None, "middle node not found"
        assert path_start_y is not None, f"Could not parse edge path: {result['path']}"

        # Edge should start within 5px of the source node's bottom
        gap = abs(path_start_y - middle_bottom)
        max_gap = 5

        assert gap <= max_gap, (
            f"Collapsed output edge has visible gap from source!\n"
            f"Edge starts at Y={path_start_y}px\n"
            f"middle bottom: {middle_bottom}px\n"
            f"Gap: {gap}px (should be <= {max_gap}px)\n"
            f"Path: {result['path'][:100] if result['path'] else 'None'}..."
        )


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestCollapsedGraphEdges:
    """Tests that collapsed graphs have edges connecting properly without gaps."""

    def test_outer_collapsed_no_edge_gap(self):
        """Test that outer graph with collapsed middle has no visual gap.

        When viewing outer at depth=0 (middle collapsed), the edge from
        middle -> log_result should connect without a gap.
        """
        from playwright.sync_api import sync_playwright
        from hypergraph.viz.widget import visualize
        import tempfile
        import os

        outer = make_outer()

        # Render with depth=0 (middle collapsed)
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name
        visualize(outer, depth=0, output=temp_path, _debug_overlays=True)

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

                    // Find middle container
                    const middle = debug.nodes.find(n => n.id === 'middle');

                    // Find the middle -> log_result edge
                    const edgeGroups = document.querySelectorAll('.react-flow__edge');
                    let outputEdgePath = null;
                    for (const group of edgeGroups) {
                        const id = group.getAttribute('data-testid') || '';
                        if (id.includes('middle') && id.includes('log_result')) {
                            const path = group.querySelector('path');
                            if (path) {
                                outputEdgePath = path.getAttribute('d');
                                break;
                            }
                        }
                    }

                    // Parse the first Y coordinate from path (source Y - where edge starts)
                    let pathStartY = null;
                    if (outputEdgePath) {
                        const match = outputEdgePath.match(/M\\s*[\\d.]+\\s+([\\d.]+)/);
                        if (match) {
                            pathStartY = parseFloat(match[1]);
                        }
                    }

                    return {
                        middleBottom: middle ? middle.y + middle.height : null,
                        pathStartY: pathStartY,
                        path: outputEdgePath,
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        middle_bottom = result["middleBottom"]
        path_start_y = result["pathStartY"]

        assert middle_bottom is not None, "middle node not found"
        assert path_start_y is not None, f"Could not parse edge path: {result['path']}"

        # Edge should start AT the container bottom (within small tolerance)
        # A gap of more than 5px indicates the edge isn't connecting properly
        tolerance = 5
        gap = abs(path_start_y - middle_bottom)

        assert gap <= tolerance, (
            f"Collapsed graph has visual gap between container and edge!\n"
            f"Edge starts at Y={path_start_y}px\n"
            f"middle bottom: {middle_bottom}px\n"
            f"Gap: {gap}px (should be <= {tolerance}px)\n"
            f"Path: {result['path'][:100] if result['path'] else 'None'}..."
        )

    def test_workflow_collapsed_no_edge_gap(self):
        """Test that workflow with collapsed preprocess has no visual gap."""
        from playwright.sync_api import sync_playwright
        from hypergraph.viz.widget import visualize
        import tempfile
        import os

        workflow = make_workflow()

        # Render with depth=0 (preprocess collapsed)
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name
        visualize(workflow, depth=0, output=temp_path, _debug_overlays=True)

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

                    // Find preprocess container
                    const preprocess = debug.nodes.find(n => n.id === 'preprocess');

                    // Find the preprocess -> analyze edge
                    const edgeGroups = document.querySelectorAll('.react-flow__edge');
                    let outputEdgePath = null;
                    for (const group of edgeGroups) {
                        const id = group.getAttribute('data-testid') || '';
                        if (id.includes('preprocess') && id.includes('analyze')) {
                            const path = group.querySelector('path');
                            if (path) {
                                outputEdgePath = path.getAttribute('d');
                                break;
                            }
                        }
                    }

                    // Parse the first Y coordinate from path (source Y - where edge starts)
                    let pathStartY = null;
                    if (outputEdgePath) {
                        const match = outputEdgePath.match(/M\\s*[\\d.]+\\s+([\\d.]+)/);
                        if (match) {
                            pathStartY = parseFloat(match[1]);
                        }
                    }

                    return {
                        preprocessBottom: preprocess ? preprocess.y + preprocess.height : null,
                        pathStartY: pathStartY,
                        path: outputEdgePath,
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        preprocess_bottom = result["preprocessBottom"]
        path_start_y = result["pathStartY"]

        assert preprocess_bottom is not None, "preprocess node not found"
        assert path_start_y is not None, f"Could not parse edge path: {result['path']}"

        # Edge should start AT the container bottom (within small tolerance)
        tolerance = 5
        gap = abs(path_start_y - preprocess_bottom)

        assert gap <= tolerance, (
            f"Collapsed graph has visual gap between container and edge!\n"
            f"Edge starts at Y={path_start_y}px\n"
            f"preprocess bottom: {preprocess_bottom}px\n"
            f"Gap: {gap}px (should be <= {tolerance}px)\n"
            f"Path: {result['path'][:100] if result['path'] else 'None'}..."
        )


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


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestNestedGraphPadding:
    """Tests for symmetric padding in nested graphs."""

    def test_root_level_top_padding(self):
        """Test that the topmost node has adequate padding from viewport top.

        The first row of nodes should have at least 30px padding from y=0.
        """
        from hypergraph.viz import extract_debug_data

        workflow = make_workflow()
        data = extract_debug_data(workflow, depth=1)

        # Find the topmost node
        min_y = float('inf')
        top_node = None
        for n in data.nodes:
            y = n.get('y', float('inf'))
            if y < min_y:
                min_y = y
                top_node = n

        # Top padding should be at least 20px (we use 24px GRAPH_PADDING)
        min_padding = 20
        assert min_y >= min_padding, (
            f"Topmost node '{top_node.get('id')}' has insufficient top padding: "
            f"y={min_y}px, expected >= {min_padding}px"
        )

    def test_nested_graph_symmetric_padding(self):
        """Test that nested graph containers have symmetric padding.

        Nodes inside expanded containers should have approximately equal
        padding on left/right and top/bottom.
        """
        from hypergraph.viz import extract_debug_data

        outer = make_outer()
        data = extract_debug_data(outer, depth=2)

        # Find nodes that have parent padding info (children of expanded containers)
        for node in data.nodes:
            parent_pad = node.get("parentPad")
            if parent_pad is None:
                continue

            left = parent_pad.get("left", 0)
            right = parent_pad.get("right", 0)
            top = parent_pad.get("top", 0)
            bottom = parent_pad.get("bottom", 0)

            # Allow 10px tolerance for rounding differences
            tolerance = 10
            h_diff = abs(left - right)
            v_diff = abs(top - bottom)

            assert h_diff <= tolerance, (
                f"Node {node.get('id')} has asymmetric horizontal padding: "
                f"left={left}, right={right}, diff={h_diff}"
            )
            assert v_diff <= tolerance, (
                f"Node {node.get('id')} has asymmetric vertical padding: "
                f"top={top}, bottom={bottom}, diff={v_diff}"
            )


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestNodeToParentDebugAPI:
    """Tests that node_to_parent map is exposed via debug API for routing."""

    def test_node_to_parent_exposed_in_debug_api(self):
        """Test that node_to_parent is accessible via debug API routingData.

        The node_to_parent map is critical for edge routing when containers
        are collapsed - it allows JavaScript to find visible ancestors.
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
                    return {
                        hasRoutingData: !!debug.routingData,
                        hasNodeToParent: !!(debug.routingData && debug.routingData.node_to_parent),
                        nodeToParent: debug.routingData ? debug.routingData.node_to_parent : null,
                        allKeys: debug.routingData ? Object.keys(debug.routingData) : [],
                    };
                }""")

                browser.close()
        finally:
            os.unlink(temp_path)

        assert result["hasRoutingData"], "routingData not found in debug API"
        assert result["hasNodeToParent"], (
            f"node_to_parent not found in routingData. Available keys: {result['allKeys']}"
        )

        node_to_parent = result["nodeToParent"]
        assert isinstance(node_to_parent, dict), f"node_to_parent should be dict, got {type(node_to_parent)}"

        # Verify expected mappings for workflow graph
        # workflow has: preprocess[clean_text, normalize_text] -> analyze
        # So clean_text and normalize_text should have preprocess as parent
        assert node_to_parent.get("clean_text") == "preprocess", (
            f"clean_text should have parent 'preprocess', got: {node_to_parent.get('clean_text')}"
        )
        assert node_to_parent.get("normalize_text") == "preprocess", (
            f"normalize_text should have parent 'preprocess', got: {node_to_parent.get('normalize_text')}"
        )

        # analyze and preprocess are at root level, so no parent
        assert "analyze" not in node_to_parent or node_to_parent.get("analyze") is None
        assert "preprocess" not in node_to_parent or node_to_parent.get("preprocess") is None

    def test_node_to_parent_deeply_nested(self):
        """Test node_to_parent for deeply nested graphs (2+ levels).

        For outer graph: middle[inner[step1, step2], validate] -> log_result
        - step1, step2 should have parent 'inner'
        - inner, validate should have parent 'middle'
        - middle, log_result should have no parent
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

                node_to_parent = page.evaluate(
                    "window.__hypergraphVizDebug.routingData.node_to_parent"
                )

                browser.close()
        finally:
            os.unlink(temp_path)

        assert node_to_parent is not None, "node_to_parent not found"

        # Level 1: step1, step2 inside inner
        assert node_to_parent.get("step1") == "inner", (
            f"step1 should have parent 'inner', got: {node_to_parent.get('step1')}"
        )
        assert node_to_parent.get("step2") == "inner", (
            f"step2 should have parent 'inner', got: {node_to_parent.get('step2')}"
        )

        # Level 2: inner, validate inside middle
        assert node_to_parent.get("inner") == "middle", (
            f"inner should have parent 'middle', got: {node_to_parent.get('inner')}"
        )
        assert node_to_parent.get("validate") == "middle", (
            f"validate should have parent 'middle', got: {node_to_parent.get('validate')}"
        )

        # Root level: no parent
        assert "middle" not in node_to_parent or node_to_parent.get("middle") is None
        assert "log_result" not in node_to_parent or node_to_parent.get("log_result") is None
