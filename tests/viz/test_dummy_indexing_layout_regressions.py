"""Regression tests for a synthetic nested indexing visualization.

This file intentionally uses only dummy node/graph names and synthetic logic.
It mirrors a real-world layout pattern with:
- input fan-in
- if/else branching
- a nested processing subgraph
"""

from __future__ import annotations

import pytest

from hypergraph import Graph, ifelse, node
from hypergraph.viz.renderer import render_graph
from tests.viz.conftest import HAS_PLAYWRIGHT, render_to_page


@node(output_name="doc_exists")
def check_document_exists(doc_id: str, vector_store: object) -> bool:
    return bool(doc_id and vector_store)


@ifelse(when_true="process_document", when_false="skip_document")
def should_process(doc_exists: bool, overwrite: bool) -> bool:
    return (not doc_exists) or overwrite


@node(output_name="index_result")
def skip_document(doc_id: str) -> dict[str, str]:
    return {"status": "skipped", "doc_id": doc_id}


@node(output_name="pdf_path")
def locate_pdf(doc_id: str) -> str:
    return f"/tmp/{doc_id}.pdf"


@node(output_name="pdf_bytes")
def load_pdf_file(pdf_path: str) -> bytes:
    return pdf_path.encode("utf-8")


@node(output_name="chunks")
def split_pdf(pdf_bytes: bytes) -> list[str]:
    return [pdf_bytes.decode("utf-8")]


@node(output_name="index_result")
def write_index(chunks: list[str], vector_store: object) -> dict[str, int]:
    return {"status": 1 if vector_store else 0, "count": len(chunks)}


def make_dummy_indexing_graph() -> Graph:
    process_document_graph = Graph(
        nodes=[locate_pdf, load_pdf_file, split_pdf, write_index],
        name="process_document",
    )
    return Graph(
        nodes=[
            check_document_exists,
            should_process,
            process_document_graph.as_node(),
            skip_document,
        ],
        name="indexing_workflow",
    )


def _bbox_overlap(a: dict, b: dict) -> bool:
    return not (
        a["x"] + a["width"] <= b["x"]
        or b["x"] + b["width"] <= a["x"]
        or a["y"] + a["height"] <= b["y"]
        or b["y"] + b["height"] <= a["y"]
    )


def test_collapsed_nested_process_node_exposes_outputs() -> None:
    """Collapsed GRAPH node should still expose terminal outputs."""
    graph = make_dummy_indexing_graph()
    result = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=False)
    process_node = next(n for n in result["nodes"] if n["id"] == "process_document")
    output_names = {o["name"] for o in process_node["data"].get("outputs", [])}

    assert "index_result" in output_names, (
        "Collapsed nested process node should show at least its terminal output "
        "'index_result' so branch outcomes are visible."
    )


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_false_branch_label_sits_on_path(page, temp_html_file) -> None:
    """False label should be anchored on the corresponding edge path."""
    graph = make_dummy_indexing_graph()
    render_to_page(page, graph, depth=0, temp_path=temp_html_file)

    result = page.evaluate(
        """() => {
            const falseLabel = Array.from(document.querySelectorAll('div'))
                .find(el => (el.textContent || '').trim() === 'False');
            if (!falseLabel) {
                return { error: 'False label not found' };
            }

            const svg = document.querySelector('.react-flow__edges');
            if (!svg) {
                return { error: 'Edge SVG not found' };
            }

            const rect = falseLabel.getBoundingClientRect();
            const svgRect = svg.getBoundingClientRect();
            const lx = rect.left + rect.width / 2 - svgRect.left;
            const ly = rect.top + rect.height / 2 - svgRect.top;

            const paths = Array.from(document.querySelectorAll('.react-flow__edge path.react-flow__edge-path'));
            let minDist = Infinity;
            paths.forEach(path => {
                const len = path.getTotalLength ? path.getTotalLength() : 0;
                if (!len) return;
                const steps = 120;
                for (let i = 0; i <= steps; i++) {
                    const p = path.getPointAtLength((i / steps) * len);
                    const dx = p.x - lx;
                    const dy = p.y - ly;
                    const d = Math.sqrt(dx * dx + dy * dy);
                    if (d < minDist) minDist = d;
                }
            });

            return { minDist };
        }"""
    )

    if "error" in result:
        pytest.fail(f"Setup error: {result}")

    assert result["minDist"] <= 8, (
        f"False label is detached from edge path (distance={result['minDist']:.1f}px)."
    )


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_critical_inputs_not_far_left_of_primary_targets(page, temp_html_file) -> None:
    """Inputs should stay reasonably aligned with their downstream targets."""
    graph = make_dummy_indexing_graph()
    render_to_page(page, graph, depth=0, temp_path=temp_html_file)

    result = page.evaluate(
        """() => {
            const debug = window.__hypergraphVizDebug;
            const byId = Object.fromEntries((debug.nodes || []).map(n => [n.id, n]));

            function centerX(id) {
                const n = byId[id];
                return n ? n.x + n.width / 2 : null;
            }

            function targetCentersForInput(inputId) {
                const edges = (debug.edges || []).filter(e => e.source === inputId);
                const centers = [];
                edges.forEach(e => {
                    const targetId = (e.data && e.data.actualTarget) || e.target;
                    const cx = centerX(targetId);
                    if (cx !== null) centers.push(cx);
                });
                return centers;
            }

            const checks = {};
            ['input_overwrite', 'input_vector_store'].forEach(inputId => {
                const cx = centerX(inputId);
                const targets = targetCentersForInput(inputId);
                const avg = targets.length ? targets.reduce((a, b) => a + b, 0) / targets.length : null;
                checks[inputId] = {
                    inputCenterX: cx,
                    targetAvgCenterX: avg,
                    absDelta: (cx !== null && avg !== null) ? Math.abs(cx - avg) : null,
                };
            });
            return checks;
        }"""
    )

    for input_id in ("input_overwrite", "input_vector_store"):
        assert result[input_id]["absDelta"] is not None, f"Missing alignment data for {input_id}"
        assert result[input_id]["absDelta"] <= 100, (
            f"{input_id} is too far from its primary consumer area: "
            f"delta={result[input_id]['absDelta']:.1f}px"
        )


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_collapsed_input_edges_do_not_cross_unrelated_nodes(page, temp_html_file) -> None:
    """Collapsed view should avoid input edges cutting through unrelated function nodes."""
    graph = make_dummy_indexing_graph()
    render_to_page(page, graph, depth=0, temp_path=temp_html_file)

    result = page.evaluate(
        """() => {
            const debug = window.__hypergraphVizDebug;
            const nodes = debug.nodes || [];
                const edges = debug.edges || [];
                const byId = new Map(nodes.map(n => [n.id, n]));
                const sourceToTargets = new Map();
                edges.forEach(e => {
                    const src = (e.data && e.data.actualSource) || e.source;
                    const tgt = (e.data && e.data.actualTarget) || e.target;
                    if (!sourceToTargets.has(src)) sourceToTargets.set(src, new Set());
                    sourceToTargets.get(src).add(tgt);
                });
                const edgeGroups = Array.from(document.querySelectorAll('.react-flow__edge'));

            function containsPoint(rect, x, y) {
                return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
            }

            const overlaps = [];
            edgeGroups.forEach(group => {
                const testId = group.getAttribute('data-testid') || '';
                const path = group.querySelector('path.react-flow__edge-path');
                if (!path || !path.getTotalLength) return;

                const edge = edges.find(e => testId.includes(e.id));
                if (!edge) return;
                if (!(edge.source || '').startsWith('input_')) return;

                const sourceId = (edge.data && edge.data.actualSource) || edge.source;
                const targetId = (edge.data && edge.data.actualTarget) || edge.target;
                const len = path.getTotalLength();
                const steps = 120;
                for (let i = 6; i < steps - 6; i++) {
                    const p = path.getPointAtLength((i / steps) * len);
                        for (const n of nodes) {
                            if (n.id === sourceId || n.id === targetId) continue;
                                if (n.nodeType === 'INPUT' || n.nodeType === 'INPUT_GROUP' || n.nodeType === 'BRANCH') continue;
                            const directTargets = sourceToTargets.get(sourceId);
                            if (directTargets && directTargets.has(n.id)) continue;
                            const rect = {
                                left: n.x,
                                right: n.x + n.width,
                            top: n.y,
                            bottom: n.y + n.height,
                        };
                        if (containsPoint(rect, p.x, p.y)) {
                            overlaps.push({
                                edge: edge.id,
                                source: sourceId,
                                target: targetId,
                                node: n.id,
                            });
                            return;
                        }
                    }
                }
            });

            return overlaps;
        }"""
    )

    assert not result, f"Input edges crossing unrelated nodes: {result}"


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_expanded_skip_node_does_not_overlap_process_container(page, temp_html_file) -> None:
    """Expanded view should keep skip node spatially separate from the process container."""
    graph = make_dummy_indexing_graph()
    render_to_page(page, graph, depth=1, temp_path=temp_html_file)

    nodes = page.evaluate(
        """() => {
            const debug = window.__hypergraphVizDebug;
            const byId = Object.fromEntries((debug.nodes || []).map(n => [n.id, n]));
            return {
                process: byId['process_document'] || null,
                skip: byId['skip_document'] || null,
            };
        }"""
    )

    assert nodes["process"] and nodes["skip"], f"Missing required nodes: {nodes}"
    assert not _bbox_overlap(nodes["process"], nodes["skip"]), (
        "Expanded layout overlaps root-level nodes 'process_document' and 'skip_document'."
    )
