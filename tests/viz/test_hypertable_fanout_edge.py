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
from hypergraph.materialization._hypertable_viz import fanout_map_fields, fanout_viz_edges
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
    """Rebuild exactly what ``visualize`` renders and return its flat graph.

    Mirrors ``HyperTable.visualize``: injects the fan-out edges AND stamps each
    with its mapped item's ``map_fields`` so the IR can re-route the edge into
    the item-field INPUT pill(s) on expansion.
    """
    from hypergraph.graph import Graph as _Graph

    table._ensure_analyzed()
    nodes = list(table._graph.nodes.values())
    nodes.extend(table._map_over_nodes)
    combined = _Graph(nodes, name=table._spec.name)
    flat = combined.to_flat_graph(extra_edges=fanout_viz_edges(table._graph, table._spec, table._map_over_nodes))
    for (src, tgt), fields in fanout_map_fields(table._graph, table._spec, table._map_over_nodes).items():
        if flat.has_edge(src, tgt):
            flat[src][tgt]["map_fields"] = list(fields)
    return flat


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

    edges = fanout_viz_edges(table._graph, table._spec, table._map_over_nodes)

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

    edges = fanout_viz_edges(root_graph, spec, map_over_nodes)

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


def test_fanout_edge_ir_reroutes_into_expanded_container(store):
    """The IR edge into the mapped GRAPH container must carry target_when_expanded.

    The mapped container starts EXPANDED at the default depth, and the JS scene
    builder ranks only visible nodes — an edge whose target is the (invisible,
    expanded) container crashes dagre with "Cannot set properties of undefined
    (setting 'rank')". Option A: the mapped item ``Item`` has a ``text`` field
    that the inner ``clean`` node consumes, so the fan-out edge re-routes to the
    ``text`` INPUT pill — the visible unpack
    ``produce_items ──items──▶ [text] ──▶ clean``.
    """
    from hypergraph.viz.renderer.ir_builder import build_graph_ir

    table = HyperTable(
        [produce_items, Graph([clean], name="proc").as_node(name="items_node").map_over("items", identity="item_id")],
        identity="doc_id",
        store=store,
    )

    ir = build_graph_ir(_combined_flat_graph(table))

    (fanout,) = [e for e in ir.edges if e.source == "produce_items" and e.target == "items_node"]
    assert fanout.target_when_expanded == "input_text", (
        f"fan-out edge must re-route to the item-field INPUT pill when expanded; got {fanout.target_when_expanded!r}"
    )
    # The re-routed target is a real INPUT pill marked map-fed — not a plain
    # external input and not the entrypoint.
    (text_pill,) = [e for e in ir.external_inputs if e.synthetic_id == "input_text"]
    assert text_pill.map_fed is True
    assert text_pill.deepest_owner == "items_node"


def test_map_fed_field_pill_is_not_a_plain_external_input(store):
    """The item-field input renders as map-fed, and a genuine external input does not.

    ``Item`` = {item_id, text}; ``clean`` consumes ``text`` (a field) while
    ``produce_items`` consumes ``source`` (a real external supplier). Only
    ``text`` is flagged map-fed.
    """
    from hypergraph.viz.renderer.ir_builder import build_graph_ir

    table = HyperTable(
        [produce_items, Graph([clean], name="proc").as_node(name="items_node").map_over("items", identity="item_id")],
        identity="doc_id",
        store=store,
    )
    ir = build_graph_ir(_combined_flat_graph(table))

    by_id = {e.synthetic_id: e for e in ir.external_inputs}
    assert by_id["input_text"].map_fed is True, "item-field input must be map-fed"
    assert by_id["input_source"].map_fed is False, "genuine external input must be untouched"


@node(output_name="clean_text")
def broadcast_consumer(config: str) -> str:
    """Inner node consuming a broadcast input whose name is NOT an item field."""
    return config.strip()


def test_broadcast_input_is_untouched_and_edge_falls_back(store):
    """A broadcast (non-field) inner input keeps its plain rendering.

    ``Item`` = {item_id, text}; the inner node consumes ``config``, which is not
    an item field, so it is a broadcast input: NOT map-fed, and the fan-out edge
    has no field pill to land on — it falls back to the container entrypoint
    (#169 behavior).
    """
    from hypergraph.viz.renderer.ir_builder import build_graph_ir

    table = HyperTable(
        [produce_items, Graph([broadcast_consumer], name="proc").as_node(name="items_node").map_over("items", identity="item_id")],
        identity="doc_id",
        store=store,
    )
    ir = build_graph_ir(_combined_flat_graph(table))

    (config_pill,) = [e for e in ir.external_inputs if e.synthetic_id == "input_config"]
    assert config_pill.map_fed is False, "broadcast input must not be map-fed"
    (fanout,) = [e for e in ir.edges if e.source == "produce_items" and e.target == "items_node"]
    assert fanout.target_when_expanded == "items_node/broadcast_consumer", (
        f"with no matching field pill the fan-out edge falls back to the entrypoint; got {fanout.target_when_expanded!r}"
    )


@node(output_name="plain_items")
def produce_plain_items(source: str) -> list[str]:
    """Producer whose item type is ``str`` — a fieldless mapped item."""
    return [source]


@node(output_name="clean_text")
def clean_plain(plain_items: str) -> str:
    return plain_items.strip()


def test_fieldless_item_falls_back_to_entrypoint(store):
    """No schema and a fieldless annotation (``list[str]``) => entrypoint fallback.

    ``_map_config`` carries only ``identity`` (no ``schema``), and the producer
    returns ``list[str]``, which has no fields — so nothing is map-fed and the
    fan-out edge routes to the entrypoint exactly as in #169.
    """
    from hypergraph.viz.renderer.ir_builder import build_graph_ir

    table = HyperTable(
        [produce_plain_items, Graph([clean_plain], name="proc").as_node(name="items_node").map_over("plain_items", identity="item_id")],
        identity="doc_id",
        store=store,
    )
    ir = build_graph_ir(_combined_flat_graph(table))

    assert not any(e.map_fed for e in ir.external_inputs), "a fieldless list[str] item feeds no field pills"
    (fanout,) = [e for e in ir.edges if e.source == "produce_plain_items" and e.target == "items_node"]
    assert fanout.target_when_expanded == "items_node/clean_plain"


def test_field_consumer_nested_a_graph_deeper_is_still_map_fed(store):
    """A field consumer wrapped in ANOTHER nested graph is still map-fed.

    Regression for the nested-owner gap: ``clean`` consumes the ``text`` field
    but sits inside ``items_node/inner_node``, so the ``text`` pill's
    ``deepest_owner`` is that inner container while ``map_fields`` live on the
    mapped ``items_node``. Matching must walk ancestors, or the pill reads as a
    plain external input and the edge falls back to the entrypoint.
    """
    from hypergraph.viz.renderer.ir_builder import build_graph_ir

    inner = Graph([clean], name="inner").as_node(name="inner_node")
    child = Graph([inner], name="proc").as_node(name="items_node").map_over("items", identity="item_id")
    table = HyperTable([produce_items, child], identity="doc_id", store=store)
    ir = build_graph_ir(_combined_flat_graph(table))

    (text_pill,) = [e for e in ir.external_inputs if e.synthetic_id == "input_text"]
    assert text_pill.map_fed is True, "nested field consumer must still be map-fed"
    assert text_pill.deepest_owner == "items_node/inner_node"
    (fanout,) = [e for e in ir.edges if e.source == "produce_items" and e.target == "items_node"]
    assert fanout.target_when_expanded == "input_text", "edge must re-route to the nested field pill, not the entrypoint"


@node(output_name="embedding")
def embed_chunk(chunk: str) -> list[float]:
    """Inner node whose input is renamed FROM the item field ``text``."""
    return [0.0]


def test_renamed_inner_input_is_still_map_fed(store):
    """A container that renames the item field to a different inner input is map-fed.

    Regression for the rename gap: ``with_inputs(chunk="text")`` makes the inner
    node consume ``chunk`` while the parent-facing container input (and pill)
    stays ``text``. Matching against the container's OWN inputs (parent-facing),
    not the renamed inner input, keeps ``text`` map-fed and routes the edge to
    its pill.
    """
    from hypergraph.viz.renderer.ir_builder import build_graph_ir

    child = Graph([embed_chunk], name="proc").as_node(name="items_node").with_inputs(chunk="text").map_over("items", identity="item_id")
    table = HyperTable([produce_items, child], identity="doc_id", store=store)
    ir = build_graph_ir(_combined_flat_graph(table))

    (text_pill,) = [e for e in ir.external_inputs if e.synthetic_id == "input_text"]
    assert text_pill.map_fed is True, "renamed inner input must still be map-fed on the parent-facing name"
    (fanout,) = [e for e in ir.edges if e.source == "produce_items" and e.target == "items_node"]
    assert fanout.target_when_expanded == "input_text"


def test_unresolvable_schema_yields_no_fields_not_a_raise():
    """``_fanout_map_fields`` swallows a ``return_type`` failure (defensive).

    A truly unresolved forward reference fails ``analyze_table`` (``_input_types``)
    before viz is ever reached, so this can't be provoked through a constructed
    table; the guard is exercised directly to prove field discovery degrades to
    "no fields" rather than propagating, keeping ``visualize`` from raising if a
    producer annotation ever resolves for analysis but not here.
    """

    class _RaisingProducer:
        name = "producer"
        data_outputs = ("items",)
        func = None  # forces return_type -> str; we monkeypatch below

    class _MapNode:
        name = "items_node"
        _map_config = {"identity": "item_id"}

    class _ChildSpec:
        map_input = "items"

    class _Spec:
        children = [_ChildSpec()]

    class _Graph:
        nodes = {"producer": _RaisingProducer()}

    # Patch return_type to raise, simulating an annotation that resolves at
    # analysis time but not here.
    import hypergraph.materialization._hypertable_viz as hypertable_viz

    original = hypertable_viz.return_type
    hypertable_viz.return_type = lambda node: (_ for _ in ()).throw(NameError("Unresolved"))
    try:
        assert fanout_map_fields(_Graph(), _Spec(), [_MapNode()]) == {("producer", "items_node"): ()}
    finally:
        hypertable_viz.return_type = original
