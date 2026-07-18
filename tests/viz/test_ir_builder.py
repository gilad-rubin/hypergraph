"""Tests for build_graph_ir — the compact IR builder.

The IR is the single source of truth for all viz frontends. It contains
pure-graph facts plus initial state, with no 2^N expansion-state
precomputation.
"""

from hypergraph.viz.ir_schema import GraphIR
from hypergraph.viz.renderer import render_graph
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from tests.viz.conftest import (
    make_hidden_source_only_dependency_graph,
    make_simple_graph,
    make_workflow,
)


def test_two_node_graph_appears_in_ir_with_connecting_edge():
    """A simple 2-node graph (a -> b) yields a typed IR where both
    function nodes are present and a data edge connects them."""
    flat_graph = make_simple_graph().to_flat_graph()

    ir = build_graph_ir(flat_graph)

    assert isinstance(ir, GraphIR)

    node_ids = {n.id for n in ir.nodes}
    assert "node_a" in node_ids
    assert "node_b" in node_ids

    edge_pairs = {(e.source, e.target) for e in ir.edges}
    assert ("node_a", "node_b") in edge_pairs


def test_function_node_records_node_type():
    """A function node's IR entry carries node_type='FUNCTION' so the
    frontend can pick the right shape."""
    flat_graph = make_simple_graph().to_flat_graph()

    ir = build_graph_ir(flat_graph)

    node_a = next(n for n in ir.nodes if n.id == "node_a")
    assert node_a.node_type == "FUNCTION"


def test_nested_graph_records_parent_relationship():
    """Children of a nested subgraph carry parent=<container id>; nodes
    at the top level carry parent=None. This is the structural fact the
    frontend needs to draw containers and route edges across nesting."""
    flat_graph = make_workflow().to_flat_graph()

    ir = build_graph_ir(flat_graph)

    by_id = {n.id: n for n in ir.nodes}
    assert by_id["preprocess/clean_text"].parent == "preprocess"
    assert by_id["preprocess/normalize_text"].parent == "preprocess"
    assert by_id["analyze"].parent is None
    assert by_id["preprocess"].parent is None


def test_expandable_nodes_lists_containers():
    """The IR exposes expandable_nodes — the set of GRAPH nodes the
    frontend can expand/collapse. A flat graph has none; a nested graph
    lists its containers."""
    flat_simple = make_simple_graph().to_flat_graph()
    flat_nested = make_workflow().to_flat_graph()

    ir_simple = build_graph_ir(flat_simple)
    ir_nested = build_graph_ir(flat_nested)

    assert ir_simple.expandable_nodes == []
    assert "preprocess" in ir_nested.expandable_nodes


def test_external_param_appears_in_ir_external_inputs():
    """An external (unsatisfied) parameter — like `x` in node_a(x: int)
    — appears as an external-input fact on the IR. Frontends use this
    to synthesize INPUT / INPUT_GROUP nodes per expansion state."""
    flat_graph = make_simple_graph().to_flat_graph()

    ir = build_graph_ir(flat_graph)

    param_names = {inp.name for inp in ir.external_inputs}
    assert "x" in param_names


def test_data_edge_records_edge_type():
    """An edge produced by data flow (a's output -> b's input) carries
    edge_type='data', so the frontend can distinguish it from ordering
    edges (which render dashed)."""
    flat_graph = make_simple_graph().to_flat_graph()

    ir = build_graph_ir(flat_graph)

    edge = next(e for e in ir.edges if e.source == "node_a" and e.target == "node_b")
    assert edge.edge_type == "data"


def test_function_node_signatures_match_render_graph_wrapper():
    """For the simple 2-node graph, the IR's function-node signatures
    (id, node_type, parent) match the scene emitted by the render_graph()
    wrapper — a wrapper-consistency check (render_graph forwards to the
    same IR pipeline), not comparison against an independent oracle."""
    flat_graph = make_simple_graph().to_flat_graph()

    ir = build_graph_ir(flat_graph)
    oracle = render_graph(flat_graph)

    ir_sigs = {(n.id, n.node_type, n.parent) for n in ir.nodes if n.node_type == "FUNCTION"}
    oracle_sigs = {(n["id"], n["data"]["nodeType"], n.get("parentId")) for n in oracle["nodes"] if n.get("data", {}).get("nodeType") == "FUNCTION"}
    assert ir_sigs == oracle_sigs


# ---------------------------------------------------------------------------
# Canonical container entrypoints (locked decision D14, #211)
# ---------------------------------------------------------------------------


def _entrypoint_fixture_ir() -> GraphIR:
    """IR for the frozen ``container_entrypoint_expanded`` baseline shape:
    a route gate targeting a container whose ``accumulate`` child self-loops."""
    from hypergraph import END, Graph, node, route

    @node(output_name="history")
    def accumulate(history: str, raw: str) -> str:
        return history + raw

    @node(output_name="status")
    def kickoff(seed: str) -> str:
        return seed

    @node(output_name="signal")
    def intake(request: str) -> str:
        return request

    @route(targets=["worker", END])
    def dispatch(signal: str) -> str:
        return "worker"

    inner = Graph(
        [accumulate, kickoff],
        name="worker",
        entrypoint=["accumulate", "kickoff"],
    )
    outer = Graph(
        [intake, dispatch, inner.as_node()],
        name="container_entrypoint",
        entrypoint="intake",
    )
    return build_graph_ir(outer.to_flat_graph())


def test_container_entrypoints_are_self_exclusive_and_keep_multiple():
    """A child's own outputs never disqualify it: the self-looping
    ``accumulate`` stays an entrypoint (first, in declared order) and the
    independent ``kickoff`` is preserved alongside it."""
    ir = _entrypoint_fixture_ir()

    assert ir.container_entrypoints == {"worker": ("worker/accumulate", "worker/kickoff")}


def test_control_edge_fallback_uses_first_canonical_entrypoint():
    """The ``target_when_expanded`` fallback for a control edge into a
    container must come from the canonical field — the #263 tripwire flip:
    ``dispatch`` re-routes to ``worker/accumulate`` (self-exclusive first
    entry), not the old self-inclusive pick ``worker/kickoff``."""
    ir = _entrypoint_fixture_ir()

    control_edge = next(e for e in ir.edges if e.source == "dispatch" and e.target == "worker")
    assert control_edge.target_when_expanded == "worker/accumulate"


def test_container_entrypoints_dependency_order_not_declaration_order():
    """A child consuming a sibling's output is not an entrypoint, even when
    declared first."""
    from hypergraph import Graph, node

    @node(output_name="done")
    def downstream(started: int) -> int:
        return started + 1

    @node(output_name="started")
    def upstream(x: int) -> int:
        return x

    inner = Graph(nodes=[downstream, upstream], name="inner")
    outer = Graph(nodes=[inner.as_node()])

    ir = build_graph_ir(outer.to_flat_graph())

    assert ir.container_entrypoints == {"inner": ("inner/upstream",)}


def test_container_entrypoints_use_empty_tuple_when_only_source_is_hidden():
    """The canonical field must not point at a node omitted from the IR."""
    graph = make_hidden_source_only_dependency_graph()

    ir = build_graph_ir(graph.to_flat_graph())

    assert ir.container_entrypoints == {"box": ()}


def test_container_entrypoints_cyclic_falls_back_to_first_child():
    """When every child consumes a sibling output (a true cycle), the canonical
    map falls back to the first declared child for a stable target."""
    from hypergraph import Graph, node

    @node(output_name="ping_out")
    def ping(pong_out: int) -> int:
        return pong_out

    @node(output_name="pong_out")
    def pong(ping_out: int) -> int:
        return ping_out

    inner = Graph(nodes=[ping, pong], name="cycle_box", entrypoint="ping")
    outer = Graph(nodes=[inner.as_node()], entrypoint="cycle_box")

    ir = build_graph_ir(outer.to_flat_graph())

    assert ir.container_entrypoints == {"cycle_box": ("cycle_box/ping",)}


def test_container_entrypoints_cover_nested_containers():
    """Every GRAPH container gets a canonical entry — including a container
    nested inside another container."""
    from hypergraph import Graph, node

    @node(output_name="core_out")
    def core(seed: int) -> int:
        return seed

    @node(output_name="wrap_out")
    def wrap(core_out: int) -> int:
        return core_out

    inner = Graph(nodes=[core], name="inner")
    middle = Graph(nodes=[inner.as_node(), wrap], name="middle")
    outer = Graph(nodes=[middle.as_node()])

    ir = build_graph_ir(outer.to_flat_graph())

    assert ir.container_entrypoints == {
        "middle": ("middle/inner",),
        "middle/inner": ("middle/inner/core",),
    }


def test_container_entrypoints_unaffected_by_boundary_renames():
    """Boundary renames (``with_inputs`` aliases) change the container's
    parent-facing port names, not the inner sibling comparison — the
    canonical derivation still compares the children's own inner names."""
    from hypergraph import Graph, node

    @node(output_name="prepared")
    def prepare(text: str) -> str:
        return text

    @node(output_name="final")
    def finish(prepared: str) -> str:
        return prepared

    inner = Graph(nodes=[prepare, finish], name="inner")
    outer = Graph(nodes=[inner.as_node().with_inputs(text="document")])

    ir = build_graph_ir(outer.to_flat_graph())

    assert ir.container_entrypoints == {"inner": ("inner/prepare",)}
