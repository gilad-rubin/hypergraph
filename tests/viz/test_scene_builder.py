"""Tests for scene_builder — derives a React Flow scene from the IR.

This is the Python reference implementation that will be ported to JS
(assets/scene_builder.js). Both implementations must produce
semantically equivalent scenes for the same IR.
"""

from hypergraph import Graph, node
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


def test_show_inputs_false_hides_all_input_nodes():
    """When the user toggles 'show inputs' off, every INPUT/INPUT_GROUP
    scene node should be hidden — matching the legacy renderer's
    behavior when show_inputs=False."""
    flat_graph = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir, show_inputs=False)

    visible_inputs = [n for n in scene["nodes"] if n["data"]["nodeType"] in ("INPUT", "INPUT_GROUP") and not n.get("hidden")]
    assert visible_inputs == []


def test_separate_outputs_true_materializes_data_nodes():
    """separate_outputs=True should make every function output a visible
    DATA scene node — the legacy contract for the 'separate outputs'
    visualization mode."""
    flat_graph = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir, separate_outputs=True)
    oracle = render_graph(flat_graph, separate_outputs=True)

    visible_scene_data = {n["id"] for n in scene["nodes"] if n["data"]["nodeType"] == "DATA" and not n.get("hidden")}
    visible_oracle_data = {n["id"] for n in oracle["nodes"] if n["data"].get("nodeType") == "DATA" and not n.get("hidden")}

    assert visible_scene_data == visible_oracle_data


def test_multi_param_consumer_yields_single_input_group():
    """When a single consumer takes multiple external params, the legacy
    renderer groups them into one INPUT_GROUP scene node — the IR path
    must do the same so labels/handles line up."""
    from hypergraph import Graph, node

    @node(output_name="out")
    def two_param(alpha: int, beta: int) -> int:
        return alpha + beta

    flat_graph = Graph([two_param]).to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)

    scene_inputs = {
        (n["id"], n["data"]["nodeType"]) for n in scene["nodes"] if n["data"]["nodeType"] in ("INPUT", "INPUT_GROUP") and not n.get("hidden")
    }
    oracle_inputs = {
        (n["id"], n["data"].get("nodeType")) for n in oracle["nodes"] if n["data"].get("nodeType") in ("INPUT", "INPUT_GROUP") and not n.get("hidden")
    }

    assert scene_inputs == oracle_inputs


def test_separate_outputs_true_edges_match_legacy():
    """In separate_outputs mode, data flows producer -> DATA -> consumer.
    Edge signatures should match the legacy renderer's."""
    flat_graph = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir, separate_outputs=True)
    oracle = render_graph(flat_graph, separate_outputs=True)

    assert _visible_edge_sigs(scene) == _visible_edge_sigs(oracle)


def _make_multi_value_graph() -> Graph:
    """A producer with two outputs feeds a single consumer that needs both —
    yields one NetworkX edge whose ``value_names`` is ``["a", "b"]``."""

    @node(output_name=("a", "b"))
    def split(x: int) -> tuple[int, int]:
        return 1, 2

    @node(output_name="r")
    def merge(a: int, b: int) -> int:
        return a + b

    return Graph(nodes=[split, merge])


def test_multi_value_edge_emits_one_scene_edge_per_value_merged_mode():
    """A data edge with value_names=("a","b") must produce two scene
    edges so each value renders as a labeled connection. Mirrors the
    legacy renderer (one edge per value)."""
    flat_graph = _make_multi_value_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir, separate_outputs=False)

    split_to_merge = [e for e in scene["edges"] if e["source"] == "split" and e["target"] == "merge"]
    value_names = {e["data"]["valueName"] for e in split_to_merge}
    assert value_names == {"a", "b"}, f"Expected one edge per value name; got {value_names}"


def test_expanded_graph_container_data_nodes_are_hidden_in_separate_mode():
    """When a GRAPH container is expanded in separate_outputs mode, the
    data edge is re-routed to the internal producer's DATA node, leaving
    the container-level DATA node disconnected. It must be hidden so it
    doesn't render as an orphan duplicate."""
    flat_graph = make_workflow().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(
        ir,
        expansion_state={"preprocess": True},
        separate_outputs=True,
    )

    container_data = [n for n in scene["nodes"] if n["id"].startswith("data_preprocess_") and n["data"]["nodeType"] == "DATA"]
    assert container_data, "preprocess container DATA nodes missing from scene"
    assert all(n["hidden"] for n in container_data), (
        f"Expanded-container DATA nodes must be hidden; got visible: {[n['id'] for n in container_data if not n['hidden']]}"
    )


def test_branch_emit_output_connects_to_its_data_node_in_separate_mode():
    """A BRANCH gate with ``emit=...`` produces a DATA node for the
    emitted value; the producer→DATA "output" edge must be emitted so
    the DATA node isn't a visible orphan."""
    from hypergraph import ifelse

    @node(output_name="value")
    def src(seed: int) -> int:
        return seed

    @ifelse(when_true="accept", when_false="reject", emit="decision_made")
    def gate(value: int) -> bool:
        return value > 0

    @node(output_name="accepted")
    def accept(value: int) -> int:
        return value

    @node(output_name="rejected")
    def reject(value: int) -> int:
        return value

    flat_graph = Graph(nodes=[src, gate, accept, reject]).to_flat_graph()
    ir = build_graph_ir(flat_graph)
    scene = build_initial_scene(ir, separate_outputs=True)

    output_edges_from_gate = [e for e in scene["edges"] if e["source"] == "gate" and e["data"].get("edgeType") == "output"]
    targets = {e["target"] for e in output_edges_from_gate}
    assert "data_gate_decision_made" in targets, f"BRANCH gate emit-output DATA node has no producer edge; output edges from gate: {targets}"


def test_multi_value_edge_routes_through_per_value_data_nodes_in_separate_mode():
    """In separate_outputs mode every value_name routes through its own
    DATA node. A producer with outputs ("a","b") feeding a consumer must
    produce two distinct producer→DATA→consumer paths."""
    flat_graph = _make_multi_value_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir, separate_outputs=True)

    edges_to_merge = [e for e in scene["edges"] if e["target"] == "merge"]
    sources_to_merge = {e["source"] for e in edges_to_merge}
    assert sources_to_merge == {"data_split_a", "data_split_b"}, f"Both per-value DATA nodes must connect to the consumer; got {sources_to_merge}"
