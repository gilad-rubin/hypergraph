"""Public acceptance tests for persisted Materialization Branches.

These tests stay at the public materialization seam and use the real LanceDB
store. They pin the six Slice A1 cases from Superposition PRD 0019.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TypedDict

import pytest

from hypergraph import AsyncRunner, Graph, GraphConfigError, node
from hypergraph.materialization import (
    LanceDBStore,
    MaterializationBranch,
    MaterializedArtifact,
)
from hypergraph.runners import SyncRunner

CALLS: defaultdict[str, int] = defaultdict(int)


class Chunk(TypedDict):
    chunk_id: str
    chunk_text: str


class Chunker:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def _config(self) -> dict[str, str]:
        return {"mode": self.mode}

    def split(self, text: str) -> list[str]:
        CALLS[f"chunk:{self.mode}"] += 1
        if self.mode == "words":
            return text.replace("|", " ").split()
        if self.mode == "segments":
            return text.split("|")
        return list(text.replace("|", ""))


class Embedder:
    def __init__(self, model: str) -> None:
        self.model = model

    def _config(self) -> dict[str, str]:
        return {"model": self.model}

    def embed(self, text: str) -> list[float]:
        CALLS[f"embed:{self.model}"] += 1
        return [float(len(self.model)), float(len(text))]


class Finalizer:
    def __init__(self, version: str) -> None:
        self.version = version

    def _config(self) -> dict[str, str]:
        return {"version": self.version}

    def finish(self, vector: list[float]) -> list[float]:
        CALLS[f"finalize:{self.version}"] += 1
        return [*vector, float(len(self.version))]


@node(output_name="pages_text")
def parse_pages(text: str) -> str:
    CALLS["parse"] += 1
    return text.strip().lower()


@node(output_name="chunks")
def chunk_pages(pages_text: str, chunker: Chunker) -> list[Chunk]:
    return [Chunk(chunk_id=f"c-{index}", chunk_text=part) for index, part in enumerate(chunker.split(pages_text))]


@node(output_name="normalized")
def normalize_chunk(chunk_text: str) -> str:
    CALLS["normalize"] += 1
    return chunk_text.strip().lower()


@node(output_name="vector")
def embed_chunk(normalized: str, embedder: Embedder) -> list[float]:
    return embedder.embed(normalized)


@node(output_name="search_vector")
def finalize_vector(vector: list[float], finalizer: Finalizer) -> list[float]:
    return finalizer.finish(vector)


DOCUMENTS = [
    {"doc_id": "d-1", "text": "Alpha beta|Gamma"},
    {"doc_id": "d-2", "text": "Delta epsilon|Zeta"},
]


def search_recipe(
    *,
    chunker: str = "words",
    embedder: str = "embed-a",
    finalizer: str | None = None,
) -> Graph:
    child_nodes = [normalize_chunk, embed_chunk]
    bindings: dict[str, object] = {
        "chunker": Chunker(chunker),
        "embedder": Embedder(embedder),
    }
    if finalizer is not None:
        child_nodes.append(finalize_vector)
        bindings["finalizer"] = Finalizer(finalizer)
    child = Graph(child_nodes, name="prepare_chunk")
    mapped = child.as_node(name="prepared_chunks").map_over("chunks", identity="chunk_id")
    return Graph([parse_pages, chunk_pages, mapped], name="search_index_recipe").bind(**bindings)


def content_table(store: LanceDBStore, *, with_finalizer: bool = False, runner=None):
    graph = search_recipe(finalizer="final-a" if with_finalizer else None)
    return graph.as_table(
        identity="doc_id",
        store=store,
        runner=runner or SyncRunner(),
        name="documents",
    )


def attach(
    table,
    attachment_id: str,
    graph: Graph,
    *,
    vector: str = "vector",
) -> MaterializationBranch:
    branch = table.attach(
        name=attachment_id,
        graph=graph,
        outputs={"text": "chunk_text", "vector": vector},
    )
    assert isinstance(branch, MaterializationBranch)
    assert isinstance(branch.output("vector"), MaterializedArtifact)
    return branch


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    CALLS.clear()


@pytest.fixture
def store_path(tmp_path) -> str:
    return str(tmp_path / "branch_store")


@pytest.fixture
def store(store_path) -> LanceDBStore:
    return LanceDBStore(store_path)


def test_embedder_only_change_reuses_documents_pages_and_chunks(store: LanceDBStore) -> None:
    table = content_table(store)
    table.insert(DOCUMENTS)
    baseline = attach(table, "baseline", search_recipe())
    before = dict(CALLS)

    candidate = attach(table, "embed-experiment", search_recipe(embedder="embed-b"))
    receipt = candidate.sync()

    assert receipt.updated == len(DOCUMENTS)
    assert CALLS["parse"] == before["parse"]
    assert CALLS["chunk:words"] == before["chunk:words"]
    assert CALLS["normalize"] == before["normalize"]
    assert CALLS["embed:embed-b"] == 6
    assert candidate.output("text").table == baseline.output("text").table
    assert candidate.output("text").column == baseline.output("text").column
    assert candidate.output("vector").table == baseline.output("vector").table
    assert candidate.output("vector").column != baseline.output("vector").column
    assert candidate.status().is_fresh
    index_spec = candidate.create_index("embed-experiment")
    assert index_spec["on"] == candidate.output("vector").table
    assert index_spec["text"] == candidate.output("text").column
    assert index_spec["vector"] == candidate.output("vector").column
    assert table.list_indexes()[0]["current"] is True
    hit = table.search([7.0, 5.0], index="embed-experiment", limit=1)[0]
    assert hit["doc_id"] in {"d-1", "d-2"}
    assert hit["chunk_text"] in {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}


def test_chunker_change_forks_at_the_chunk_grain(store: LanceDBStore) -> None:
    table = content_table(store)
    table.insert(DOCUMENTS)
    baseline = attach(table, "baseline", search_recipe())
    before = dict(CALLS)

    candidate = attach(table, "segment-experiment", search_recipe(chunker="segments"))
    receipt = candidate.sync()

    assert receipt.updated == len(DOCUMENTS)
    assert CALLS["parse"] == before["parse"]
    assert CALLS["chunk:words"] == before["chunk:words"]
    assert CALLS["chunk:segments"] == len(DOCUMENTS)
    assert CALLS["normalize"] == before["normalize"] + 4
    assert CALLS["embed:embed-a"] == before["embed:embed-a"] + 4
    assert candidate.output("vector").table != baseline.output("vector").table
    child_table = next(artifact for artifact in candidate.artifacts() if artifact.column is None)
    assert child_table.table == candidate.output("vector").table
    assert child_table.shared is False
    assert candidate.status().is_fresh


def test_last_step_only_change_derives_exactly_one_column(store: LanceDBStore) -> None:
    table = content_table(store, with_finalizer=True)
    table.insert(DOCUMENTS)
    baseline = attach(table, "baseline", search_recipe(finalizer="final-a"), vector="search_vector")
    before = dict(CALLS)

    candidate = attach(
        table,
        "final-experiment",
        search_recipe(finalizer="final-b"),
        vector="search_vector",
    )
    candidate.sync()

    assert CALLS["parse"] == before["parse"]
    assert CALLS["chunk:words"] == before["chunk:words"]
    assert CALLS["normalize"] == before["normalize"]
    assert CALLS["embed:embed-a"] == before["embed:embed-a"]
    assert CALLS["finalize:final-b"] == 6
    assert candidate.output("vector").table == baseline.output("vector").table
    assert candidate.output("vector").column != baseline.output("vector").column
    assert candidate.status().is_fresh


def test_identical_full_recipe_derives_nothing(store: LanceDBStore) -> None:
    table = content_table(store)
    table.insert(DOCUMENTS)
    first = attach(table, "first-reference", search_recipe())
    before = dict(CALLS)

    identical = attach(table, "second-reference", search_recipe())
    receipt = identical.sync()

    assert receipt.skipped == len(DOCUMENTS)
    assert dict(CALLS) == before
    assert identical.output("text").table == first.output("text").table
    assert identical.output("text").column == first.output("text").column
    assert identical.output("vector").table == first.output("vector").table
    assert identical.output("vector").column == first.output("vector").column
    assert first.output("vector").shared
    assert identical.output("vector").shared


def test_root_delete_converges_every_registered_branch_without_open_handles(
    store: LanceDBStore,
    store_path: str,
) -> None:
    table = content_table(store)
    table.insert(DOCUMENTS)
    attach(table, "embed-experiment", search_recipe(embedder="embed-b")).sync()
    attach(table, "segment-experiment", search_recipe(chunker="segments")).sync()

    fresh_store = LanceDBStore(store_path)
    fresh_root = content_table(fresh_store)
    fresh_root.delete("d-1")

    reopened_embed = attach(fresh_root, "embed-experiment", search_recipe(embedder="embed-b"))
    reopened_segments = attach(fresh_root, "segment-experiment", search_recipe(chunker="segments"))
    assert reopened_embed.status().total == 1
    assert reopened_embed.status().children[0].total == 3
    assert reopened_segments.status().total == 1
    assert reopened_segments.status().children[0].total == 2
    assert reopened_embed.status().is_fresh
    assert reopened_segments.status().is_fresh


def test_two_same_grain_branches_get_distinct_physical_namespaces(store: LanceDBStore, store_path: str) -> None:
    table = content_table(store)
    table.insert(DOCUMENTS)

    segments = attach(table, "segments-id", search_recipe(chunker="segments"))
    characters = attach(table, "characters-id", search_recipe(chunker="characters"))
    segments.sync()
    characters.sync()

    assert segments.output("vector").table != characters.output("vector").table
    assert segments.output("text").table != characters.output("text").table
    fresh_root = content_table(LanceDBStore(store_path))
    reopened_segments = attach(fresh_root, "segments-id", search_recipe(chunker="segments"))
    reopened_characters = attach(fresh_root, "characters-id", search_recipe(chunker="characters"))
    assert reopened_segments.output("vector").table == segments.output("vector").table
    assert reopened_characters.output("vector").table == characters.output("vector").table
    assert reopened_segments.status().is_fresh
    assert reopened_characters.status().is_fresh


async def test_materialization_branch_preserves_async_runner_contract(store: LanceDBStore) -> None:
    table = content_table(store, runner=AsyncRunner())
    await table.insert(DOCUMENTS)
    candidate = attach(table, "async-embed", search_recipe(embedder="embed-b"))

    receipt = await candidate.sync()

    assert receipt.updated == len(DOCUMENTS)
    assert candidate.status().is_fresh


def test_attach_rejects_reusing_an_id_for_a_different_recipe(store: LanceDBStore) -> None:
    table = content_table(store)
    table.insert(DOCUMENTS)
    attach(table, "stable-id", search_recipe())

    with pytest.raises(GraphConfigError, match="different recipe"):
        attach(table, "stable-id", search_recipe(embedder="embed-b"))


def test_attach_validates_stable_id_and_outputs(store: LanceDBStore) -> None:
    table = content_table(store)

    with pytest.raises(GraphConfigError, match="stable attachment name"):
        table.attach("", graph=search_recipe(), outputs={"vector": "vector"})
    with pytest.raises(GraphConfigError, match="requires terminal outputs"):
        table.attach("stable-id", graph=search_recipe(), outputs={})


def test_create_index_rejects_outputs_from_different_grains(store: LanceDBStore) -> None:
    table = content_table(store)
    table.insert(DOCUMENTS)
    branch = table.attach(
        "split-output-id",
        graph=search_recipe(),
        outputs={"text": "pages_text", "vector": "vector"},
    )

    with pytest.raises(GraphConfigError, match="different grains"):
        branch.create_index("invalid")
