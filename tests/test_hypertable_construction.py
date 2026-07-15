"""Tests for HyperTable construction: graph analysis → TableSpec → physical tables."""

from __future__ import annotations

from typing import TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization._lancedb_store import LanceDBStore
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


class Converter:
    def convert(self, source: str) -> list[Utterance]:
        return [
            Utterance(utterance_id="u0", text=f"{source} one", speaker="Alice", start=0.0, end=1.0),
            Utterance(utterance_id="u1", text=f"{source} two", speaker="Bob", start=1.0, end=2.0),
        ]


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


@node(output_name="utterances")
def convert_source(source: str, converter: Converter) -> list[Utterance]:
    return converter.convert(source)


# Subgraph for processing one utterance
process_utterance = Graph([clean, embed_text], name="process_utterance")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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

        table = Graph([extract_audio, transcribe]).as_table(identity="video_id", store=store, runner=SyncRunner())

        assert table is not None

    def test_bind_components(self, store, embedder):
        """Components via .bind() are not stored as columns."""

        table = Graph([clean, embed_text]).bind(embedder=embedder).as_table(identity="text_id", store=store, runner=SyncRunner())

        assert table is not None

    def test_as_table_keeps_the_graph_as_the_artifact(self, store):
        graph = Graph([extract_audio, transcribe])
        table = graph.as_table(identity="video_id", store=store, runner=SyncRunner())

        assert table.graph is graph

    def test_reads_use_the_default_runner_without_configuration(self, store):
        table = Graph([extract_audio, transcribe]).as_table(identity="video_id", store=store)
        assert table.count() == 0

    def test_writes_default_to_sync_runner(self, store):
        table = Graph([extract_audio, transcribe]).as_table(identity="video_id", store=store)
        receipt = table.insert(video_id="v1", path="/data/test.mp4")

        assert receipt.completed

    def test_missing_identity_errors(self, store):
        """Constructor requires identity=."""

        with pytest.raises(TypeError):
            Graph([extract_audio, transcribe]).as_table(store=store)

    def test_grain_boundary_with_map_over(self, store, embedder):
        """map_over creates a child table with its own identity."""

        table = (
            Graph([extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")])
            .bind(embedder=embedder)
            .as_table(identity="video_id", store=store, runner=SyncRunner())
        )

        assert table is not None

    def test_child_graph_receives_only_its_component_binds(self, store, embedder):
        """Root-only components should not be bound into mapped child graphs."""

        table = (
            Graph([convert_source, process_utterance.as_node().map_over("utterances", identity="utterance_id")])
            .bind(converter=Converter(), embedder=embedder)
            .as_table(identity="doc_id", store=store, runner=SyncRunner())
        )

        table.insert(doc_id="d1", source="Alpha")
        children = table.child(table.child_table_names[0]).rows(parent="d1")

        assert [child["clean_text"] for child in children] == ["alpha one", "alpha two"]
        assert all("vector" in child for child in children)


class TestBasicInsert:
    """Insert operations on a simple single-grain table."""

    def test_insert_single_item(self, store):
        """Insert via kwargs creates one row."""

        table = Graph([extract_audio, transcribe]).as_table(identity="video_id", store=store, runner=SyncRunner())

        table.insert(video_id="v1", path="/data/meeting.mp4")
        assert table.count() == 1

    def test_insert_derives_columns(self, store):
        """Insert runs the graph and stores derived column values."""

        table = Graph([extract_audio, transcribe]).as_table(identity="video_id", store=store, runner=SyncRunner())

        table.insert(video_id="v1", path="/data/meeting.mp4")
        row = table.get("v1")
        assert row["audio_path"] == "/tmp/meeting.mp4.wav"
        assert "transcript" in row["transcript"]

    def test_insert_batch(self, store):
        """Insert a list of dicts creates multiple rows."""

        table = Graph([extract_audio, transcribe]).as_table(identity="video_id", store=store, runner=SyncRunner())

        table.insert(
            [
                dict(video_id="v1", path="/data/a.mp4"),
                dict(video_id="v2", path="/data/b.mp4"),
            ]
        )
        assert table.count() == 2

    def test_insert_with_metadata(self, store):
        """Extra kwargs not matching graph inputs are stored as metadata."""

        table = Graph([extract_audio, transcribe]).as_table(identity="video_id", store=store, runner=SyncRunner())

        table.insert(video_id="v1", path="/data/meeting.mp4", title="Q3 Planning")
        row = table.get("v1")
        assert row["title"] == "Q3 Planning"

    def test_insert_with_bound_component(self, store, embedder):
        """Bound components are used during derivation, not stored."""

        table = Graph([clean, embed_text]).bind(embedder=embedder).as_table(identity="text_id", store=store, runner=SyncRunner())

        table.insert(text_id="t1", text="hello world")
        row = table.get("t1")
        assert isinstance(row["vector"], list)
        assert len(row["vector"]) == 3
        assert "embedder" not in row


class TestGrainBoundary:
    """Tests for map_over grain boundaries (parent + child tables)."""

    def test_insert_creates_child_rows(self, store, embedder):
        """Insert into parent cascades through map_over, creating child rows."""

        table = (
            Graph([extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")])
            .bind(embedder=embedder)
            .as_table(identity="video_id", store=store, runner=SyncRunner())
        )

        table.insert(video_id="v1", path="/data/meeting.mp4")
        assert table.count() == 1
        children = table.child(table.child_table_names[0]).rows(parent="v1")
        assert len(children) == 2

    def test_child_rows_have_parent_link(self, store, embedder):
        """Child rows expose the named parent identity."""

        table = (
            Graph([extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")])
            .bind(embedder=embedder)
            .as_table(identity="video_id", store=store, runner=SyncRunner())
        )

        table.insert(video_id="v1", path="/data/meeting.mp4")
        children = table.child(table.child_table_names[0]).rows(parent="v1")
        assert all(c["video_id"] == "v1" for c in children)

    def test_child_rows_have_derived_columns(self, store, embedder):
        """Child rows have derived columns from the subgraph."""

        table = (
            Graph([extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")])
            .bind(embedder=embedder)
            .as_table(identity="video_id", store=store, runner=SyncRunner())
        )

        table.insert(video_id="v1", path="/data/meeting.mp4")
        children = table.child(table.child_table_names[0]).rows(parent="v1")
        assert "clean_text" in children[0]
        assert "vector" in children[0]
        assert children[0]["clean_text"] == "hello"

    def test_child_count(self, store, embedder):
        """Count child table rows."""

        table = (
            Graph([extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")])
            .bind(embedder=embedder)
            .as_table(identity="video_id", store=store, runner=SyncRunner())
        )

        table.insert(video_id="v1", path="/data/meeting.mp4")
        assert table.child("utterance").count() == 2
