"""Tests for child-row operations: filter_children, set_children, delete_children."""

from __future__ import annotations

from typing import TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import HyperTable
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.materialization._store import clear_store_cache
from hypergraph.runners import SyncRunner

# ---------------------------------------------------------------------------
# Shared graph: video → utterances (parent → children)
# ---------------------------------------------------------------------------


class Embedder:
    def __init__(self, dim: int = 3):
        self.dim = dim

    def _config(self):
        return {"dim": self.dim}

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


@node(output_name="audio_path")
def extract_audio(path: str) -> str:
    return f"/tmp/{path.split('/')[-1]}.wav"


@node(output_name="transcript")
def transcribe(audio_path: str) -> str:
    return f"transcript of {audio_path}"


class Utterance(TypedDict):
    utterance_id: str
    text: str
    speaker: str


@node(output_name="utterances")
def split_utterances(transcript: str) -> list[Utterance]:
    return [
        Utterance(utterance_id="u0", text="hello", speaker="Alice"),
        Utterance(utterance_id="u1", text="world", speaker="Bob"),
    ]


process_utterance = Graph([clean, embed_text], name="process_utterance")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_stores():
    clear_store_cache()
    yield
    clear_store_cache()


@pytest.fixture
def store(tmp_path):
    return LanceDBStore(str(tmp_path / "child_ops_store"))


@pytest.fixture
def embedder():
    return Embedder()


@pytest.fixture
def table(store, embedder):
    """A HyperTable with two parents, each having two child utterances."""
    t = (
        HyperTable(
            [
                extract_audio,
                transcribe,
                split_utterances,
                process_utterance.as_node().map_over("utterances", identity="utterance_id"),
            ],
            identity="video_id",
            store=store,
        )
        .bind(embedder=embedder)
        .with_runner(SyncRunner())
    )
    t.insert(video_id="v1", path="/data/a.mp4")
    t.insert(video_id="v2", path="/data/b.mp4")
    return t


# ---------------------------------------------------------------------------
# filter_children
# ---------------------------------------------------------------------------


class TestFilterChildren:
    def test_filter_by_predicate(self, table):
        rows = table.filter_children(where=[("clean_text", "eq", "hello")])
        assert len(rows) == 2  # one "hello" utterance per parent
        assert all(r["clean_text"] == "hello" for r in rows)

    def test_filter_by_parent(self, table):
        rows = table.filter_children(where=[("_parent_id", "eq", "v1")])
        assert len(rows) == 2
        assert all(r["_parent_id"] == "v1" for r in rows)

    def test_filter_no_match_returns_empty(self, table):
        rows = table.filter_children(where=[("clean_text", "eq", "nonexistent")])
        assert rows == []

    def test_filter_with_limit(self, table):
        rows = table.filter_children(where=[("clean_text", "eq", "hello")], limit=1)
        assert len(rows) == 1

    def test_filter_no_children_table(self, store):
        flat = HyperTable(
            [clean],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())
        flat.insert(doc_id="d1", text="hello")
        assert flat.filter_children() == []


# ---------------------------------------------------------------------------
# set_children
# ---------------------------------------------------------------------------


class TestSetChildren:
    def test_set_updates_matching_children(self, table):
        count = table.set_children(
            where=[("_parent_id", "eq", "v1"), ("clean_text", "eq", "hello")],
            station="NICU",
        )
        assert count == 1
        rows = table.filter_children(where=[("_parent_id", "eq", "v1"), ("clean_text", "eq", "hello")])
        assert rows[0]["station"] == "NICU"

    def test_set_does_not_touch_non_matching(self, table):
        table.set_children(
            where=[("_parent_id", "eq", "v1")],
            station="ER",
        )
        v2_rows = table.filter_children(where=[("_parent_id", "eq", "v2")])
        assert all("station" not in r or r.get("station") != "ER" for r in v2_rows)

    def test_set_no_match_returns_zero(self, table):
        count = table.set_children(
            where=[("clean_text", "eq", "nonexistent")],
            station="X",
        )
        assert count == 0

    def test_set_no_children_table(self, store):
        flat = HyperTable(
            [clean],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())
        flat.insert(doc_id="d1", text="hello")
        assert flat.set_children(where=[("doc_id", "eq", "d1")], tag="x") == 0

    def test_set_children_scoped_to_parent(self, table):
        """set_children for one parent must not delete another parent's children
        that share the same child identity value."""
        # Both v1 and v2 have children with utterance_id "u0" and "u1".
        # Updating v1's children should leave v2's children intact.
        v2_before = table.filter_children(where=[("_parent_id", "eq", "v2")])
        assert len(v2_before) == 2

        table.set_children(
            where=[("_parent_id", "eq", "v1")],
            reviewed=True,
        )

        # v2's children must still be present and unmodified
        v2_after = table.filter_children(where=[("_parent_id", "eq", "v2")])
        assert len(v2_after) == 2
        assert all("reviewed" not in r or r.get("reviewed") is not True for r in v2_after)

        # v1's children should have the new metadata
        v1_after = table.filter_children(where=[("_parent_id", "eq", "v1")])
        assert len(v1_after) == 2
        assert all(r["reviewed"] is True for r in v1_after)


# ---------------------------------------------------------------------------
# delete_children
# ---------------------------------------------------------------------------


class TestDeleteChildren:
    def test_delete_by_predicate(self, table):
        count = table.delete_children(where=[("_parent_id", "eq", "v1"), ("clean_text", "eq", "hello")])
        assert count == 1
        remaining = table.filter_children(where=[("_parent_id", "eq", "v1")])
        assert len(remaining) == 1
        assert remaining[0]["clean_text"] == "world"

    def test_delete_all_children_of_parent(self, table):
        count = table.delete_children(where=[("_parent_id", "eq", "v1")])
        assert count == 2
        assert table.filter_children(where=[("_parent_id", "eq", "v1")]) == []
        assert len(table.filter_children(where=[("_parent_id", "eq", "v2")])) == 2

    def test_delete_no_match_returns_zero(self, table):
        count = table.delete_children(where=[("clean_text", "eq", "nonexistent")])
        assert count == 0

    def test_delete_no_children_table(self, store):
        flat = HyperTable(
            [clean],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())
        flat.insert(doc_id="d1", text="hello")
        assert flat.delete_children(where=[("doc_id", "eq", "d1")]) == 0
