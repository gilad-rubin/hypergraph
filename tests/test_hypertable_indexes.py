"""Tests for named indexes as persisted query specs (create_index / search).

An index is a projection, not a table: a named, persisted query spec over a
vector column that already lives in the table. For the LanceDB store there is
no separate materialized artifact — LanceDB ANN-searches the column directly.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import HyperTable
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import SyncRunner

VECTORS = {
    "alpha": [1.0, 0.0, 0.0],
    "beta": [0.0, 1.0, 0.0],
    "gamma": [0.0, 0.0, 1.0],
}


class Embedder:
    def __init__(self, model: str = "embed-a"):
        self.model = model

    def _config(self):
        return {"model": self.model}

    def embed(self, text: str) -> list[float]:
        return VECTORS.get(text, [0.5, 0.5, 0.5])


@node(output_name="vec")
def embed_doc(text: str, embedder: Embedder) -> list[float]:
    return embedder.embed(text)


def make_table(store, embedder=None):
    return HyperTable([embed_doc], identity="doc_id", store=store).bind(embedder=embedder or Embedder()).with_runner(SyncRunner())


DOCS = [
    {"doc_id": "d1", "text": "alpha", "active": True},
    {"doc_id": "d2", "text": "beta", "active": True},
    {"doc_id": "d3", "text": "gamma", "active": False},
]


@pytest.fixture
def store(tmp_path):
    return LanceDBStore(str(tmp_path / "index_store"))


@pytest.fixture
def table(store):
    t = make_table(store)
    t.insert(DOCS)
    return t


class TestCreateListDrop:
    def test_create_and_list(self, table):
        table.create_index("main", vector="vec", text="text")

        specs = table.list_indexes()
        assert len(specs) == 1
        spec = specs[0]
        assert spec["name"] == "main"
        assert spec["on"] == "doc"
        assert spec["vector"] == "vec"
        assert spec["text"] == "text"
        assert spec["recipe_fingerprint"]
        assert spec["current"] is True

    def test_drop(self, table):
        table.create_index("main", vector="vec")
        table.drop_index("main")
        assert table.list_indexes() == []

    def test_drop_unknown_raises(self, table):
        with pytest.raises(KeyError, match="no index named"):
            table.drop_index("nope")

    def test_unknown_vector_column_raises(self, table):
        with pytest.raises(ValueError, match="nope"):
            table.create_index("main", vector="nope")

    def test_unknown_rows_column_raises(self, table):
        with pytest.raises(ValueError, match="missing_col"):
            table.create_index("main", vector="vec", rows={"missing_col": True})

    def test_unknown_table_raises(self, table):
        with pytest.raises(ValueError, match="not_a_table"):
            table.create_index("main", on="not_a_table", vector="vec")

    def test_vector_is_required(self, table):
        with pytest.raises(ValueError, match="vector"):
            table.create_index("main")


class TestPersistence:
    def test_index_survives_a_fresh_instance_over_the_same_store(self, store, table):
        table.create_index("main", vector="vec", rows={"active": True})

        fresh = make_table(store)
        specs = fresh.list_indexes()
        assert [s["name"] for s in specs] == ["main"]
        assert specs[0]["rows"] == {"active": True}
        assert specs[0]["current"] is True


class TestSearch:
    def test_search_returns_the_nearest_row(self, table):
        table.create_index("main", vector="vec")

        hits = table.search([0.9, 0.1, 0.0], index="main", limit=1)
        assert len(hits) == 1
        assert hits[0]["doc_id"] == "d1"
        assert hits[0]["text"] == "alpha"
        assert "_distance" in hits[0]

    def test_search_honors_the_row_filter(self, table):
        table.create_index("active_only", vector="vec", rows={"active": True})

        hits = table.search([0.0, 0.1, 0.9], index="active_only", limit=1)
        # d3 (gamma) is the true nearest but inactive; the filter excludes it.
        assert hits[0]["doc_id"] == "d2"

    def test_search_unknown_index_raises(self, table):
        with pytest.raises(KeyError, match="no index named"):
            table.search([1.0, 0.0, 0.0], index="nope")


class Page(TypedDict):
    page_id: str
    page_text: str


@node(output_name="pages")
def split_pages(text: str) -> list[Page]:
    return [Page(page_id=f"p{i}", page_text=part) for i, part in enumerate(text.split("|"))]


@node(output_name="page_vector")
def embed_page(page_text: str, embedder: Embedder) -> list[float]:
    return embedder.embed(page_text)


process_page = Graph([embed_page], name="process_page")


def make_child_table(store, embedder=None):
    pages_node = process_page.as_node().map_over("pages", identity="page_id")
    return HyperTable([split_pages, pages_node], identity="doc_id", store=store).bind(embedder=embedder or Embedder()).with_runner(SyncRunner())


class TestChildTableIndex:
    def test_index_on_child_pages(self, tmp_path):
        store = LanceDBStore(str(tmp_path / "child_index_store"))
        table = make_child_table(store)
        table.insert([{"doc_id": "d1", "text": "alpha|beta"}, {"doc_id": "d2", "text": "gamma"}])

        table.create_index("pages_idx", on="page", vector="page_vector")

        hits = table.search([0.0, 0.95, 0.05], index="pages_idx", limit=1)
        assert hits[0]["page_text"] == "beta"
        assert hits[0]["_parent_id"] == "d1"


class TestRecipeCurrency:
    def test_current_flips_after_rebinding_a_different_embedder(self, store, table):
        table.create_index("main", vector="vec")
        assert table.list_indexes()[0]["current"] is True

        rebound = make_table(store, embedder=Embedder("embed-b"))
        assert rebound.list_indexes()[0]["current"] is False
