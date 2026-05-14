"""Visualization coverage for GraphNode boundary projection."""

from hypergraph import Graph, node
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from hypergraph.viz.scene_builder import build_initial_scene


def test_namespaced_graphnode_input_renders_resolved_address_and_routes_to_leaf():
    @node(output_name="docs")
    def retrieve(query: str) -> str:
        return f"docs:{query}"

    graph = Graph([Graph([retrieve], name="retrieval").as_node(namespaced=True)])
    ir = build_graph_ir(graph.to_flat_graph())

    external = ir.external_inputs[0]
    assert external.params == ("retrieval.query",)
    assert external.consumers == ("retrieval/retrieve",)
    assert external.deepest_owner == "retrieval"

    scene = build_initial_scene(ir, expansion_state={"retrieval": True})
    input_node = next(n for n in scene["nodes"] if n["id"] == "input_query")
    input_edges = [e for e in scene["edges"] if e["source"] == "input_query" and not e.get("hidden")]

    assert input_node["data"]["label"] == "retrieval.query"
    assert input_node["data"]["actualTargets"] == ["retrieval/retrieve"]
    assert {edge["target"] for edge in input_edges} == {"retrieval/retrieve"}


def test_exposed_shared_input_renders_once_at_parent_boundary():
    @node(output_name="docs")
    def retrieve(query: str) -> str:
        return f"docs:{query}"

    @node(output_name="answer")
    def generate(query: str) -> str:
        return f"answer:{query}"

    graph = Graph(
        [
            Graph([retrieve], name="retrieval").as_node(namespaced=True).expose("query"),
            Graph([generate], name="generation").as_node(namespaced=True).expose("query"),
        ]
    )
    ir = build_graph_ir(graph.to_flat_graph())

    assert len(ir.external_inputs) == 1
    external = ir.external_inputs[0]
    assert external.params == ("query",)
    assert set(external.consumers) == {"retrieval/retrieve", "generation/generate"}
    assert external.deepest_owner is None

    scene = build_initial_scene(ir, expansion_state={"retrieval": True, "generation": True})
    input_nodes = [n for n in scene["nodes"] if n["data"]["nodeType"] == "INPUT"]
    input_edges = [e for e in scene["edges"] if e["source"] == "input_query" and not e.get("hidden")]

    assert [(n["id"], n["data"]["label"]) for n in input_nodes] == [("input_query", "query")]
    assert {edge["target"] for edge in input_edges} == {"retrieval/retrieve", "generation/generate"}


def test_mermaid_namespaced_input_label_uses_resolved_port_address():
    @node(output_name="docs")
    def retrieve(query: str) -> str:
        return f"docs:{query}"

    graph = Graph([Graph([retrieve], name="retrieval").as_node(namespaced=True)])

    mermaid = graph.to_mermaid(depth=1)

    assert 'input_query(["retrieval.query: str"])' in mermaid
