"""Tests for scene_builder — derives a React Flow scene from the IR.

This is the Python reference implementation that will be ported to JS
(assets/scene_builder.js). Both implementations must produce
semantically equivalent scenes for the same IR.
"""

from hypergraph import Graph, node
from hypergraph.viz.ir_schema import GraphIR, IRNode
from hypergraph.viz.renderer import render_graph
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from hypergraph.viz.scene_builder import build_initial_scene
from tests.viz.conftest import (
    make_hidden_only_container_graph,
    make_hidden_source_only_dependency_graph,
    make_nested_container_entrypoint_graph,
    make_outer,
    make_simple_graph,
    make_workflow,
)


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


def test_simple_graph_scene_node_signatures_match_render_graph_wrapper():
    """Wrapper consistency for make_simple_graph: building the scene
    directly (build_graph_ir + build_initial_scene) yields the same set
    of visible (id, nodeType) signatures as going through the
    render_graph() convenience wrapper, which forwards to the same IR
    pipeline but computes its own default expansion state and flag
    plumbing. Not an independent oracle."""
    flat_graph = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)

    assert _visible_node_sigs(scene["nodes"]) == _visible_node_sigs(oracle["nodes"])


def test_workflow_scene_node_signatures_match_render_graph_wrapper():
    """Wrapper consistency for a 1-level nested graph (preprocess[clean, normalize] -> analyze)."""
    flat_graph = make_workflow().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)

    assert _visible_node_sigs(scene["nodes"]) == _visible_node_sigs(oracle["nodes"])


def test_outer_scene_node_signatures_match_render_graph_wrapper():
    """Wrapper consistency for a 2-level nested graph."""
    flat_graph = make_outer().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)

    assert _visible_node_sigs(scene["nodes"]) == _visible_node_sigs(oracle["nodes"])


def test_simple_graph_scene_edges_match_render_graph_wrapper():
    """Edge wrapper consistency for the simple 2-node graph: the directly
    built scene has the same set of visible (source, target, edgeType)
    signatures as the render_graph() wrapper output."""
    flat_graph = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)

    assert _visible_edge_sigs(scene) == _visible_edge_sigs(oracle)


def test_workflow_scene_edges_match_render_graph_wrapper():
    flat_graph = make_workflow().to_flat_graph()
    ir = build_graph_ir(flat_graph)
    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)
    assert _visible_edge_sigs(scene) == _visible_edge_sigs(oracle)


def test_outer_scene_edges_match_render_graph_wrapper():
    flat_graph = make_outer().to_flat_graph()
    ir = build_graph_ir(flat_graph)
    scene = build_initial_scene(ir)
    oracle = render_graph(flat_graph)
    assert _visible_edge_sigs(scene) == _visible_edge_sigs(oracle)


def test_show_inputs_false_hides_all_input_nodes():
    """When the user toggles 'show inputs' off, every INPUT/INPUT_GROUP
    scene node should be hidden."""
    flat_graph = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir, show_inputs=False)

    visible_inputs = [n for n in scene["nodes"] if n["data"]["nodeType"] in ("INPUT", "INPUT_GROUP") and not n.get("hidden")]
    assert visible_inputs == []


def test_separate_outputs_true_materializes_data_nodes():
    """separate_outputs=True should make every function output a visible
    DATA scene node, and the directly built scene must agree with the
    render_graph() wrapper output."""
    flat_graph = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir, separate_outputs=True)
    oracle = render_graph(flat_graph, separate_outputs=True)

    visible_scene_data = {n["id"] for n in scene["nodes"] if n["data"]["nodeType"] == "DATA" and not n.get("hidden")}
    visible_oracle_data = {n["id"] for n in oracle["nodes"] if n["data"].get("nodeType") == "DATA" and not n.get("hidden")}

    assert visible_scene_data == visible_oracle_data


def test_multi_param_consumer_yields_single_input_group():
    """When a single consumer takes multiple external params, they group
    into one INPUT_GROUP scene node so labels/handles line up — and the
    directly built scene must agree with the render_graph() wrapper."""
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


def test_separate_outputs_true_edges_match_render_graph_wrapper():
    """In separate_outputs mode, data flows producer -> DATA -> consumer.
    Edge signatures from the directly built scene should match the
    render_graph() wrapper output."""
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


def test_multi_value_edge_emits_single_scene_edge_in_merged_mode():
    """Merged-output mode should render one visible edge per producer/consumer
    pair even when that edge carries multiple values."""
    flat_graph = _make_multi_value_graph().to_flat_graph()
    ir = build_graph_ir(flat_graph)

    scene = build_initial_scene(ir, separate_outputs=False)

    split_to_merge = [e for e in scene["edges"] if e["source"] == "split" and e["target"] == "merge"]
    assert len(split_to_merge) == 1
    assert split_to_merge[0]["id"] == "split__merge"
    assert split_to_merge[0]["data"]["valueName"] is None


def test_branch_to_end_edge_carries_label_for_when_false():
    """``@ifelse(when_false=END)`` produces a synthetic branch→__end__
    edge. The label ("True"/"False") that identifies which arm exits
    must be preserved so the rendered stop path is readable."""
    from hypergraph import END, ifelse

    @node(output_name="value")
    def src(seed: int) -> int:
        return seed

    @ifelse(when_true="accept", when_false=END)
    def gate(value: int) -> bool:
        return value > 0

    @node(output_name="accepted")
    def accept(value: int) -> int:
        return value

    flat_graph = Graph(nodes=[src, gate, accept]).to_flat_graph()
    ir = build_graph_ir(flat_graph)
    scene = build_initial_scene(ir)

    end_edges = [e for e in scene["edges"] if e["target"] == "__end__"]
    assert end_edges, "branch→__end__ edge missing from scene"
    assert end_edges[0]["data"].get("label") == "False", f"Expected branch→__end__ edge label 'False'; got {end_edges[0]['data'].get('label')!r}"


def test_start_edge_to_expanded_container_targets_real_entrypoint():
    """Expanded START routing should follow dependency entrypoints, not child order."""

    @node(output_name="done")
    def downstream(started: int) -> int:
        return started + 1

    @node(output_name="started")
    def upstream(x: int) -> int:
        return x

    inner = Graph(nodes=[downstream, upstream], name="inner")
    outer = Graph(nodes=[inner.as_node()], entrypoint="inner")

    ir = build_graph_ir(outer.to_flat_graph())
    scene = build_initial_scene(ir, expansion_state={"inner": True})

    start_edges = [e for e in scene["edges"] if e["source"] == "__start__"]
    assert [(e["source"], e["target"]) for e in start_edges] == [("__start__", "inner/upstream")]


def test_expanded_container_entrypoints_ignore_self_outputs_and_keep_multiple_entrypoints():
    """End-to-end through the canonical ``GraphIR.container_entrypoints``
    field (D14, #211): a self-loop child stays an entrypoint (self-EXCLUSIVE
    rule) and independent entrypoints are all preserved."""

    @node(output_name="loop")
    def selfish(loop: str) -> str:
        return loop

    @node(output_name="y")
    def independent(x: str) -> str:
        return x

    @node(output_name="z")
    def downstream(y: str) -> str:
        return y

    # The self-loop makes the inner graph cyclic — execution entrypoints are
    # required at construction (both kept active, mirroring the frozen
    # container_entrypoint_expanded baseline case).
    inner = Graph(
        nodes=[selfish, independent, downstream],
        name="outer",
        entrypoint=["selfish", "independent"],
    )
    outer = Graph(nodes=[inner.as_node()], entrypoint="outer")

    ir = build_graph_ir(outer.to_flat_graph())
    assert ir.container_entrypoints == {"outer": ("outer/selfish", "outer/independent")}

    scene = build_initial_scene(ir, expansion_state={"outer": True})

    start_targets = {edge["target"] for edge in scene["edges"] if edge["source"] == "__start__"}
    assert start_targets == {"outer/selfish", "outer/independent"}


def test_scene_builder_consumes_container_entrypoints_field_verbatim():
    """The scene builder must consume ``GraphIR.container_entrypoints``, not
    re-derive it. The hand-built IR is shaped so any re-derivation from node
    inputs/outputs would pick 'outer/upstream'; the field deliberately says
    'outer/downstream'. If this fails with 'outer/upstream', a duplicate
    derivation crept back into scene_builder.py — delete it (#211)."""
    ir = GraphIR(
        nodes=[
            IRNode(id="outer", node_type="GRAPH"),
            IRNode(
                id="outer/downstream",
                node_type="FUNCTION",
                parent="outer",
                inputs=({"name": "started"},),
                outputs=({"name": "done"},),
            ),
            IRNode(
                id="outer/upstream",
                node_type="FUNCTION",
                parent="outer",
                inputs=({"name": "x"},),
                outputs=({"name": "started"},),
            ),
        ],
        configured_entrypoints=("outer",),
        container_entrypoints={"outer": ("outer/downstream",)},
    )

    scene = build_initial_scene(ir, expansion_state={"outer": True})

    start_targets = {edge["target"] for edge in scene["edges"] if edge["source"] == "__start__"}
    assert start_targets == {"outer/downstream"}


def test_edge_into_nested_expanded_container_targets_visible_leaf():
    """An edge may never terminate on an expanded compound parent."""
    graph = make_nested_container_entrypoint_graph()
    ir = build_graph_ir(graph.to_flat_graph())

    scene = build_initial_scene(
        ir,
        expansion_state={"mid": True, "mid/accum": True},
    )

    dispatch_edges = [edge for edge in scene["edges"] if edge["source"] == "dispatch" and edge["target"] != "__end__"]
    assert [(edge["source"], edge["target"]) for edge in dispatch_edges] == [("dispatch", "mid/accum/acc_step")]


def test_nested_expansion_preserves_every_start_entrypoint():
    """Resolving one nested entrypoint must not drop its plain sibling."""
    graph = make_nested_container_entrypoint_graph().with_entrypoint("mid")
    ir = build_graph_ir(graph.to_flat_graph())

    scene = build_initial_scene(
        ir,
        expansion_state={"mid": True, "mid/accum": True},
    )

    start_targets = {edge["target"] for edge in scene["edges"] if edge["source"] == "__start__" and edge["target"].startswith("mid/")}
    assert start_targets == {"mid/accum/acc_step", "mid/starter"}


def test_edge_into_all_hidden_container_is_not_attached_to_expanded_hull():
    """An expanded container with no visible child must remain layoutable."""
    graph = make_hidden_only_container_graph()
    ir = build_graph_ir(graph.to_flat_graph())

    scene = build_initial_scene(ir, expansion_state={"box": True})

    visible_box_edges = [edge for edge in scene["edges"] if not edge["hidden"] and edge["target"] == "box"]
    assert visible_box_edges == []


def test_hidden_source_does_not_promote_visible_dependent_to_entrypoint():
    """A hidden dependency source remains the structural entrypoint."""
    graph = make_hidden_source_only_dependency_graph()
    ir = build_graph_ir(graph.to_flat_graph())

    scene = build_initial_scene(ir, expansion_state={"box": True})

    visible_dispatch_targets = {
        edge["target"] for edge in scene["edges"] if edge["source"] == "dispatch" and edge["target"] != "__end__" and not edge["hidden"]
    }
    assert visible_dispatch_targets == set()


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
