"""A MOUNTED HyperTable keeps its fan-out story at every depth.

Regression suite for four related lies first observed on panda's real
ingestion graph (a document-ingest graph whose ``materialize_pages`` node is a
HyperTable mounted with ``as_node()``):

1. The recipe's fan-out edge vanished when the table was mounted — only
   ``HyperTable.visualize`` injected it, so the mapped child drew as an
   unreachable island dagre parked at the TOP of the expanded container.
2. An outer graph input sharing a name with a mapped item field (``pdf_uri``)
   drew an edge straight into the mapped container's interior — an edge
   runtime never wires (the mapped rows feed those inputs per item).
3. Item fields with no outer presence (``page_markdown``) had no pill at all,
   so their consumers rendered with no incoming edges.
4. A multi-value boundary edge resolved its expanded target from its FIRST
   value name only — a fuzzy guess for value one (``doc_version_id``
   substring-matching ``version_id``) swallowed the exact consumers of the
   values after it (``filename`` → the converter node).

The fixture mirrors panda's shape with neutral names:

    stage ─(doc_version_id, version_id, doc_name)─▶ materialize ─▶ publish
    materialize = HyperTable(load_bytes → convert → build_pages ⇒ derive_pages)
    derive_pages = map_over("pages", identity="page_id", schema=Page)
    Page fields: page_id, page_text, source_uri, doc_name
    derive_pages inner: clean_page(page_text, source_uri), page_title(doc_name)
"""

from __future__ import annotations

import tempfile
from typing import TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.viz.renderer.ir_builder import build_graph_ir

from .conftest import scene_for_state


class Page(TypedDict):
    page_id: str
    page_text: str
    source_uri: str
    doc_name: str


@node(output_name="raw_bytes")
def load_bytes(source_uri: str) -> str:
    return source_uri


@node(output_name="converted")
def convert(raw_bytes: str, doc_name: str) -> str:
    return f"{doc_name}:{raw_bytes}"


@node(output_name="pages")
def build_pages(converted: str, version_id: str, doc_name: str, source_uri: str) -> list[Page]:
    return [Page(page_id=f"{version_id}_1", page_text=converted, source_uri=source_uri, doc_name=doc_name)]


@node(output_name="clean_text")
def clean_page(page_text: str, source_uri: str) -> str:
    return f"{page_text}@{source_uri}"


@node(output_name="title")
def page_title(doc_name: str) -> str:
    return doc_name


@node(output_name=("doc_version_id", "version_id", "doc_name"))
def stage(source_uri: str) -> tuple[str, str, str]:
    return f"{source_uri}#v1", "v1", source_uri


@node(output_name="published")
def publish(materialization: object) -> str:
    return "done"


@pytest.fixture
def mounted_graph() -> Graph:
    store = LanceDBStore(tempfile.mkdtemp() + "/store")
    derive = Graph([clean_page, page_title], name="derive_page").as_node(name="derive_pages").map_over("pages", identity="page_id", schema=Page)
    table = Graph([load_bytes, convert, build_pages, derive], name="pages_recipe").as_table(identity="doc_version_id", store=store)
    mounted = table.as_node(name="materialize", output_name="materialization")
    return Graph([stage, mounted, publish], name="ingest")


def test_mounted_flat_graph_carries_the_fanout_edge(mounted_graph):
    """The identity fan-out edge survives mounting (no extra_edges anywhere)."""
    flat = mounted_graph.to_flat_graph()

    assert flat.has_edge("materialize/build_pages", "materialize/derive_pages")
    edge = flat["materialize/build_pages"]["materialize/derive_pages"]
    assert edge["edge_type"] == "data"
    assert edge["value_names"] == ["pages"]
    assert edge.get("is_map") is True
    assert edge.get("map_fields") == ["page_id", "page_text", "source_uri", "doc_name"]


def test_mounted_mapped_node_is_not_an_island(mounted_graph):
    """Fully expanded, the mapped container has an incoming edge — dagre can
    rank it below its producer instead of parking it at the top."""
    scene = scene_for_state(mounted_graph, expand_all=True)
    edges = scene["edges"]

    fanout = [e for e in edges if e["source"] == "materialize/build_pages"]
    assert fanout, "expected the fan-out edge to leave build_pages in the expanded scene"
    node_ids = {n["id"] for n in scene["nodes"]}
    for e in edges:
        assert e["source"] in node_ids and e["target"] in node_ids, f"dangling edge {e['source']} -> {e['target']}"


def test_outer_input_pill_never_claims_map_fed_consumers(mounted_graph):
    """``source_uri`` (an outer graph input AND an item field) feeds only the
    nodes the outer value really reaches; the mapped rows feed the rest."""
    ir = build_graph_ir(mounted_graph.to_flat_graph())

    (outer_pill,) = [e for e in ir.external_inputs if e.params == ("source_uri",) and not e.map_fed]
    assert set(outer_pill.consumers) == {"stage", "materialize/load_bytes", "materialize/build_pages"}

    scene = scene_for_state(mounted_graph, expand_all=True)
    lies = [e for e in scene["edges"] if e["source"] == outer_pill.synthetic_id and e["target"].startswith("materialize/derive_pages/")]
    assert lies == [], f"outer pill must not reach into the mapped container: {lies}"


def test_map_fed_field_pills_are_synthesized_for_the_mounted_container(mounted_graph):
    """Every consumed item field gets a map-fed pill inside the container —
    including fields absent from the outer input surface entirely."""
    ir = build_graph_ir(mounted_graph.to_flat_graph())

    map_fed = {e.params[0]: e for e in ir.external_inputs if e.map_fed}
    assert set(map_fed) == {"page_text", "source_uri", "doc_name"}
    for pill in map_fed.values():
        assert pill.deepest_owner == "materialize/derive_pages"

    assert map_fed["doc_name"].consumers == ("materialize/derive_pages/page_title",)
    assert map_fed["page_text"].consumers == ("materialize/derive_pages/clean_page",)
    assert set(map_fed["source_uri"].consumers) == {"materialize/derive_pages/clean_page"}
    # The item-field pill shares a leaf name with the outer input, so its
    # synthetic id must not collide with the outer pill's.
    assert map_fed["source_uri"].synthetic_id != "input_source_uri"

    (fanout,) = [e for e in ir.edges if e.source == "materialize/build_pages" and e.target == "materialize/derive_pages"]
    assert isinstance(fanout.target_when_expanded, tuple)
    assert set(fanout.target_when_expanded) == {pill.synthetic_id for pill in map_fed.values()}


def test_previously_islanded_field_consumer_gets_its_edge(mounted_graph):
    """``page_title`` (consumes only a row-fed field) is reachable when fully
    expanded: fan-out edge → doc_name pill → page_title."""
    scene = scene_for_state(mounted_graph, expand_all=True)
    edges = scene["edges"]

    incoming = [e for e in edges if e["target"] == "materialize/derive_pages/page_title"]
    assert len(incoming) == 1
    pill_id = incoming[0]["source"]
    assert any(e["source"] == "materialize/build_pages" and e["target"] == pill_id for e in edges), (
        f"the fan-out edge must feed the pill that feeds page_title; got edges into it: {[e for e in edges if e['target'] == pill_id]}"
    )


def test_fanout_merge_promotes_an_ordering_edge_to_data():
    """A ``wait_for`` ordering edge from the producer to the mapped node must
    not survive as a mislabeled ordering edge once the fan-out facts merge in:
    the fan-out IS a data dependency, so the merged edge reports ``data``."""
    import tempfile

    from hypergraph.materialization._lancedb_store import LanceDBStore

    store = LanceDBStore(tempfile.mkdtemp() + "/store")
    derive = Graph([clean_page, page_title], name="derive_page").as_node(name="derive_pages").map_over("pages", identity="page_id", schema=Page)
    table = Graph(
        [load_bytes, convert, build_pages, derive],
        edges=[(build_pages, derive)],
        name="pages_recipe",
    ).as_table(identity="doc_version_id", store=store)
    flat = Graph([table.as_node(name="materialize")], name="outer").to_flat_graph()

    edge = flat["materialize/build_pages"]["materialize/derive_pages"]
    assert edge["edge_type"] == "data"
    assert edge.get("is_map") is True
    assert edge["value_names"] == ["pages"]
    assert edge.get("map_fields") == ["page_id", "page_text", "source_uri", "doc_name"]


def test_multi_value_boundary_edge_unions_exact_consumers(mounted_graph):
    """``stage → materialize`` carries three values; its expanded target is the
    union of every value's EXACT consumers, not the first value's fuzzy guess."""
    ir = build_graph_ir(mounted_graph.to_flat_graph())

    (edge,) = [e for e in ir.edges if e.source == "stage" and e.target == "materialize"]
    assert set(edge.value_names) == {"doc_version_id", "version_id", "doc_name"}
    targets = edge.target_when_expanded
    targets = targets if isinstance(targets, tuple) else (targets,)
    # version_id → build_pages (exact); doc_name → convert + build_pages
    # (exact; page_title is map-fed and must stay excluded);
    # doc_version_id → nothing exact, and its fuzzy match must not win.
    assert set(targets) == {"materialize/build_pages", "materialize/convert"}


ANCHOR_ID = "materialize/__output__materialization"


def test_receipt_gets_a_synthesized_output_anchor(mounted_graph):
    """The receipt (``materialization``) has no inner producer — it is the
    whole table's completion — so the IR synthesizes an OUTPUT anchor pill
    inside the container and re-sources the boundary edge from it."""
    ir = build_graph_ir(mounted_graph.to_flat_graph())

    (anchor,) = [n for n in ir.nodes if n.node_type == "OUTPUT"]
    assert anchor.id == ANCHOR_ID
    assert anchor.parent == "materialize"
    assert anchor.label == "materialization"

    (edge,) = [e for e in ir.edges if e.source == "materialize" and e.target == "publish"]
    assert edge.source_when_expanded == ANCHOR_ID


def test_expanded_container_never_sources_the_receipt_from_its_hull(mounted_graph):
    """An edge's source must be a node; only a COLLAPSED container may stand
    in as one. Expanded, the receipt edge leaves the anchor pill."""
    scene = scene_for_state(mounted_graph, expand_all=True)
    visible_edges = [e for e in scene["edges"] if not e.get("hidden")]

    assert [e for e in visible_edges if e["source"] == "materialize"] == []
    (receipt,) = [e for e in visible_edges if e["target"] == "publish" and e["source"] != "stage"]
    assert receipt["source"] == ANCHOR_ID

    (anchor_node,) = [n for n in scene["nodes"] if n["id"] == ANCHOR_ID]
    assert not anchor_node["hidden"]
    assert anchor_node["parentNode"] == "materialize"
    assert anchor_node["data"]["nodeType"] == "OUTPUT"


def test_receipt_anchor_stays_hidden_while_collapsed(mounted_graph):
    """Collapsed keeps the historical picture: the box itself sources the
    edge and no anchor pill leaks out of it."""
    scene = scene_for_state(mounted_graph)

    (anchor_node,) = [n for n in scene["nodes"] if n["id"] == ANCHOR_ID]
    assert anchor_node["hidden"]
    visible_edges = [e for e in scene["edges"] if not e.get("hidden")]
    assert ("materialize", "publish") in {(e["source"], e["target"]) for e in visible_edges}


def test_receipt_anchor_is_the_value_pill_in_separate_outputs(mounted_graph):
    """separate_outputs must not interpose a DATA node for an anchor-sourced
    edge — the anchor already IS the value pill."""
    scene = scene_for_state(mounted_graph, expand_all=True, separate_outputs=True)
    visible_edges = [e for e in scene["edges"] if not e.get("hidden")]

    (receipt,) = [e for e in visible_edges if e["target"] == "publish"]
    assert receipt["source"] == ANCHOR_ID
    assert not any(n["id"].startswith("data_" + ANCHOR_ID) for n in scene["nodes"])


def test_mermaid_receipt_leaves_the_anchor_not_the_subgraph(mounted_graph):
    """The text export agrees with the widget: expanded, the receipt edge
    leaves the anchor pill inside the subgraph, never the subgraph id."""
    mermaid = str(mounted_graph.to_mermaid(depth=2))
    lines = [line.strip() for line in mermaid.splitlines()]

    assert 'materialize____output__materialization(["materialization: MaterializationReceipt"])' in lines
    assert "materialize____output__materialization --> publish" in lines
    assert "materialize --> publish" not in lines


def test_mermaid_merged_mode_draws_one_edge_per_pair(mounted_graph):
    """The widget draws ONE merged edge per (source, target) pair; the text
    export must not fan a multi-value boundary edge into parallel arrows."""
    mermaid = str(mounted_graph.to_mermaid(depth=0))
    lines = [line.strip() for line in mermaid.splitlines()]

    assert lines.count("stage --> materialize") == 1
    assert lines.count("materialize --> publish") == 1
