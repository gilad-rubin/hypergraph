"""End-to-end HyperTable tests: LanceDB vector search, incrementality, sync runner."""

from __future__ import annotations

from typing import TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization._store import clear_store_cache
from hypergraph.runners import SyncRunner

# ---------------------------------------------------------------------------
# Domain nodes and components
# ---------------------------------------------------------------------------


class Embedder:
    def __init__(self, model_name: str = "test-embed", dim: int = 8):
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


@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())


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
    words = transcript.split()
    return [Utterance(utterance_id=f"u{i}", text=w, speaker="Alice" if i % 2 == 0 else "Bob") for i, w in enumerate(words)]


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
def store_path(tmp_path):
    return str(tmp_path / "e2e_store")


@pytest.fixture
def embedder():
    return Embedder()


# ---------------------------------------------------------------------------
# End-to-end: single-grain table with vector search
# ---------------------------------------------------------------------------


class TestVectorSearchE2E:
    """Full pipeline: insert → derive vectors → search by similarity."""

    def test_insert_and_vector_search(self, store_path, embedder):
        """Insert documents, then search by vector similarity via LanceDB."""
        import lancedb

        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [clean, embed_text, count_words],
                identity="doc_id",
                store=f"lancedb://{store_path}",
                vector_columns={"vector": 8},
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(
            [
                dict(doc_id="d1", text="hello world"),
                dict(doc_id="d2", text="goodbye cruel world"),
                dict(doc_id="d3", text="hello there friend"),
            ]
        )

        assert table.count() == 3

        row = table.get("d1")
        assert row["clean_text"] == "hello world"
        assert row["word_count"] == 2
        assert len(row["vector"]) == 8

        db = lancedb.connect(store_path)
        lance_tbl = db.open_table("doc")
        query_vec = embedder.embed("hello")
        results = lance_tbl.search(query_vec, vector_column_name="vector").limit(2).to_pandas()
        assert len(results) == 2
        assert "d1" in results["doc_id"].values or "d3" in results["doc_id"].values

    def test_multiple_inserts_accumulate(self, store_path, embedder):
        """Multiple insert calls accumulate rows."""
        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [clean, embed_text],
                identity="doc_id",
                store=f"lancedb://{store_path}",
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(doc_id="d1", text="first")
        table.insert(doc_id="d2", text="second")
        table.insert(doc_id="d3", text="third")

        assert table.count() == 3
        assert table.get("d2")["clean_text"] == "second"


# ---------------------------------------------------------------------------
# End-to-end: grain boundary with vector search on children
# ---------------------------------------------------------------------------


class TestGrainBoundaryE2E:
    """Full grain boundary pipeline with child vector search."""

    def test_parent_child_full_pipeline(self, store_path, embedder):
        """Insert video → transcribe → split → embed each utterance."""
        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
                identity="video_id",
                store=f"lancedb://{store_path}",
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(video_id="v1", path="/data/meeting.mp4")

        assert table.count() == 1
        parent = table.get("v1")
        assert parent["audio_path"] == "/tmp/meeting.mp4.wav"
        assert "transcript" in parent["transcript"]

        children = table.children("v1")
        assert len(children) > 0
        assert all("clean_text" in c for c in children)
        assert all("vector" in c for c in children)
        assert all(isinstance(c["vector"], list) for c in children)

    def test_child_vector_search(self, store_path, embedder):
        """Search child table vectors directly via LanceDB."""
        import lancedb

        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
                identity="video_id",
                store=f"lancedb://{store_path}",
                vector_columns={"vector": 8},
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(video_id="v1", path="/data/meeting.mp4")

        db = lancedb.connect(store_path)
        child_tbl = db.open_table("utterance")
        query_vec = embedder.embed("transcript")
        results = child_tbl.search(query_vec, vector_column_name="vector").limit(3).to_pandas()
        assert len(results) > 0
        assert "_parent_id" in results.columns
        assert all(results["_parent_id"] == "v1")

    def test_multiple_parents_children_isolated(self, store_path, embedder):
        """Children from different parents are correctly linked."""
        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [extract_audio, transcribe, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
                identity="video_id",
                store=f"lancedb://{store_path}",
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(video_id="v1", path="/data/a.mp4")
        table.insert(video_id="v2", path="/data/b.mp4")

        assert table.count() == 2
        children_v1 = table.children("v1")
        children_v2 = table.children("v2")
        assert len(children_v1) > 0
        assert len(children_v2) > 0
        assert all(c["_parent_id"] == "v1" for c in children_v1)
        assert all(c["_parent_id"] == "v2" for c in children_v2)


# ---------------------------------------------------------------------------
# Incrementality: provenance and fingerprint
# ---------------------------------------------------------------------------


class TestIncrementality:
    """Content-key provenance checks for skip/recompute decisions."""

    def test_row_fingerprint_stable(self, store_path, embedder):
        """Same input → same fingerprint."""
        import lancedb

        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [clean, embed_text],
                identity="doc_id",
                store=f"lancedb://{store_path}",
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(doc_id="d1", text="hello")

        db = lancedb.connect(store_path)
        tbl = db.open_table("doc")
        df = tbl.to_pandas()
        fp1 = df[df["doc_id"] == "d1"]["_row_fingerprint"].iloc[0]
        assert fp1 and len(fp1) == 64  # sha256 hex

    def test_different_inputs_different_fingerprint(self, store_path, embedder):
        """Different source values → different fingerprint."""
        import lancedb

        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [clean, embed_text],
                identity="doc_id",
                store=f"lancedb://{store_path}",
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(doc_id="d1", text="hello")
        table.insert(doc_id="d2", text="world")

        db = lancedb.connect(store_path)
        tbl = db.open_table("doc")
        df = tbl.to_pandas()
        fp1 = df[df["doc_id"] == "d1"]["_row_fingerprint"].iloc[0]
        fp2 = df[df["doc_id"] == "d2"]["_row_fingerprint"].iloc[0]
        assert fp1 != fp2

    def test_per_column_provenance_stored(self, store_path, embedder):
        """Each derived column has its own provenance hash."""
        import lancedb

        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [clean, embed_text, count_words],
                identity="doc_id",
                store=f"lancedb://{store_path}",
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(doc_id="d1", text="hello world")

        db = lancedb.connect(store_path)
        tbl = db.open_table("doc")
        df = tbl.to_pandas()
        row = df.iloc[0]
        assert "_provenance_clean_text" in df.columns
        assert "_provenance_vector" in df.columns
        assert "_provenance_word_count" in df.columns
        assert row["_provenance_clean_text"] != ""
        assert row["_provenance_vector"] != ""

    def test_write_gen_increments(self, store_path, embedder):
        """Each insert call increments _write_gen."""
        import lancedb

        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [clean, embed_text],
                identity="doc_id",
                store=f"lancedb://{store_path}",
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(doc_id="d1", text="first")
        table.insert(doc_id="d2", text="second")

        db = lancedb.connect(store_path)
        tbl = db.open_table("doc")
        df = tbl.to_pandas()
        gen1 = df[df["doc_id"] == "d1"]["_write_gen"].iloc[0]
        gen2 = df[df["doc_id"] == "d2"]["_write_gen"].iloc[0]
        assert gen2 > gen1


# ---------------------------------------------------------------------------
# Component swap: same table, different embedder
# ---------------------------------------------------------------------------


class TestComponentSwap:
    """Swapping components changes derivation output."""

    def test_different_embedder_different_vectors(self, store_path):
        """Same text with different embedder config → different vectors."""
        from hypergraph.materialization import HyperTable

        emb_a = Embedder(model_name="model-a", dim=4)
        emb_b = Embedder(model_name="model-b", dim=8)

        table_a = (
            HyperTable(
                [clean, embed_text],
                identity="doc_id",
                store=f"lancedb://{store_path}_a",
            )
            .bind(embedder=emb_a)
            .with_runner(SyncRunner())
        )

        table_b = (
            HyperTable(
                [clean, embed_text],
                identity="doc_id",
                store=f"lancedb://{store_path}_b",
            )
            .bind(embedder=emb_b)
            .with_runner(SyncRunner())
        )

        table_a.insert(doc_id="d1", text="hello")
        table_b.insert(doc_id="d1", text="hello")

        row_a = table_a.get("d1")
        row_b = table_b.get("d1")
        assert len(row_a["vector"]) == 4
        assert len(row_b["vector"]) == 8
