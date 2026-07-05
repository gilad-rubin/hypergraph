"""Tests for per-column provenance across the parent/child (map_over) boundary.

The scenario: one document row fans out to page rows; the per-page pipeline is
enrich (expensive LLM stand-in) -> embed. Swapping the embedder must re-run
ONLY embed per page — not the document split, not the enrichment.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import SyncRunner

CALLS = {"split": 0, "split_v2": 0, "enrich": 0, "embed": 0}


class Enricher:
    def __init__(self, name: str = "enrich-a"):
        self.name = name

    def _config(self):
        return {"name": self.name}

    def summarize(self, text: str) -> str:
        return f"{self.name}:{text[:5]}"


class Embedder:
    def __init__(self, model: str = "embed-a"):
        self.model = model

    def _config(self):
        return {"model": self.model}

    def embed(self, text: str) -> list[float]:
        return [float(len(self.model)), float(len(text))]


class Page(TypedDict):
    page_id: str
    page_text: str


@node(output_name="pages")
def split_pages(text: str) -> list[Page]:
    CALLS["split"] += 1
    return [Page(page_id=f"p{i}", page_text=part) for i, part in enumerate(text.split("|"))]


@node(output_name="pages")
def split_pages_v2(text: str) -> list[Page]:
    CALLS["split_v2"] += 1
    parts = text.split("|")  # different code, same pages
    return [Page(page_id=f"p{i}", page_text=part) for i, part in enumerate(parts)]


@node(output_name="summary")
def enrich_page(page_text: str, enricher: Enricher) -> str:
    CALLS["enrich"] += 1
    return enricher.summarize(page_text)


@node(output_name="page_vector")
def embed_page(page_text: str, embedder: Embedder) -> list[float]:
    CALLS["embed"] += 1
    return embedder.embed(page_text)


process_page = Graph([enrich_page, embed_page], name="process_page")


@pytest.fixture(autouse=True)
def reset_calls():
    for key in CALLS:
        CALLS[key] = 0


@pytest.fixture
def store(tmp_path):
    return LanceDBStore(str(tmp_path / "child_prov_store"))


def make_table(store, embedder=None, enricher=None, split_node=split_pages):
    from hypergraph.materialization import HyperTable

    pages_node = process_page.as_node().map_over("pages", identity="page_id")
    return (
        HyperTable([split_node, pages_node], identity="doc_id", store=store)
        .bind(embedder=embedder or Embedder(), enricher=enricher or Enricher())
        .with_runner(SyncRunner())
    )


DOCS = [
    {"doc_id": "d1", "text": "alpha|beta"},
    {"doc_id": "d2", "text": "gamma|delta|epsilon"},
]  # 2 docs, 5 pages


class TestRecipeChanges:
    def test_embedder_swap_rederives_only_embeddings(self, store):
        make_table(store).insert(DOCS)
        assert (CALLS["split"], CALLS["enrich"], CALLS["embed"]) == (2, 5, 5)

        swapped = make_table(store, embedder=Embedder("embed-b"))
        swapped.sync(DOCS)

        assert CALLS["split"] == 2, "document split must not re-run on an embedder swap"
        assert CALLS["enrich"] == 5, "per-page enrichment (the LLM) must not re-run"
        assert CALLS["embed"] == 10, "embeddings must re-run for all 5 pages"
        assert swapped.children("d1")[0]["page_vector"][0] == float(len("embed-b"))
        assert swapped.status().is_fresh

    def test_enricher_swap_leaves_embeddings_alone(self, store):
        make_table(store).insert(DOCS)

        swapped = make_table(store, enricher=Enricher("enrich-b"))
        swapped.sync(DOCS)

        assert CALLS["split"] == 2
        assert CALLS["enrich"] == 10
        assert CALLS["embed"] == 5, "embeddings must not re-run on an enricher swap"
        assert swapped.children("d1")[0]["summary"].startswith("enrich-b:")
        assert swapped.status().is_fresh

    def test_split_code_change_with_same_pages_skips_children(self, store):
        make_table(store).insert(DOCS)

        variant = make_table(store, split_node=split_pages_v2)
        variant.sync(DOCS)

        assert CALLS["split_v2"] == 2, "changed split must re-run"
        assert CALLS["enrich"] == 5, "identical pages must not re-enrich"
        assert CALLS["embed"] == 5, "identical pages must not re-embed"
        assert variant.status().is_fresh


class TestContentChanges:
    def test_reinsert_unchanged_runs_nothing(self, store):
        table = make_table(store)
        table.insert(DOCS)
        calls_before = dict(CALLS)

        table.insert(DOCS)
        assert dict(CALLS) == calls_before, "unchanged re-insert must execute nothing, not even split"

    def test_content_change_rederives_only_the_changed_page(self, store):
        table = make_table(store)
        table.insert(DOCS)

        table.sync([{"doc_id": "d1", "text": "alpha|zeta"}, DOCS[1]])

        assert CALLS["split"] == 3, "d1 must re-split (its text changed)"
        assert CALLS["enrich"] == 6, "only the changed page re-enriches"
        assert CALLS["embed"] == 6, "only the changed page re-embeds"
        texts = {c["page_text"] for c in table.children("d1")}
        assert texts == {"alpha", "zeta"}


class TestChildStatus:
    def test_child_status_reports_stale_columns(self, store):
        make_table(store).insert(DOCS)

        report = make_table(store, embedder=Embedder("embed-b")).status()
        child = report.children[0]
        assert child.stale == 5
        assert child.stale_columns == (("page_vector", 5),)
