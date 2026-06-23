"""Tests for HyperTable construction: graph analysis → TableSpec → physical tables."""

from __future__ import annotations

from typing import TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.materialization._store import clear_store_cache
from hypergraph.runners import SyncRunner

# ---------------------------------------------------------------------------
# Test nodes and components
# ---------------------------------------------------------------------------


class Utterance(TypedDict):
    utterance_id: str
    text: str
    speaker: str
    start: float
    end: float


class Embedder:
    def __init__(self, model_name: str = "test-embed"):
        self.model_name = model_name

    def _config(self):
        return {"model": self.model_name}

    def embed(self, text: str) -> list[float]:
        return [float(ord(c)) for c in text[:3]]


@node(output_name="audio_path")
def extract_audio(path: str) -> str:
    return f"/tmp/{path.split('/')[-1]}.wav"


@node(output_name="transcript")
def transcribe(audio_path: str) -> str:
    return f"transcript of {audio_path}"


@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()


@node(output_name="vector")
def embed_text(clean_text: str, embedder: Embedder) -> list[float]:
    return embedder.embed(clean_text)


@node(output_name="utterances")
def split_utterances(transcript: str) -> list[Utterance]:
    return [
        Utterance(utterance_id="u0", text="hello", speaker="Alice", start=0.0, end=1.0),
        Utterance(utterance_id="u1", text="world", speaker="Bob", start=1.0, end=2.0),
    ]


# Subgraph for processing one utterance
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
    return LanceDBStore(str(tmp_path / "test_store"))


@pytest.fixture
def embedder():
    return Embedder()


# ---------------------------------------------------------------------------
# Construction tests
# ---------------------------------------------------------------------------


class TestConstruction:
    """HyperTable construction: graph analysis, schema inference."""

    def test_basic_construction(self, store, embedder):
        """Simple linear graph → single-grain table with source + derived columns."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [extract_audio, transcribe],
            identity="video_id",
            store=store,
        ).with_runner(SyncRunner())

        assert table is not None

    def test_bind_components(self, store, embedder):
        """Components via .bind() are not stored as columns."""
        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [clean, embed_text],
                identity="text_id",
                store=store,
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        assert table is not None

    def test_with_runner_returns_new_instance(self, store):
        """with_runner returns a new immutable instance."""
        from hypergraph.materialization import HyperTable

        base = HyperTable(
            [extract_audio, transcribe],
            identity="video_id",
            store=store,
        )
        with_runner = base.with_runner(SyncRunner())
        assert with_runner is not base

    def test_read_without_runner(self, store):
        """Read operations work without a runner set."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [extract_audio, transcribe],
            identity="video_id",
            store=store,
        )
        # count should work without a runner
        assert table.count() == 0

    def test_write_without_runner_errors(self, store):
        """Write operations error without a runner."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [extract_audio, transcribe],
            identity="video_id",
            store=store,
        )
        with pytest.raises(RuntimeError, match="runner"):
            table.insert(video_id="v1", path="/data/test.mp4")

    def test_missing_identity_errors(self, store):
        """Constructor requires identity=."""
        from hypergraph.materialization import HyperTable

        with pytest.raises(TypeError):
            HyperTable(
                [extract_audio, transcribe],
                store=store,
            )

    def test_grain_boundary_with_map_over(self, store, embedder):
        """map_over creates a child table with its own identity."""
        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
                identity="video_id",
                store=store,
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        assert table is not None


class TestBasicInsert:
    """Insert operations on a simple single-grain table."""

    def test_insert_single_item(self, store):
        """Insert via kwargs creates one row."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [extract_audio, transcribe],
            identity="video_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(video_id="v1", path="/data/meeting.mp4")
        assert table.count() == 1

    def test_insert_derives_columns(self, store):
        """Insert runs the graph and stores derived column values."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [extract_audio, transcribe],
            identity="video_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(video_id="v1", path="/data/meeting.mp4")
        row = table.get("v1")
        assert row["audio_path"] == "/tmp/meeting.mp4.wav"
        assert "transcript" in row["transcript"]

    def test_insert_batch(self, store):
        """Insert a list of dicts creates multiple rows."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [extract_audio, transcribe],
            identity="video_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(
            [
                dict(video_id="v1", path="/data/a.mp4"),
                dict(video_id="v2", path="/data/b.mp4"),
            ]
        )
        assert table.count() == 2

    def test_insert_with_metadata(self, store):
        """Extra kwargs not matching graph inputs are stored as metadata."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [extract_audio, transcribe],
            identity="video_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(video_id="v1", path="/data/meeting.mp4", title="Q3 Planning")
        row = table.get("v1")
        assert row["title"] == "Q3 Planning"

    def test_insert_with_bound_component(self, store, embedder):
        """Bound components are used during derivation, not stored."""
        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [clean, embed_text],
                identity="text_id",
                store=store,
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(text_id="t1", text="hello world")
        row = table.get("t1")
        assert isinstance(row["vector"], list)
        assert len(row["vector"]) == 3
        assert "embedder" not in row


class TestGrainBoundary:
    """Tests for map_over grain boundaries (parent + child tables)."""

    def test_insert_creates_child_rows(self, store, embedder):
        """Insert into parent cascades through map_over, creating child rows."""
        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
                identity="video_id",
                store=store,
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(video_id="v1", path="/data/meeting.mp4")
        assert table.count() == 1
        children = table.children("v1")
        assert len(children) == 2

    def test_child_rows_have_parent_link(self, store, embedder):
        """Child rows have _parent_id linking back to parent."""
        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
                identity="video_id",
                store=store,
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(video_id="v1", path="/data/meeting.mp4")
        children = table.children("v1")
        assert all(c["_parent_id"] == "v1" for c in children)

    def test_child_rows_have_derived_columns(self, store, embedder):
        """Child rows have derived columns from the subgraph."""
        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
                identity="video_id",
                store=store,
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(video_id="v1", path="/data/meeting.mp4")
        children = table.children("v1")
        assert "clean_text" in children[0]
        assert "vector" in children[0]
        assert children[0]["clean_text"] == "hello"

    def test_child_count(self, store, embedder):
        """Count child table rows."""
        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
                identity="video_id",
                store=store,
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(video_id="v1", path="/data/meeting.mp4")
        assert table.count("utterance") == 2
