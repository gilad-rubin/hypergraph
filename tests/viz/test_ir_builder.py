"""Tests for build_graph_ir — the compact IR builder.

The IR is the single source of truth for all viz frontends. It contains
pure-graph facts plus initial state, with no 2^N expansion-state
precomputation.
"""

from hypergraph.viz.ir_schema import GraphIR
from hypergraph.viz.renderer import render_graph
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from tests.viz.conftest import make_simple_graph, make_workflow


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


def test_function_node_signatures_match_legacy_renderer():
    """First parity slice: for the simple 2-node graph, the IR's
    function-node signatures (id, node_type, parent) match the legacy
    renderer's. This is the contract that lets a frontend built on the
    IR produce the same scene as today."""
    flat_graph = make_simple_graph().to_flat_graph()

    ir = build_graph_ir(flat_graph)
    oracle = render_graph(flat_graph)

    ir_sigs = {(n.id, n.node_type, n.parent) for n in ir.nodes if n.node_type == "FUNCTION"}
    oracle_sigs = {(n["id"], n["data"]["nodeType"], n.get("parentId")) for n in oracle["nodes"] if n.get("data", {}).get("nodeType") == "FUNCTION"}
    assert ir_sigs == oracle_sigs
