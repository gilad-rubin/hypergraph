"""Regression tests for expanded mapped GraphNode boundaries.

A ``.map_over(...)`` GraphNode renames params at its boundary: the outer
graph wires the list param (``pages``) while the inner node consumes the
per-item param (``page``), and ``with_outputs(item_out="generated")`` renames
the collected output. The IR must re-route edges to the inner
consumer/producer when the container is expanded — an edge incident to an
expanded container becomes a dagre *compound parent* endpoint, which crashes
layout with "Cannot set properties of undefined (setting 'rank')" and renders
a blank canvas.

https://github.com/dagrejs/dagre: edges between a cluster (compound parent)
and any node are unsupported; ranking drops cluster nodes but keeps their
edges, dereferencing a label-less endpoint.
"""

import pytest

from hypergraph import Graph, ifelse, node
from tests.viz.conftest import scene_for_state


@node(output_name="exist")
def check(store, n: int) -> bool:
    return False


@ifelse(when_true="create_items", when_false="load_items")
def gate(exist: bool) -> bool:
    return exist


@node(output_name="items")
def load_items(store) -> list[str]:
    return []


@node(output_name="item_out")
def process(worker, page: str) -> str:
    return page


@node(output_name="items")
def save(store, generated: list[str]) -> list[str]:
    return generated


def make_mapped_gate_graph() -> Graph:
    """Gate routing into a mapped GraphNode whose boundary renames params."""
    create = (
        Graph(nodes=[process], name="process")
        .as_node(name="create_items")
        .with_inputs(page="pages")
        .with_outputs(item_out="generated")
        .map_over("pages")
    )
    return Graph(
        nodes=[check, gate, load_items, create, save],
        name="ensure_items",
    ).bind(store=object(), worker=object())


def assert_scene_layoutable(scene: dict) -> None:
    """Assert the scene can be handed to dagre without crashing.

    Every visible edge must reference visible nodes, and no endpoint may be
    an *expanded* container — expanded containers become dagre compound
    parents, and dagre does not support edges incident to compound parents.
    """
    nodes_by_id = {n["id"]: n for n in scene["nodes"]}
    for edge in scene["edges"]:
        if edge["hidden"]:
            continue
        for end in ("source", "target"):
            node_id = edge[end]
            assert node_id in nodes_by_id, f"edge {edge['id']} {end} references missing node {node_id!r}"
            scene_node = nodes_by_id[node_id]
            assert not scene_node["hidden"], f"edge {edge['id']} {end} references hidden node {node_id!r}"
            data = scene_node["data"]
            assert not (data["nodeType"] == "PIPELINE" and data.get("isExpanded")), (
                f"edge {edge['id']} {end} references expanded container {node_id!r} (dagre compound-parent crash)"
            )


@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("show_inputs", [False, True])
@pytest.mark.parametrize("separate_outputs", [False, True])
def test_mapped_graphnode_scenes_are_layoutable(expanded, show_inputs, separate_outputs):
    scene = scene_for_state(
        make_mapped_gate_graph(),
        expansion_state={"create_items": True} if expanded else {},
        show_inputs=show_inputs,
        separate_outputs=separate_outputs,
    )
    assert_scene_layoutable(scene)


@pytest.mark.parametrize("show_inputs", [False, True])
def test_mapped_graphnode_merged_edges_have_existing_endpoints(show_inputs):
    """In merged-output mode every emitted edge — hidden ones included —
    must reference node ids that exist in the scene node list."""
    scene = scene_for_state(
        make_mapped_gate_graph(),
        expansion_state={"create_items": True},
        show_inputs=show_inputs,
    )
    node_ids = {n["id"] for n in scene["nodes"]}
    for edge in scene["edges"]:
        assert edge["source"] in node_ids, f"edge {edge['id']} has dangling source"
        assert edge["target"] in node_ids, f"edge {edge['id']} has dangling target"


def test_mapped_input_edge_routes_to_inner_consumer_when_expanded():
    """The renamed map_over input (pages -> page) must reach the inner node."""
    scene = scene_for_state(make_mapped_gate_graph(), expansion_state={"create_items": True})
    edges_by_id = {e["id"]: e for e in scene["edges"]}
    assert "input_pages__create_items/process" in edges_by_id
    assert not edges_by_id["input_pages__create_items/process"]["hidden"]


def test_mapped_output_edge_routes_from_inner_producer_when_expanded():
    """The renamed output (item_out -> generated) must leave from the inner node."""
    scene = scene_for_state(make_mapped_gate_graph(), expansion_state={"create_items": True})
    edges_by_id = {e["id"]: e for e in scene["edges"]}
    assert "create_items/process__save" in edges_by_id
    assert not edges_by_id["create_items/process__save"]["hidden"]


def test_mapped_output_edge_attaches_to_collapsed_container():
    """Collapsed view keeps the output boundary edge on the container hull."""
    scene = scene_for_state(make_mapped_gate_graph(), expansion_state={})
    edge_pairs = {(e["source"], e["target"]) for e in scene["edges"]}
    assert ("create_items", "save") in edge_pairs


@node(output_name="out_a")
def consume_x(x: int) -> int:
    return x


@node(output_name="out_b")
def consume_y(y: int) -> int:
    return y


def test_swapped_input_renames_route_to_correct_inner_consumers():
    """Parallel rename batches must resolve as simultaneous transforms.

    ``rename_inputs(x="y", y="x")`` swaps the boundary names in one batch:
    outer ``x`` is the inner ``y`` and vice versa. A batch-unaware resolver
    chains through both entries and wires the outer name back to the
    same-named inner param — the wrong consumer."""
    swapped = Graph(nodes=[consume_x, consume_y], name="inner").as_node().rename_inputs(x="y", y="x")
    graph = Graph(nodes=[swapped])

    name_map = graph.to_flat_graph().nodes["inner"]["input_name_map"]
    assert name_map == {"x": ("y",), "y": ("x",)}

    scene = scene_for_state(graph, expansion_state={"inner": True})
    visible_edges = {(e["source"], e["target"]) for e in scene["edges"] if not e["hidden"]}
    assert ("input_x", "inner/consume_y") in visible_edges
    assert ("input_y", "inner/consume_x") in visible_edges
    assert ("input_x", "inner/consume_x") not in visible_edges
    assert ("input_y", "inner/consume_y") not in visible_edges
    assert_scene_layoutable(scene)
