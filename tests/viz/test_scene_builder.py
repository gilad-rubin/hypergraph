"""Tests for scene_builder — derives a React Flow scene from the IR.

This is the Python reference implementation that will be ported to JS
(assets/scene_builder.js). Both implementations must produce
semantically equivalent scenes for the same IR.
"""

from hypergraph.viz.renderer import render_graph
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from hypergraph.viz.scene_builder import build_initial_scene
from tests.viz.conftest import make_outer, make_simple_graph, make_workflow


def _visible_node_sigs(scene_nodes: list[dict]) -> set[tuple[str, str]]:
    """Project visible nodes to (id, nodeType) signatures.

    Hidden nodes (e.g., DATA nodes when separate_outputs=False) don't
    render and aren't part of the visual parity contract.
    """
    return {(n["id"], n["data"]["nodeType"]) for n in scene_nodes if not n.get("hidden")}


def _visible_edge_sigs(scene: dict) -> set[tuple[str, str, str]]:
    """Edges where both endpoints are visible. Projects to (source, target, edgeType)."""
    visible_ids = {n["id"] for n in scene["nodes"] if not n.get("hidden")}
    return {
        (e["source"], e["target"], e["data"]["edgeType"])
        for e in scene["edges"]
        if e["source"] in visible_ids and e["target"] in visible_ids and not e.get("hidden")
    }


def test_scene_materializes_input_node_for_external_param():
    """An external param recorded in IR.external_inputs becomes a
    synthetic INPUT scene node with id `input_<name>`."""
    flat_graph = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir)

    input_ids = {n["id"] for n in scene["nodes"] if n["data"]["nodeType"] == "INPUT"}
    assert "input_x" in input_ids


def test_simple_graph_scene_node_signatures_match_legacy():
    """Comprehensive parity: scene_builder output for make_simple_graph
    has the same set of (id, nodeType) signatures as the legacy
    render_graph. This is the contract that lets us swap the legacy
    path for the IR + scene_builder path without visual regressions."""
    flat_graph = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)

    assert _visible_node_sigs(scene["nodes"]) == _visible_node_sigs(oracle["nodes"])


def test_workflow_scene_node_signatures_match_legacy():
    """Parity for a 1-level nested graph (preprocess[clean, normalize] -> analyze)."""
    flat_graph = make_workflow().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)

    assert _visible_node_sigs(scene["nodes"]) == _visible_node_sigs(oracle["nodes"])


def test_outer_scene_node_signatures_match_legacy():
    """Parity for a 2-level nested graph."""
    flat_graph = make_outer().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)

    assert _visible_node_sigs(scene["nodes"]) == _visible_node_sigs(oracle["nodes"])


def test_simple_graph_scene_edges_match_legacy():
    """Edge parity for the simple 2-node graph: scene has the same set
    of (source, target, edgeType) as the legacy renderer for visible
    edges."""
    flat_graph = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)

    assert _visible_edge_sigs(scene) == _visible_edge_sigs(oracle)


def test_workflow_scene_edges_match_legacy():
    flat_graph = make_workflow().to_flat_graph()
    ir = build_graph_ir(flat_graph)
    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)
    assert _visible_edge_sigs(scene) == _visible_edge_sigs(oracle)


def test_outer_scene_edges_match_legacy():
    flat_graph = make_outer().to_flat_graph()
    ir = build_graph_ir(flat_graph)
    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)
    assert _visible_edge_sigs(scene) == _visible_edge_sigs(oracle)
