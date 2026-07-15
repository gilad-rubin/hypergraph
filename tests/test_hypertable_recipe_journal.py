"""The recipe journal: every provenance stamp resolves to readable recipe text forever.

Fingerprints hash the recipe (node source + component configs + bound plain
values) and discard the text. A stamp can detect "built under something else"
but cannot name what that was — an uncommitted edit that derived rows is
unrecoverable from the hash alone. The journal persists the payloads, keyed by
their hash, into a table in the SAME store the HyperTable writes, append-once —
so ``HyperTable.explain(...)`` resolves any stamp to verbatim source, commits
or not.
"""

from __future__ import annotations

import pytest

from hypergraph import Graph, node
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import SyncRunner


class Embedder:
    def __init__(self, model_name: str = "embed-a"):
        self.model_name = model_name

    def _config(self):
        return {"model": self.model_name}

    def embed(self, text: str) -> list[float]:
        return [float(len(self.model_name)), float(len(text))]


@pytest.fixture
def store(tmp_path):
    return LanceDBStore(str(tmp_path / "journal_store"))


DOCS = [{"doc_id": "d1", "text": "Chest pain"}, {"doc_id": "d2", "text": "Stroke triage"}]


def _make_table(store, clean_fn, embedder=None):
    @node(output_name="vector")
    def embed_text(clean_text: str, embedder: Embedder) -> list[float]:
        return embedder.embed(clean_text)

    return Graph([clean_fn, embed_text]).bind(embedder=embedder or Embedder("embed-a")).as_table(identity="doc_id", store=store, runner=SyncRunner())


# ---------------------------------------------------------------------------
# F1 (decisive): an uncommitted edit's OLD source is still resolvable.
# ---------------------------------------------------------------------------


def test_uncommitted_node_edit_keeps_old_source_resolvable(store):
    """Redefine a node with different source (an uncommitted edit), re-derive,
    and the journal still resolves BOTH the old stamp to the old source verbatim
    and the new stamp to the new source — no git anywhere."""

    # v1 of the node.
    @node(output_name="clean_text")
    def clean(text: str) -> str:
        return text.strip().lower()

    table = _make_table(store, clean)
    table.insert(DOCS)

    # Capture the OLD stamp + its resolved source BEFORE the edit.
    old_explained = table.explain("d1")
    old_stamp = old_explained["clean_text"]["provenance"]
    assert "text.strip().lower()" in old_explained["clean_text"]["source"]
    assert "!" not in old_explained["clean_text"]["source"]

    # v2: DIFFERENT source (an uncommitted edit — new body, same name).
    @node(output_name="clean_text")
    def clean(text: str) -> str:  # noqa: F811
        return text.strip().lower() + "!"

    table_v2 = _make_table(store, clean)
    table_v2.sync(DOCS)

    # The row re-derived under the new recipe...
    assert table_v2.get("d1")["clean_text"] == "chest pain!"
    new_explained = table_v2.explain("d1")
    new_stamp = new_explained["clean_text"]["provenance"]
    assert new_stamp != old_stamp, "an edited node must mint a new stamp"
    assert 'text.strip().lower() + "!"' in new_explained["clean_text"]["source"]

    # ...and the OLD stamp STILL resolves to the OLD source verbatim, straight
    # from the store's journal — no git anywhere.
    resolved_old = table_v2.resolve_provenance(old_stamp)
    assert resolved_old is not None
    assert "text.strip().lower()" in resolved_old
    assert '+ "!"' not in resolved_old


# ---------------------------------------------------------------------------
# F2: append-once — 100 rows under one recipe write each payload exactly once.
# ---------------------------------------------------------------------------


def test_append_once_across_many_rows(store):
    @node(output_name="clean_text")
    def clean(text: str) -> str:
        return text.strip().lower()

    table = _make_table(store, clean)
    many = [{"doc_id": f"d{i}", "text": f"row {i}"} for i in range(100)]
    table.insert(many)

    journal = table.journal_rows()
    # Two nodes (clean, embed_text) => two node-source payloads; one component
    # config (embedder) => one config payload. Regardless of node/config count,
    # NO payload appears twice.
    hashes = [r["hash"] for r in journal]
    assert len(hashes) == len(set(hashes)), "no journal payload may be written twice"
    # The whole 100-row insert shares one recipe, so the journal is tiny.
    assert len(journal) < 20, f"append-once should keep the journal tiny, got {len(journal)} rows"


# ---------------------------------------------------------------------------
# F3: a fresh table object over the same store resolves old stamps.
# ---------------------------------------------------------------------------


def test_fresh_table_resolves_old_stamps(tmp_path):
    path = str(tmp_path / "reopen_store")

    @node(output_name="clean_text")
    def clean(text: str) -> str:
        return text.strip().lower()

    store1 = LanceDBStore(path)
    table1 = _make_table(store1, clean)
    table1.insert(DOCS)
    stamp = table1.explain("d1")["clean_text"]["provenance"]

    # New process simulation: brand-new store handle + table object, same folder.
    store2 = LanceDBStore(path)
    table2 = _make_table(store2, clean)
    resolved = table2.resolve_provenance(stamp)
    assert resolved is not None
    assert "text.strip().lower()" in resolved
