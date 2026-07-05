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
