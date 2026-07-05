"""HyperTable.visualize must connect the parent node to its mapped child.

Regression for the "disconnected island" bug: with ``map_over(..., identity=)``
the mapped GraphNode consumes the parent's list column through the derive lane,
not through a name-matched input port, so graph auto-wiring produced no edge and
``visualize(include_children=True)`` rendered the mapped subgraph as a floating
island. The fix injects a viz-only fan-out edge; these tests assert on the
flattened IR edge structure, not pixels.
"""

from __future__ import annotations

import tempfile
from typing import TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import HyperTable
from hypergraph.materialization._lancedb_store import LanceDBStore


class Item(TypedDict):
    item_id: str
    text: str


@node(output_name="items")
def produce_items(source: str) -> list[Item]:
    return [Item(item_id="i0", text=source)]


@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip()


@pytest.fixture
def store():
    return LanceDBStore(tempfile.mkdtemp() + "/store")


def _combined_flat_graph(table: HyperTable):
    """Rebuild exactly what ``visualize`` renders and return its flat graph."""
    from hypergraph.graph import Graph as _Graph

    table._ensure_analyzed()
    nodes = list(table._graph.nodes.values())
    nodes.extend(table._map_over_nodes)
    combined = _Graph(nodes, name=table._spec.name)
    return combined.to_flat_graph(extra_edges=table._fanout_viz_edges())


def test_fanout_edge_connects_producer_to_mapped_node(store):
    """The node producing the mapped column links to the mapped GraphNode."""
    table = HyperTable(
        [produce_items, Graph([clean], name="proc").as_node(name="items_node").map_over("items", identity="item_id")],
        identity="doc_id",
        store=store,
    )

    G = _combined_flat_graph(table)

    assert G.has_edge("produce_items", "items_node"), f"expected fan-out edge produce_items -> items_node; edges: {list(G.edges())}"
    edge = G["produce_items"]["items_node"]
    assert edge["value_names"] == ["items"]
    assert edge["edge_type"] == "data"
    assert edge.get("is_map") is True


def test_without_fix_the_nodes_are_disconnected(store):
    """Guards the diagnosis: without the injected edge there is no connection.

    This is what the bug looked like — the combined graph's auto-wiring produces
    zero edges between the producer and the mapped node.
    """
    from hypergraph.graph import Graph as _Graph

    table = HyperTable(
        [produce_items, Graph([clean], name="proc").as_node(name="items_node").map_over("items", identity="item_id")],
        identity="doc_id",
        store=store,
    )
    table._ensure_analyzed()
    nodes = list(table._graph.nodes.values())
    nodes.extend(table._map_over_nodes)
    combined = _Graph(nodes, name=table._spec.name)

    G_no_fix = combined.to_flat_graph()  # no extra_edges
    assert not G_no_fix.has_edge("produce_items", "items_node")


def test_visualize_renders_without_raising(store):
    """End-to-end: visualize(include_children=True) produces a widget, no raise."""
    table = HyperTable(
        [produce_items, Graph([clean], name="proc").as_node(name="items_node").map_over("items", identity="item_id")],
        identity="doc_id",
        store=store,
    )
    out = table.visualize()
    assert out is not None
    # filepath mode writes HTML and returns None
    path = tempfile.mkdtemp() + "/g.html"
    assert table.visualize(filepath=path) is None


def test_extra_edges_are_viz_only_not_in_runtime_graph(store):
    """The fan-out edge lives only in the viz flat graph, never in the derive graph."""
    table = HyperTable(
        [produce_items, Graph([clean], name="proc").as_node(name="items_node").map_over("items", identity="item_id")],
        identity="doc_id",
        store=store,
    )
    table._ensure_analyzed()

    # The runtime graph (plain nodes only) has neither the map node nor any
    # 'items' input port — proving the port asymmetry that motivates the viz fix.
    runtime_flat = table._graph.to_flat_graph()
    assert "items_node" not in runtime_flat.nodes
    assert "items" not in table._graph.inputs.all


@node(output_name="items_a")
def produce_a(source: str) -> list[Item]:
    return [Item(item_id="a0", text=source)]


@node(output_name="items_b")
def produce_b(source: str) -> list[Item]:
    return [Item(item_id="b0", text=source)]


def test_two_children_each_get_their_own_fanout_edge(store):
    """Two map_over children (different producer columns) each connect to their own node.

    Regression for a position-vs-name-matching bug in ``_fanout_viz_edges``: an
    earlier version looked up the mapped GraphNode by scanning for whichever
    node's ``_map_over`` happened to contain the column name, so two children
    could resolve to the same (first-matching) map node. The fix pairs
    ``self._spec.children`` with ``self._map_over_nodes`` by position — the two
    lists are always parallel because ``analyze_table`` builds one child spec
    per map node, in order.
    """
    table = HyperTable(
        [
            produce_a,
            produce_b,
            Graph([clean], name="proc_a").as_node(name="a_node").map_over("items_a", identity="item_id"),
            Graph([clean], name="proc_b").as_node(name="b_node").map_over("items_b", identity="item_id"),
        ],
        identity="doc_id",
        store=store,
    )
    table._ensure_analyzed()

    edges = table._fanout_viz_edges()

    assert ("produce_a", "a_node", ("items_a",)) in edges
    assert ("produce_b", "b_node", ("items_b",)) in edges
    assert len(edges) == 2


def test_fanout_pairing_is_positional_not_name_matched():
    """Two map nodes mapping the *same* column name still get distinct edges.

    Exercises ``_fanout_viz_edges``'s pairing directly against hand-built
    ``TableSpec``/map-node lists (bypassing ``HyperTable`` construction, which
    does not yet support two children over one shared parent column end to
    end). Proves the zip-by-position fix resolves each child to its own map
    node rather than both resolving to the first name match.
    """
    from hypergraph.materialization._schema import analyze_table

    @node(output_name="upper_text")
    def shout(text: str) -> str:
        return text.upper()

    clean_map = Graph([clean], name="proc_clean").as_node(name="clean_node").map_over("items", identity="item_id")
    shout_map = Graph([shout], name="proc_shout").as_node(name="shout_node").map_over("items", identity="item_id")
    root_graph = Graph([produce_items], name="root")
    map_over_nodes = [clean_map, shout_map]

    spec = analyze_table(root_graph, "doc_id", {}, map_over_nodes)
    assert [c.map_input for c in spec.children] == ["items", "items"]

    table = HyperTable.__new__(HyperTable)
    table._spec = spec
    table._map_over_nodes = map_over_nodes
    table._boundary_node = lambda child_spec: root_graph.nodes.get("produce_items")

    edges = table._fanout_viz_edges()

    assert ("produce_items", "clean_node", ("items",)) in edges
    assert ("produce_items", "shout_node", ("items",)) in edges
    assert len(edges) == 2


def test_to_flat_graph_merges_extra_edge_into_existing_edge():
    """extra_edges must merge into a pre-existing edge, not drop the fan-out label.

    If the mapped GraphNode already has a real data edge from the same
    producer (e.g. for another shared value), injecting the fan-out edge must
    not silently no-op — it should union the value_names and still tag
    ``is_map=True``, matching the merge convention ``_add_explicit_data_edges``
    already uses for colliding user-declared edges.
    """

    @node(output_name=("items", "other"))
    def produce_both(source: str) -> tuple[list[Item], str]:
        return [Item(item_id="i0", text=source)], "shared"

    @node(output_name="clean_text")
    def consume_other(other: str) -> str:
        return other.strip()

    map_node = Graph([consume_other], name="proc").as_node(name="items_node")
    root_graph = Graph([produce_both, map_node], name="root")

    # Sanity: auto-wiring already connected produce_both -> items_node via 'other'.
    pre_flat = root_graph.to_flat_graph()
    assert pre_flat.has_edge("produce_both", "items_node")
    assert pre_flat["produce_both"]["items_node"]["value_names"] == ["other"]

    flat = root_graph.to_flat_graph(extra_edges=[("produce_both", "items_node", ("items",))])

    edge = flat["produce_both"]["items_node"]
    assert set(edge["value_names"]) == {"other", "items"}
    assert edge["is_map"] is True


def test_visualize_sizes_for_the_injected_fanout_edge(store):
    """Layout estimate reflects the fan-out edge, not just the disconnected view.

    ``LayoutEstimator`` sizes off ``combined.nx_graph`` (the pre-injection,
    unflattened graph), which is a different object than the flat graph the
    fan-out edge is injected into for rendering. Without also injecting into
    ``combined.nx_graph``, the producer and mapped node both look like
    layer-0 roots, and the estimate is too wide/too short — visibly wrong
    versus the two-layer graph actually rendered.
    """
    table = HyperTable(
        [produce_items, Graph([clean], name="proc").as_node(name="items_node").map_over("items", identity="item_id")],
        identity="doc_id",
        store=store,
    )

    out = table.visualize()

    # Two stacked layers (produce_items -> items_node) must be taller than the
    # side-by-side (disconnected-roots) estimate would have been.
    assert out.height > 300
