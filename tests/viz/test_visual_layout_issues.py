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
    click_to_collapse_container,
    click_to_expand_container,
    make_workflow,
    make_outer,
    render_to_page,
)
from tests.viz.test_complex_nested_graphs import make_rag_style_graph
from tests.viz.test_cross_boundary_edge import build_triple_nested_graph


@node(output_name="validated")
def branch_anchor_validate(value: str) -> bool:
    return bool(value)


@ifelse(when_true="branch_anchor_accept", when_false="branch_anchor_reject")
def branch_anchor_gate(validated: bool) -> bool:
    return validated


@node(output_name="accepted")
def branch_anchor_accept() -> str:
    return "ok"


@node(output_name="rejected")
def branch_anchor_reject() -> str:
    return "nope"


@node(output_name="doc_exists")
def check_document_exists(doc_id: str, vector_store: str) -> bool:
    return bool(doc_id and vector_store)


@ifelse(when_true="process_document", when_false="skip_document")
def should_process(doc_exists: bool, overwrite: bool) -> bool:
    return (not doc_exists) or overwrite


@node(output_name="prepared")
def prepare_document(doc_exists: bool) -> str:
    return "prepared"


@node(output_name="index_result")
def process_document(prepared: str) -> dict:
    return {"prepared": prepared, "status": "processed"}


@node(output_name="index_result")
def skip_document() -> dict:
    return {"status": "skipped"}


def make_separate_outputs_crossing_graph() -> Graph:
    return Graph(
        nodes=[
            check_document_exists,
            should_process,
            prepare_document,
            process_document,
            skip_document,
        ]
    )


def make_branch_anchor_graph() -> Graph:
    return Graph(
        nodes=[
            branch_anchor_validate,
            branch_anchor_gate,
            branch_anchor_accept,
            branch_anchor_reject,
        ]
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

    @staticmethod
    def _branch_label_reports(page) -> dict:
        return page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;
            const edges = debug.layoutedEdges || [];

            const transformToPoint = (style) => {
                if (!style) return null;
                const match = style.match(
                    /translate\\(-50%,\\s*-50%\\)\\s*translate\\(\\s*([\\d.-]+)px\\s*,\\s*([\\d.-]+)px\\s*\\)/
                );
                if (!match) return null;
                return { x: parseFloat(match[1]), y: parseFloat(match[2]) };
            };

            const labelNodes = Array.from(document.querySelectorAll('.react-flow__edgelabel-renderer div'))
                .map((el) => {
                    const text = (el.textContent || '').trim();
                    const point = transformToPoint(el.getAttribute('style') || '');
                    if (!point) return null;
                    return { text: text, x: point.x, y: point.y };
                })
                .filter(Boolean)
                .filter((l) => l.text.includes('True') || l.text.includes('False'));

            const midpointOnOutgoingLeg = (pts) => {
                if (!pts || pts.length < 2) return null;
                const MIN_SIGNIFICANT_SEGMENT = 6;
                const TURN_THRESHOLD_DEG = 38;

                const segments = [];
                let cumulative = 0;
                for (let i = 0; i < pts.length - 1; i += 1) {
                    const a = pts[i];
                    const b = pts[i + 1];
                    const dx = b.x - a.x;
                    const dy = b.y - a.y;
                    const len = Math.hypot(dx, dy);
                    cumulative += len;
                    segments.push({ x: dx, y: dy, len: len, endDistance: cumulative });
                }
                if (!segments.length) return null;

                let firstSig = -1;
                for (let i = 0; i < segments.length; i += 1) {
                    if (segments[i].len >= MIN_SIGNIFICANT_SEGMENT) {
                        firstSig = i;
                        break;
                    }
                }
                if (firstSig < 0) return null;

                const base = segments[firstSig];
                const baseLen = base.len || 1;
                const baseX = base.x / baseLen;
                const baseY = base.y / baseLen;
                let outgoingLength = cumulative;

                for (let i = firstSig + 1; i < segments.length; i += 1) {
                    const seg = segments[i];
                    if (seg.len < MIN_SIGNIFICANT_SEGMENT) continue;
                    const segX = seg.x / seg.len;
                    const segY = seg.y / seg.len;
                    const dot = Math.max(-1, Math.min(1, baseX * segX + baseY * segY));
                    const angleDeg = Math.acos(dot) * 180 / Math.PI;
                    if (angleDeg >= TURN_THRESHOLD_DEG) {
                        outgoingLength = segments[i - 1].endDistance;
                        break;
                    }
                }

                const target = outgoingLength * 0.5;
                let walked = 0;
                for (let i = 0; i < pts.length - 1; i += 1) {
                    const p0 = pts[i];
                    const p1 = pts[i + 1];
                    const dx = p1.x - p0.x;
                    const dy = p1.y - p0.y;
                    const segLen = Math.hypot(dx, dy);
                    if (segLen <= 1e-6) continue;
                    if (walked + segLen >= target) {
                        const t = (target - walked) / segLen;
                        return { x: p0.x + dx * t, y: p0.y + dy * t };
                    }
                    walked += segLen;
                }
                return { x: pts[pts.length - 1].x, y: pts[pts.length - 1].y };
            };

            const reports = [];
            for (const edge of edges) {
                const label = (edge.data && edge.data.label) || '';
                if (label !== 'True' && label !== 'False') continue;
                const pts = (edge.data && edge.data.points) || [];
                const expected = midpointOnOutgoingLeg(pts);
                if (!expected) continue;

                let best = null;
                let bestDist = Infinity;
                for (const ln of labelNodes) {
                    if (!ln.text.includes(label)) continue;
                    const d = Math.hypot(ln.x - expected.x, ln.y - expected.y);
                    if (d < bestDist) {
                        best = ln;
                        bestDist = d;
                    }
                }

                reports.push({
                    edge: edge.id,
                    label: label,
                    expected: expected,
                    actual: best,
                    distance: bestDist,
                });
            }

            return {
                reports: reports,
                parsedLabels: labelNodes,
                edgesWithBranchLabel: edges
                    .filter((e) => {
                        const label = (e.data && e.data.label) || '';
                        return label === 'True' || label === 'False';
                    })
                    .map((e) => e.id),
            };
        }""")

    @staticmethod
    def _assert_branch_labels_centered(result: dict, stage: str) -> None:
        assert result["reports"], (
            f"No True/False edge labels found ({stage}). "
            f"Parsed labels: {result['parsedLabels']}, branch edges: {result['edgesWithBranchLabel']}"
        )
        for report in result["reports"]:
            assert report["actual"] is not None, (
                f"Label not found for edge {report['edge']} ({stage}). "
                f"Parsed labels: {result['parsedLabels']}"
            )
            assert report["distance"] <= 2.5, (
                f"Branch label not centered on outgoing leg for {report['edge']} ({report['label']}) [{stage}].\n"
                f"Expected: ({report['expected']['x']:.2f}, {report['expected']['y']:.2f})\n"
                f"Actual: ({report['actual']['x']:.2f}, {report['actual']['y']:.2f})\n"
                f"Distance: {report['distance']:.2f}px"
            )

    def test_branch_incoming_edge_touches_diamond_top(self, page, temp_html_file):
        """Incoming edge should terminate on the BRANCH diamond top boundary."""
        graph = make_branch_anchor_graph()
        render_to_page(page, graph, depth=0, temp_path=temp_html_file)

        result = page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;
            const branch = debug.nodes.find(n => n.id === 'branch_anchor_gate' && n.nodeType === 'BRANCH');
            if (!branch) {
                return { error: 'Branch node not found', nodes: debug.nodes.map(n => [n.id, n.nodeType]) };
            }

            const incoming = (debug.layoutedEdges || []).find(e => {
                const actualTarget = (e.data && e.data.actualTarget) || e.target;
                return actualTarget === branch.id;
            });
            if (!incoming || !incoming.data || !incoming.data.points || incoming.data.points.length < 2) {
                return { error: 'Incoming branch edge not found', edge: incoming || null };
            }

            const endPoint = incoming.data.points[incoming.data.points.length - 1];

            const viewport = document.querySelector('.react-flow__viewport');
            if (!viewport) return { error: 'Viewport not found' };
            const match = viewport.style.transform.match(/translate\\(([\\d.-]+)px,\\s*([\\d.-]+)px\\)\\s*scale\\(([\\d.-]+)\\)/);
            if (!match) return { error: 'Viewport transform not found', transform: viewport.style.transform };
            const translateY = parseFloat(match[2]);
            const zoom = parseFloat(match[3]);
            const endYScreen = endPoint.y * zoom + translateY;

            const wrapper = document.querySelector(`.react-flow__node[data-id="${branch.id}"]`);
            if (!wrapper) return { error: 'Branch wrapper not found' };
            const diamond = wrapper.querySelector('div[style*="rotate(45deg)"]');
            if (!diamond) return { error: 'Diamond element not found' };
            const diamondRect = diamond.getBoundingClientRect();

            return {
                edgeId: incoming.id,
                endYLayout: endPoint.y,
                endYScreen: endYScreen,
                diamondTop: diamondRect.top,
                deltaPx: endYScreen - diamondRect.top,
            };
        }""")

        if "error" in result:
            pytest.fail(f"Setup error: {result}")

        assert abs(result["deltaPx"]) <= 1.5, (
            "Incoming edge does not touch BRANCH top boundary.\n"
            f"Edge: {result['edgeId']}\n"
            f"Edge end (screen Y): {result['endYScreen']:.2f}px\n"
            f"Diamond top (screen Y): {result['diamondTop']:.2f}px\n"
            f"Delta: {result['deltaPx']:.2f}px"
        )

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

    def test_cross_boundary_edges_do_not_cross_visible_nodes(self, page, temp_html_file):
        """Cross-boundary rerouted edges must avoid crossing unrelated visible nodes."""
        graph = build_triple_nested_graph()
        render_to_page(page, graph, depth=1, temp_path=temp_html_file)

        result = page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;
            const nodes = debug.nodes || [];
            const edges = debug.layoutedEdges || [];
            const nodeById = {};
            for (const n of nodes) nodeById[n.id] = n;

            const segmentIntersectsRect = (ax, ay, bx, by, rect) => {
                if (ax === bx && ay === by) {
                    return ax >= rect.left && ax <= rect.right && ay >= rect.top && ay <= rect.bottom;
                }
                let t0 = 0;
                let t1 = 1;
                const dx = bx - ax;
                const dy = by - ay;
                const p = [-dx, dx, -dy, dy];
                const q = [ax - rect.left, rect.right - ax, ay - rect.top, rect.bottom - ay];
                for (let i = 0; i < 4; i += 1) {
                    const pi = p[i];
                    const qi = q[i];
                    if (pi === 0) {
                        if (qi < 0) return false;
                        continue;
                    }
                    const r = qi / pi;
                    if (pi < 0) {
                        if (r > t1) return false;
                        if (r > t0) t0 = r;
                    } else {
                        if (r < t0) return false;
                        if (r < t1) t1 = r;
                    }
                }
                return true;
            };

            const issues = [];
            for (const edge of edges) {
                const points = (edge.data && edge.data.points) || [];
                if (points.length < 2) continue;
                const actualSrc = (edge.data && edge.data.actualSource) || edge.source;
                const actualTgt = (edge.data && edge.data.actualTarget) || edge.target;

                for (const node of nodes) {
                    if (node.id === actualSrc || node.id === actualTgt) continue;
                    // Ignore container ancestors of source/target; edges are expected
                    // to live inside their owner container bounds.
                    const nodeIsSrcAncestor = actualSrc.startsWith(node.id + '/');
                    const nodeIsTgtAncestor = actualTgt.startsWith(node.id + '/');
                    if (nodeIsSrcAncestor || nodeIsTgtAncestor) continue;
                    const rect = {
                        left: node.x + 1,
                        right: node.x + node.width - 1,
                        top: node.y + 1,
                        bottom: node.y + node.height - 1,
                    };
                    if (rect.left >= rect.right || rect.top >= rect.bottom) continue;

                    for (let i = 0; i < points.length - 1; i += 1) {
                        const a = points[i];
                        const b = points[i + 1];
                        if (segmentIntersectsRect(a.x, a.y, b.x, b.y, rect)) {
                            issues.push({
                                edge: edge.id,
                                source: actualSrc,
                                target: actualTgt,
                                node: node.id,
                                segment: [a, b],
                            });
                            break;
                        }
                    }
                }
            }
            return { issues };
        }""")

        assert not result["issues"], (
            "Found edges crossing visible nodes:\n" +
            "\n".join(
                f"  - {i['edge']} ({i['source']} -> {i['target']}) crosses {i['node']}"
                for i in result["issues"][:10]
            )
        )

    def test_branch_labels_center_on_outgoing_leg(self, page, temp_html_file):
        """True/False labels should be centered on the outgoing branch leg."""
        graph = make_branch_anchor_graph()
        render_to_page(page, graph, depth=0, temp_path=temp_html_file)
        self._assert_branch_labels_centered(
            self._branch_label_reports(page),
            stage="baseline",
        )

    def test_branch_labels_recalculate_after_expand_and_mode_changes(self, page, temp_html_file):
        """True/False label centering should update after expand/collapse and mode toggles."""
        graph = make_rag_style_graph()
        render_to_page(page, graph, depth=0, temp_path=temp_html_file)

        self._assert_branch_labels_centered(
            self._branch_label_reports(page),
            stage="initial collapsed",
        )

        click_to_expand_container(page, "retrieval")
        self._assert_branch_labels_centered(
            self._branch_label_reports(page),
            stage="after expand retrieval",
        )

        version_before_modes = page.evaluate("window.__hypergraphVizDebug.version")
        page.evaluate("""() => {
            window.__hypergraphVizSetRenderOptions({
                separateOutputs: true,
                showTypes: true,
            });
        }""")
        page.wait_for_function(
            f"window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > {version_before_modes} && window.__hypergraphVizReady === true",
            timeout=10000,
        )
        self._assert_branch_labels_centered(
            self._branch_label_reports(page),
            stage="after separate outputs + show types",
        )

        click_to_collapse_container(page, "retrieval")
        self._assert_branch_labels_centered(
            self._branch_label_reports(page),
            stage="after collapse retrieval",
        )

    def test_separate_outputs_avoids_crossings_and_merges_control_with_data(self, page, temp_html_file):
        """Separate-outputs mode should avoid node/edge crossings and merge shared-target tails."""
        graph = make_separate_outputs_crossing_graph()
        render_to_page(page, graph, depth=0, temp_path=temp_html_file)

        version_before = page.evaluate("window.__hypergraphVizDebug.version")
        page.evaluate("""() => {
            window.__hypergraphVizSetRenderOptions({
                separateOutputs: true,
                showTypes: false,
            });
        }""")
        page.wait_for_function(
            f"window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > {version_before} && window.__hypergraphVizReady === true",
            timeout=10000,
        )

        result = page.evaluate("""() => {
            const debug = window.__hypergraphVizDebug;
            const edges = debug.layoutedEdges || [];
            const nodes = debug.nodes || [];

            const segmentIntersectsRect = (ax, ay, bx, by, rect) => {
                let t0 = 0;
                let t1 = 1;
                const dx = bx - ax;
                const dy = by - ay;
                const p = [-dx, dx, -dy, dy];
                const q = [ax - rect.left, rect.right - ax, ay - rect.top, rect.bottom - ay];
                for (let i = 0; i < 4; i += 1) {
                    const pi = p[i];
                    const qi = q[i];
                    if (pi === 0) {
                        if (qi < 0) return false;
                        continue;
                    }
                    const r = qi / pi;
                    if (pi < 0) {
                        if (r > t1) return false;
                        if (r > t0) t0 = r;
                    } else {
                        if (r < t0) return false;
                        if (r < t1) t1 = r;
                    }
                }
                return true;
            };

            const orient = (a, b, c) => (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
            const properSegmentsCross = (a, b, c, d) => {
                const o1 = orient(a, b, c);
                const o2 = orient(a, b, d);
                const o3 = orient(c, d, a);
                const o4 = orient(c, d, b);
                return (o1 * o2 < 0) && (o3 * o4 < 0);
            };

            const edgeNodeIssues = [];
            for (const edge of edges) {
                const points = (edge.data && edge.data.points) || [];
                if (points.length < 2) continue;
                const actualSrc = (edge.data && edge.data.actualSource) || edge.source;
                const actualTgt = (edge.data && edge.data.actualTarget) || edge.target;

                for (const node of nodes) {
                    if (node.id === actualSrc || node.id === actualTgt) continue;
                    const rect = {
                        left: node.x + 1,
                        right: node.x + node.width - 1,
                        top: node.y + 1,
                        bottom: node.y + node.height - 1,
                    };
                    if (rect.left >= rect.right || rect.top >= rect.bottom) continue;

                    for (let i = 0; i < points.length - 1; i += 1) {
                        const a = points[i];
                        const b = points[i + 1];
                        if (segmentIntersectsRect(a.x, a.y, b.x, b.y, rect)) {
                            edgeNodeIssues.push({
                                edge: edge.id,
                                source: actualSrc,
                                target: actualTgt,
                                node: node.id,
                            });
                            break;
                        }
                    }
                }
            }

            const edgeEdgeIssues = [];
            for (let i = 0; i < edges.length; i += 1) {
                const e1 = edges[i];
                const p1 = (e1.data && e1.data.points) || [];
                if (p1.length < 2) continue;
                const s1 = (e1.data && e1.data.actualSource) || e1.source;
                const t1 = (e1.data && e1.data.actualTarget) || e1.target;

                for (let j = i + 1; j < edges.length; j += 1) {
                    const e2 = edges[j];
                    const p2 = (e2.data && e2.data.points) || [];
                    if (p2.length < 2) continue;
                    const s2 = (e2.data && e2.data.actualSource) || e2.source;
                    const t2 = (e2.data && e2.data.actualTarget) || e2.target;
                    if (s1 === s2 || s1 === t2 || t1 === s2 || t1 === t2) continue;

                    let found = false;
                    for (let a = 0; a < p1.length - 1 && !found; a += 1) {
                        for (let b = 0; b < p2.length - 1 && !found; b += 1) {
                            if (properSegmentsCross(p1[a], p1[a + 1], p2[b], p2[b + 1])) {
                                edgeEdgeIssues.push({
                                    edgeA: e1.id,
                                    edgeB: e2.id,
                                    segA: [p1[a], p1[a + 1]],
                                    segB: [p2[b], p2[b + 1]],
                                });
                                found = true;
                            }
                        }
                    }
                }
            }

            const mergeIssues = [];
            const byTarget = new Map();
            for (const edge of edges) {
                const points = (edge.data && edge.data.points) || [];
                if (points.length < 2) continue;
                const actualTarget = (edge.data && edge.data.actualTarget) || edge.target;
                if (!byTarget.has(actualTarget)) byTarget.set(actualTarget, []);
                byTarget.get(actualTarget).push(edge);
            }

            byTarget.forEach((incoming, targetId) => {
                if (incoming.length < 2) return;
                const hasControl = incoming.some((e) => (e.data && e.data.edgeType) === 'control');
                const hasNonControl = incoming.some((e) => (e.data && e.data.edgeType) !== 'control');
                if (!hasControl || !hasNonControl) return;

                const tails = incoming.map((e) => {
                    const points = (e.data && e.data.points) || [];
                    return {
                        edge: e.id,
                        penultimate: points[points.length - 2],
                        end: points[points.length - 1],
                    };
                });
                const anchor = tails[0].penultimate;
                const sameTail = tails.every((t) =>
                    Math.abs(t.penultimate.x - anchor.x) <= 1.0 &&
                    Math.abs(t.penultimate.y - anchor.y) <= 1.0
                );
                if (!sameTail) {
                    mergeIssues.push({
                        target: targetId,
                        edges: tails,
                    });
                }
            });

            return { edgeNodeIssues, edgeEdgeIssues, mergeIssues };
        }""")

        assert not result["edgeNodeIssues"], (
            "Separate-outputs mode has edges crossing nodes:\n" +
            "\n".join(
                f"  - {i['edge']} ({i['source']} -> {i['target']}) crosses {i['node']}"
                for i in result["edgeNodeIssues"][:10]
            )
        )
        assert not result["edgeEdgeIssues"], (
            "Separate-outputs mode has edge/edge crossings:\n" +
            "\n".join(
                f"  - {i['edgeA']} crosses {i['edgeB']}"
                for i in result["edgeEdgeIssues"][:10]
            )
        )
        assert not result["mergeIssues"], (
            "Control and non-control edges with same target do not blend into a shared tail:\n" +
            "\n".join(
                f"  - target {m['target']} edges {[e['edge'] for e in m['edges']]}"
                for m in result["mergeIssues"][:10]
            )
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
