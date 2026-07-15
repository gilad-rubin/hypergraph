"""Tests for HyperTable.status(): the read-only staleness dry-run."""

from __future__ import annotations

from typing import TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import SyncRunner

# ---------------------------------------------------------------------------
# Test nodes and components
# ---------------------------------------------------------------------------


class Embedder:
    def __init__(self, model_name: str = "test-embed", dim: int = 3):
        self.model_name = model_name
        self.dim = dim

    def _config(self):
        return {"model": self.model_name, "dim": self.dim}

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for i, c in enumerate(text[: self.dim]):
            vec[i] = float(ord(c)) / 122.0
        return vec


@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()


@node(output_name="vector")
def embed_text(clean_text: str, embedder: Embedder) -> list[float]:
    return embedder.embed(clean_text)


@node(output_name="clean_text")
def clean_variant(text: str) -> str:
    return text.strip().lower().replace("-", " ")


@node(output_name="length")
def measure(text: str) -> int:
    if "boom" in text:
        raise ValueError("boom row")
    return len(text)


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


@pytest.fixture
def store(tmp_path):
    return LanceDBStore(str(tmp_path / "status_store"))


@pytest.fixture
def embedder():
    return Embedder()


def make_table(store, embedder):

    return Graph([clean, embed_text]).bind(embedder=embedder).as_table(identity="doc_id", store=store, runner=SyncRunner())


# ---------------------------------------------------------------------------
# Root-row staleness
# ---------------------------------------------------------------------------


class TestStatusRoot:
    def test_fresh_after_insert(self, store, embedder):
        table = make_table(store, embedder)
        table.insert([{"doc_id": "d1", "text": "hello"}, {"doc_id": "d2", "text": "world"}])

        report = table.status()
        assert report.table == "doc"
        assert (report.total, report.fresh, report.stale, report.errored) == (2, 2, 0, 0)
        assert report.is_fresh

    def test_component_config_change_marks_all_stale(self, store, embedder):
        table = make_table(store, embedder)
        table.insert([{"doc_id": "d1", "text": "hello"}, {"doc_id": "d2", "text": "world"}])

        rebound = table.graph.bind(embedder=Embedder(model_name="new-embed")).as_table(identity="doc_id", store=store, runner=SyncRunner())
        report = rebound.status()
        assert (report.fresh, report.stale) == (0, 2)
        assert report.stale_ids == ("d1", "d2")
        assert not report.is_fresh

    def test_node_definition_change_marks_stale(self, store, embedder):

        make_table(store, embedder).insert(doc_id="d1", text="a-b")

        variant = Graph([clean_variant, embed_text]).bind(embedder=embedder).as_table(identity="doc_id", store=store, runner=SyncRunner())
        report = variant.status()
        assert (report.fresh, report.stale) == (0, 1)

    def test_status_requires_no_runner(self, store, embedder):

        make_table(store, embedder).insert(doc_id="d1", text="hello")

        readonly = Graph([clean, embed_text]).bind(embedder=embedder).as_table(identity="doc_id", store=store)
        report = readonly.status()
        assert report.is_fresh

    def test_sync_heals_stale_rows(self, store, embedder):
        table = make_table(store, embedder)
        items = [{"doc_id": "d1", "text": "hello"}, {"doc_id": "d2", "text": "world"}]
        table.insert(items)

        rebound = table.graph.bind(embedder=Embedder(model_name="new-embed")).as_table(identity="doc_id", store=store, runner=SyncRunner())
        assert rebound.status().stale == 2
        rebound.sync(items)
        assert rebound.status().is_fresh

    def test_errored_rows_reported_separately(self, store):

        table = Graph([measure]).as_table(identity="doc_id", store=store, on_error="store", runner=SyncRunner())
        table.insert([{"doc_id": "ok", "text": "fine"}, {"doc_id": "bad", "text": "boom"}])

        report = table.status()
        assert (report.fresh, report.stale, report.errored) == (1, 0, 1)
        assert report.errored_ids == ("bad",)
        assert not report.is_fresh

    def test_metadata_set_does_not_stale(self, store, embedder):
        table = make_table(store, embedder)
        table.insert(doc_id="d1", text="hello", station="north")
        table.set({"doc_id": "d1"}, station="south")
        assert table.status().is_fresh


# ---------------------------------------------------------------------------
# Child-table staleness
# ---------------------------------------------------------------------------


class TestStatusChildren:
    def make_parent_child(self, store, embedder):

        pages_node = process_page.as_node().map_over("pages", identity="page_id")
        return Graph([split_pages, pages_node]).bind(embedder=embedder).as_table(identity="doc_id", store=store, runner=SyncRunner())

    def test_children_fresh_after_insert(self, store, embedder):
        table = self.make_parent_child(store, embedder)
        table.insert(doc_id="d1", text="alpha|beta")

        report = table.status()
        assert report.is_fresh
        assert len(report.children) == 1
        child = report.children[0]
        assert (child.total, child.fresh, child.stale) == (2, 2, 0)

    def test_child_component_change_marks_children_stale(self, store, embedder):
        table = self.make_parent_child(store, embedder)
        table.insert(doc_id="d1", text="alpha|beta")

        rebound = table.graph.bind(embedder=Embedder(model_name="new-embed")).as_table(identity="doc_id", store=store, runner=SyncRunner())
        child = rebound.status().children[0]
        assert (child.fresh, child.stale) == (0, 2)
        assert not rebound.status().is_fresh
