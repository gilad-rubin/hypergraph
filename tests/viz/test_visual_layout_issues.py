"""Tests for visual layout issues.

These tests verify:
1. Input nodes are positioned ABOVE their target nodes (edges flow downward)
2. No visible gaps between edges and nodes
3. Edges connect to actual nodes, not container boundaries
4. Multiple INPUT nodes feeding the same target are side-by-side (horizontal spread)
"""

import pytest
from hypergraph import Graph, ifelse, node

# Import shared fixtures and helpers from conftest
from tests.viz.conftest import (
    HAS_PLAYWRIGHT,
    make_workflow,
    make_outer,
    render_to_page,
)


@node(output_name="doc_exists")
def check_document_exists(doc_id: str, vector_store: object, overwrite: bool) -> bool:
    return False


@ifelse(when_true="skip_document", when_false="process_document")
def should_process(doc_exists: bool, overwrite: bool) -> bool:
    return doc_exists and not overwrite


@node(output_name="processed_document")
def process_document(doc_id: str) -> dict:
    return {"status": "processed", "doc_id": doc_id}


@node(output_name="skipped_document")
def skip_document(doc_id: str) -> dict:
    return {"status": "skipped", "doc_id": doc_id}


@node(output_name="next_query")
def generate_query_from_doc(doc_id: str) -> str:
    return doc_id


def make_indexing_like_graph() -> Graph:
    """Nested topology used for visual routing regressions."""
    indexing_inner = Graph(
        nodes=[check_document_exists, should_process, process_document, skip_document],
        name="indexing",
    )
    return Graph(nodes=[indexing_inner.as_node(name="indexing_graph"), generate_query_from_doc]).bind(
        vector_store="mock_vector_store",
        overwrite=False,
    )


# =============================================================================
# Test: Input nodes should be ABOVE their targets (edges flow downward)
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestInputNodePosition:
    """Tests that input nodes are positioned above their target nodes."""

    def test_outer_depth2_input_above_step1(self, page, temp_html_file):
        """Input x should be positioned ABOVE step1, not below.

        The edge from input_x to step1 should flow DOWNWARD.
        """
        outer = make_outer()
        render_to_page(page, outer, depth=2, temp_path=temp_html_file)

        result = page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;

            // Find input node and step1 node (hierarchical ID)
            const inputNode = debug.nodes.find(n =>
                n.id.includes('input') || n.id === '__inputs__'
            );
            const step1Node = debug.nodes.find(n => n.id === 'middle/inner/step1');

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

    def test_workflow_depth1_input_above_clean_text(self, page, temp_html_file):
        """Input text should be positioned ABOVE clean_text."""
        workflow = make_workflow()
        render_to_page(page, workflow, depth=1, temp_path=temp_html_file)

        result = page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;

            const inputNode = debug.nodes.find(n =>
                n.id.includes('input') || n.id === '__inputs__'
            );
            const cleanTextNode = debug.nodes.find(n => n.id === 'preprocess/clean_text');

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

    def test_outer_depth2_input_edge_no_gap(self, page, temp_html_file):
        """Edge from input to step1 should have no gap at start or end."""
        outer = make_outer()
        render_to_page(page, outer, depth=2, temp_path=temp_html_file)

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


    def test_workflow_depth1_all_edges_no_gap(self, page, temp_html_file):
        """All edges in workflow should have no visible gaps.

        This tests the screenshot issue where:
        - 7px gap below 'text' input node
        - 19px gap above 'analyze' node
        """
        workflow = make_workflow()
        render_to_page(page, workflow, depth=1, temp_path=temp_html_file)

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

    def test_outer_depth2_edge_to_step1_not_inner(self, page, temp_html_file):
        """Edge should connect to step1's position, not inner container's boundary."""
        outer = make_outer()
        render_to_page(page, outer, depth=2, temp_path=temp_html_file)

        result = page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;

            // Find nodes (hierarchical IDs)
            const innerNode = debug.nodes.find(n => n.id === 'middle/inner');
            const step1Node = debug.nodes.find(n => n.id === 'middle/inner/step1');

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
# Test: Edge anchors + spacing regressions on indexing-like graph
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestIndexingLikeAnchorAndSpacing:
    """Regression tests from user-reported indexing visualization issues."""

    def test_ifelse_target_anchor_touches_visible_diamond_top(self, page, temp_html_file):
        """Edges targeting should_process must terminate exactly on visible top boundary."""
        graph = make_indexing_like_graph()
        render_to_page(page, graph, depth=1, temp_path=temp_html_file)

        result = page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;
            if (!debug || !debug.nodes || !debug.layoutedEdges) return { error: 'debug data unavailable' };

            const targetId = 'indexing_graph/should_process';
            const target = debug.nodes.find((n) => n.id === targetId);
            if (!target) return { error: 'target node not found' };

            const targetTop = target.y;
            const incoming = (debug.layoutedEdges || [])
                .filter((e) => (((e.data && e.data.actualTarget) || e.target) === targetId))
                .map((e) => {
                    const pts = (e.data && e.data.points) || [];
                    const end = pts.length ? pts[pts.length - 1] : null;
                    return {
                        edge: `${e.source} -> ${e.target}`,
                        actualEdge: `${(e.data && e.data.actualSource) || e.source} -> ${((e.data && e.data.actualTarget) || e.target)}`,
                        end,
                        dy: end ? Math.abs(end.y - targetTop) : null,
                    };
                });

            return { targetTop, incoming };
        }""")

        if "error" in result:
            pytest.fail(f"Setup error: {result}")

        bad = [e for e in result["incoming"] if (e["end"] is None) or (e["dy"] is None) or (e["dy"] > 0.75)]
        assert not bad, (
            "Edges to if-else node do not touch visible top boundary:\n" +
            "\n".join(
                f"  - {e['edge']} (actual {e['actualEdge']}), end={e['end']}, dy={e['dy']}"
                for e in bad
            ) +
            f"\nTarget top: {result['targetTop']}"
        )

    def test_distinct_source_lanes_have_min_horizontal_gap(self, page, temp_html_file):
        """Incoming edges from different sources should keep minimum lane separation."""
        graph = make_indexing_like_graph()
        render_to_page(page, graph, depth=1, temp_path=temp_html_file)

        result = page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;
            if (!debug || !debug.layoutedEdges) return { error: 'debug data unavailable' };

            const targetId = 'indexing_graph/check_document_exists';
            const incoming = (debug.layoutedEdges || [])
                .filter((e) => (((e.data && e.data.actualTarget) || e.target) === targetId))
                .map((e) => {
                    const points = (e.data && e.data.points) || [];
                    if (points.length < 2) return null;
                    const penultimate = points[points.length - 2];
                    return {
                        source: (e.data && e.data.actualSource) || e.source,
                        edge: `${e.source} -> ${e.target}`,
                        laneX: penultimate.x,
                    };
                })
                .filter(Boolean);

            const minGap = 12;
            const violations = [];
            for (let i = 0; i < incoming.length; i += 1) {
                for (let j = i + 1; j < incoming.length; j += 1) {
                    const a = incoming[i];
                    const b = incoming[j];
                    if (a.source === b.source) continue;
                    const dx = Math.abs(a.laneX - b.laneX);
                    if (dx < minGap) {
                        violations.push({
                            edgeA: a.edge,
                            edgeB: b.edge,
                            sourceA: a.source,
                            sourceB: b.source,
                            laneXA: a.laneX,
                            laneXB: b.laneX,
                            dx,
                            minGap,
                        });
                    }
                }
            }

            return { incoming, violations };
        }""")

        if "error" in result:
            pytest.fail(f"Setup error: {result}")

        assert not result["violations"], (
            "Incoming lanes are too close for distinct sources:\n" +
            "\n".join(
                f"  - {v['edgeA']} vs {v['edgeB']} ({v['sourceA']} / {v['sourceB']}), "
                f"laneX={v['laneXA']:.2f}/{v['laneXB']:.2f}, dx={v['dx']:.2f} < {v['minGap']}"
                for v in result["violations"]
            ) +
            f"\nIncoming: {result['incoming']}"
        )


# =============================================================================
# Test: Multiple INPUT nodes feeding the same target should be side-by-side
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestInputNodeHorizontalSpread:
    """Tests that multiple INPUT nodes feeding the same target are spread horizontally.

    Note: When inputs share the same target AND bound status, they are now grouped into
    INPUT_GROUP nodes. This test verifies that:
    1. When inputs CAN be grouped (same target, same bound status), they become INPUT_GROUP
    2. When inputs CANNOT be grouped (different targets or bound status), they spread horizontally
    """

    def test_multiple_inputs_same_target_are_grouped(self, page, temp_html_file):
        """When two unbound inputs feed the same target, they should be grouped.

        This validates the INPUT_GROUP feature - inputs with identical consumers
        and bound status are consolidated into a single group node.
        """
        from hypergraph import Graph, node

        # Create a graph with two inputs feeding the same function
        @node(output_name="response")
        def generate(system_prompt: str, max_tokens: int) -> str:
            return f"{system_prompt} {max_tokens}"

        graph = Graph(nodes=[generate])
        render_to_page(page, graph, depth=1, temp_path=temp_html_file)

        result = page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;

            // Find INPUT_GROUP nodes
            const groupNodes = debug.nodes.filter(n =>
                n.nodeType === 'INPUT_GROUP'
            );

            // Find individual INPUT nodes
            const inputNodes = debug.nodes.filter(n =>
                n.nodeType === 'INPUT'
            );

            return {
                groupNodes: groupNodes.map(n => ({ id: n.id, nodeType: n.nodeType })),
                inputNodes: inputNodes.map(n => ({ id: n.id, nodeType: n.nodeType })),
                allNodes: debug.nodes.map(n => ({ id: n.id, nodeType: n.nodeType }))
            };
        }""")

        # With INPUT_GROUP feature, both inputs should be grouped into one node
        assert len(result["groupNodes"]) == 1, (
            f"Expected 1 INPUT_GROUP node (grouping max_tokens and system_prompt), found {len(result['groupNodes'])}:\n"
            f"Group nodes: {result['groupNodes']}\n"
            f"Input nodes: {result['inputNodes']}\n"
            f"All nodes: {result['allNodes']}"
        )

        # The group ID should contain both param names
        group_id = result["groupNodes"][0]["id"]
        assert "max_tokens" in group_id and "system_prompt" in group_id, (
            f"INPUT_GROUP should contain both parameter names, got: {group_id}"
        )

    def test_different_targets_no_grouping_horizontal_spread(self, page, temp_html_file):
        """When inputs have different targets, they should NOT be grouped and spread horizontally.

        This tests that the horizontal spread logic still works for inputs that cannot
        be grouped (different consumers).
        """
        from hypergraph import Graph, node

        # Create a graph with two inputs feeding different functions
        @node(output_name="step1_out")
        def step1(input_a: str) -> str:
            return input_a

        @node(output_name="step2_out")
        def step2(input_b: str, step1_out: str) -> str:
            return f"{input_b} {step1_out}"

        graph = Graph(nodes=[step1, step2])
        render_to_page(page, graph, depth=1, temp_path=temp_html_file)

        result = page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;

            // Find individual INPUT nodes (not grouped since different targets)
            const inputNodes = debug.nodes.filter(n =>
                n.nodeType === 'INPUT'
            );

            // Get the X coordinates of INPUT nodes
            const inputXCoords = inputNodes.map(n => ({
                id: n.id,
                x: n.x,
                y: n.y,
                centerX: n.x + n.width / 2
            }));

            // Check if they have different X coordinates (horizontal spread)
            let hasHorizontalSpread = false;
            let minXDiff = Infinity;

            if (inputXCoords.length >= 2) {
                for (let i = 0; i < inputXCoords.length; i++) {
                    for (let j = i + 1; j < inputXCoords.length; j++) {
                        const xDiff = Math.abs(inputXCoords[i].x - inputXCoords[j].x);
                        if (xDiff > 10) {
                            hasHorizontalSpread = true;
                        }
                        if (xDiff < minXDiff) minXDiff = xDiff;
                    }
                }
            }

            return {
                inputNodes: inputXCoords,
                hasHorizontalSpread: hasHorizontalSpread,
                minXDiff: minXDiff,
                allNodes: debug.nodes.map(n => ({ id: n.id, x: n.x, y: n.y, nodeType: n.nodeType }))
            };
        }""")

        # Should have 2 separate INPUT nodes (different targets = no grouping)
        assert len(result["inputNodes"]) == 2, (
            f"Expected 2 INPUT nodes (different targets, no grouping), found {len(result['inputNodes'])}:\n"
            f"Input nodes: {result['inputNodes']}\n"
            f"All nodes: {result['allNodes']}"
        )

        # Check that they have different X coordinates (horizontal spread)
        assert result["hasHorizontalSpread"], (
            f"INPUT nodes should be spread horizontally (side-by-side), not stacked vertically!\n"
            f"Input nodes: {result['inputNodes']}\n"
            f"Minimum X difference: {result['minXDiff']}px (should be > 10px for horizontal spread)\n"
            f"The fixOverlappingNodes function should shift leaf nodes horizontally, not vertically."
        )

    def test_bound_unbound_same_target_separate_groups(self, page, temp_html_file):
        """When some inputs are bound and others unbound (same target), they should be in separate groups.

        This validates that the INPUT_GROUP feature respects bound status:
        - Bound inputs (with values) get their own group
        - Unbound inputs (requiring user input) get their own group
        """
        from hypergraph import Graph, node

        # Create a graph with 4 inputs: 2 bound, 2 unbound - all to same target
        @node(output_name="response")
        def generate(
            system_prompt: str,
            max_tokens: int,
            temperature: float,
            model: str,
        ) -> str:
            return f"{system_prompt} {max_tokens} {temperature} {model}"

        graph = Graph(nodes=[generate])
        # Bind 2 of the 4 inputs
        bound_graph = graph.bind(temperature=0.7, model="gpt-4")
        render_to_page(page, bound_graph, depth=1, temp_path=temp_html_file)

        result = page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;

            // Find INPUT_GROUP nodes
            const groupNodes = debug.nodes.filter(n =>
                n.nodeType === 'INPUT_GROUP'
            );

            // Find individual INPUT nodes (should be none if all are grouped)
            const inputNodes = debug.nodes.filter(n =>
                n.nodeType === 'INPUT'
            );

            // Get details about each group
            const groupDetails = groupNodes.map(n => ({
                id: n.id,
                nodeType: n.nodeType,
                // Check if the group contains bound inputs (dashed outline class)
                // Group ID format: input_group_param1_param2_...
                params: n.id.replace('input_group_', '').split('_'),
            }));

            return {
                groupNodes: groupDetails,
                inputNodes: inputNodes.map(n => ({ id: n.id, nodeType: n.nodeType })),
                allNodes: debug.nodes.map(n => ({ id: n.id, nodeType: n.nodeType }))
            };
        }""")

        # With 2 bound and 2 unbound inputs to the same target, we expect 2 groups
        assert len(result["groupNodes"]) == 2, (
            f"Expected 2 INPUT_GROUP nodes (one for bound, one for unbound inputs), found {len(result['groupNodes'])}:\n"
            f"Group nodes: {result['groupNodes']}\n"
            f"Input nodes: {result['inputNodes']}\n"
            f"All nodes: {result['allNodes']}"
        )

        # Verify no ungrouped INPUT nodes remain
        assert len(result["inputNodes"]) == 0, (
            f"Expected all inputs to be grouped, but found {len(result['inputNodes'])} ungrouped:\n"
            f"Input nodes: {result['inputNodes']}"
        )

        # Check that one group has bound params (model, temperature) and one has unbound (max_tokens, system_prompt)
        group_ids = [g["id"] for g in result["groupNodes"]]
        bound_params = {"model", "temperature"}
        unbound_params = {"max_tokens", "system_prompt"}

        # Each group should contain either all bound or all unbound params
        # Check by looking for param names in the group ID string
        for group_id in group_ids:
            has_bound = any(p in group_id for p in bound_params)
            has_unbound = any(p in group_id for p in unbound_params)
            # A group should have EITHER bound OR unbound params, not both
            assert has_bound != has_unbound, (
                f"Group {group_id} mixes bound and unbound params!\n"
                f"has_bound: {has_bound}, has_unbound: {has_unbound}\n"
                f"Expected groups to separate bound ({bound_params}) from unbound ({unbound_params})"
            )
