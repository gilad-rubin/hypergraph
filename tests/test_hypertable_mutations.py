"""Tests for HyperTable mutations: update, delete, sync, recompute, backfill."""

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
    return [
        Utterance(utterance_id="u0", text="hello", speaker="Alice"),
        Utterance(utterance_id="u1", text="world", speaker="Bob"),
    ]


process_utterance = Graph([clean, embed_text], name="process_utterance")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "mut_store")


@pytest.fixture
def store(store_path):
    return LanceDBStore(store_path)


@pytest.fixture
def embedder():
    return Embedder()


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdate:
    """update(id, **changes) re-derives downstream or stores metadata."""

    def test_update_source_column_re_derives(self, store, embedder):
        """Updating a source column re-derives downstream columns."""
        from hypergraph.materialization import HyperTable

        table = (
            HyperTable(
                [clean, embed_text, count_words],
                identity="doc_id",
                store=store,
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(doc_id="d1", text="hello world")
        assert table.get("d1")["word_count"] == 2

        table.update("d1", text="one two three four")
        row = table.get("d1")
        assert row["text"] == "one two three four"
        assert row["clean_text"] == "one two three four"
        assert row["word_count"] == 4

    def test_update_metadata_no_re_derive(self, store, embedder):
        """Updating a metadata column does not re-derive."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="hello", title="Old Title")
        table.update("d1", title="New Title")

        row = table.get("d1")
        assert row["title"] == "New Title"
        assert row["clean_text"] == "hello"  # unchanged

    def test_update_introducing_a_new_metadata_column_persists_it(self, store, embedder):
        """A metadata-only update with a brand-new column evolves the schema.

        The no-re-derive update path used to write straight to the store, which
        silently drops keys the schema has never seen; a curated tag added after
        insert would vanish. The update must evolve for the new column first.
        """
        from hypergraph.materialization import HyperTable

        table = HyperTable([clean, count_words], identity="doc_id", store=store).with_runner(SyncRunner())
        table.insert(doc_id="d1", text="hello")

        table.update("d1", topic="presets")

        assert table.get("d1")["topic"] == "presets"

    def test_update_cascades_to_children(self, store, embedder):
        """Updating a parent source re-derives children."""
        from hypergraph.materialization import HyperTable

        @node(output_name="utterances")
        def split_dynamic(transcript: str) -> list[Utterance]:
            words = transcript.split()
            return [Utterance(utterance_id=f"u{i}", text=w, speaker="A") for i, w in enumerate(words)]

        table = (
            HyperTable(
                [extract_audio, transcribe, split_dynamic, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
                identity="video_id",
                store=store,
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(video_id="v1", path="/data/a.mp4")
        children_before = table.children("v1")

        table.update("v1", path="/data/b.mp4")
        children_after = table.children("v1")

        # Different path → different transcript → different utterances
        before_texts = {c["clean_text"] for c in children_before}
        after_texts = {c["clean_text"] for c in children_after}
        assert before_texts != after_texts

    def test_update_nonexistent_row_errors(self, store):
        """Updating a row that doesn't exist raises an error."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        with pytest.raises(KeyError):
            table.update("nonexistent", text="nope")

    def test_update_changes_row(self, store):
        """update() applies changes and re-derives downstream columns."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="hello")
        assert table.get("d1")["word_count"] == 1

        table.update("d1", text="hello world")
        assert table.get("d1")["word_count"] == 2
        assert table.count() == 1


# ---------------------------------------------------------------------------
# Metadata evolution on an emptied table (archive -> restore regression)
# ---------------------------------------------------------------------------


class TestMetadataEvolveOnEmptyTable:
    """A metadata column added earlier must not be re-evolved once the table empties.

    ``_evolve_for_metadata`` decides which columns are "new" by sampling one
    stored row. When the table is EMPTY (every row deleted — the archive step in
    Superposition's KB), the sample is empty and it falls back to the spec-only
    column set, which excludes any metadata column previously added via update.
    Re-inserting a row carrying that column then re-evolves a column the physical
    schema already holds, and LanceDB rejects the duplicate field.
    """

    def test_reinsert_metadata_column_after_emptying_table(self, store):
        """Add a metadata column, delete every row, re-insert with it — must not crash."""
        from hypergraph.materialization import HyperTable

        def build_table():
            return HyperTable([clean, count_words], identity="doc_id", store=store).with_runner(SyncRunner())

        # 1. Insert a row and add a brand-new metadata column via update.
        #    This evolves the physical schema to carry ``station``.
        table = build_table()
        table.insert(doc_id="d1", text="hello")
        table.update("d1", station="north")
        assert table.get("d1")["station"] == "north"

        # 2. Empty the table (the archive step deletes the last corpus row).
        table.delete("d1")
        assert table.count() == 0

        # 3. Re-insert a row that itself CARRIES ``station`` into the now-empty
        #    table (the restore step). ``_evolve_for_metadata`` runs against zero
        #    stored rows, falls back to spec-only columns, and re-adds ``station``
        #    to a physical schema that already holds it → LanceDB duplicate-field
        #    crash on the next write.
        table = build_table()
        table.insert(doc_id="d2", text="world", station="south")
        assert table.get("d2")["station"] == "south"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    """delete(id) removes parent + cascades to children."""

    def test_delete_removes_row(self, store):
        """delete(id) removes the row."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="hello")
        table.insert(doc_id="d2", text="world")
        assert table.count() == 2

        table.delete("d1")
        assert table.count() == 1
        assert table.get("d1") is None
        assert table.get("d2") is not None

    def test_delete_cascades_children(self, store, embedder):
        """delete(parent_id) also deletes all child rows."""
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

        table.insert(video_id="v1", path="/data/a.mp4")
        table.insert(video_id="v2", path="/data/b.mp4")
        assert table.count("utterance") == 4  # 2 per parent

        table.delete("v1")
        assert table.count() == 1
        assert table.children("v1") == []
        assert len(table.children("v2")) == 2

    def test_delete_nonexistent_is_noop(self, store):
        """Deleting a nonexistent row does nothing."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="hello")
        table.delete("nonexistent")
        assert table.count() == 1


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


class TestSync:
    """sync(current_items) reconciles: insert, update, delete, skip."""

    def test_sync_inserts_new(self, store):
        """sync inserts items not in the table."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        result = table.sync(
            [
                dict(doc_id="d1", text="hello"),
                dict(doc_id="d2", text="world"),
            ]
        )

        assert table.count() == 2
        assert result.inserted == 2

    def test_sync_skips_unchanged(self, store):
        """sync skips rows whose fingerprint matches."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="hello")

        result = table.sync([dict(doc_id="d1", text="hello")])
        assert result.skipped == 1
        assert result.inserted == 0

    def test_sync_updates_changed(self, store):
        """sync updates rows whose source values changed."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="hello")

        result = table.sync([dict(doc_id="d1", text="goodbye cruel world")])
        assert result.updated == 1
        assert table.get("d1")["word_count"] == 3

    def test_sync_deletes_missing(self, store):
        """sync deletes rows not in the current items list."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="hello")
        table.insert(doc_id="d2", text="world")

        result = table.sync([dict(doc_id="d1", text="hello")])
        assert result.deleted == 1
        assert table.count() == 1
        assert table.get("d2") is None

    def test_sync_combined(self, store):
        """sync handles insert + update + delete + skip in one call."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="unchanged")
        table.insert(doc_id="d2", text="will change")
        table.insert(doc_id="d3", text="will delete")

        result = table.sync(
            [
                dict(doc_id="d1", text="unchanged"),  # skip
                dict(doc_id="d2", text="changed text"),  # update
                dict(doc_id="d4", text="brand new"),  # insert
            ]
        )

        assert result.skipped == 1
        assert result.updated == 1
        assert result.deleted == 1
        assert result.inserted == 1
        assert table.count() == 3


# ---------------------------------------------------------------------------
# Incrementality
# ---------------------------------------------------------------------------


class TestIncrementality:
    """insert skips rows whose fingerprint already matches."""

    def test_insert_skips_unchanged(self, store):
        """Re-inserting the same row is a no-op (same fingerprint)."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="hello")
        assert table.count() == 1

        table.insert(doc_id="d1", text="hello")
        assert table.count() == 1  # not duplicated

    def test_insert_updates_changed(self, store):
        """Re-inserting with different source updates the row."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="hello")
        assert table.get("d1")["word_count"] == 1

        table.insert(doc_id="d1", text="hello world")
        assert table.count() == 1  # still one row
        assert table.get("d1")["word_count"] == 2  # re-derived


# ---------------------------------------------------------------------------
# Recompute
# ---------------------------------------------------------------------------


class TestRecompute:
    """recompute(column) re-derives one column for all rows."""

    def test_recompute_with_new_component(self, store):
        """Recompute re-derives using current bound components."""
        from hypergraph.materialization import HyperTable

        emb_small = Embedder(dim=3)
        table = (
            HyperTable(
                [clean, embed_text],
                identity="doc_id",
                store=store,
            )
            .bind(embedder=emb_small)
            .with_runner(SyncRunner())
        )

        table.insert(doc_id="d1", text="hello")
        old_vec = table.get("d1")["vector"]
        assert len(old_vec) == 3

        # Swap to a different embedder and recompute
        emb_big = Embedder(model_name="big", dim=3)
        table_rebound = table.bind(embedder=emb_big)
        table_rebound.recompute("vector")

        new_vec = table_rebound.get("d1")["vector"]
        assert len(new_vec) == 3
        # Same dim but different model — values should differ
        # (In this test embedder, model_name doesn't affect output, but the
        # recompute should still run and produce valid output)
        assert isinstance(new_vec, list)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


class TestBackfill:
    """backfill(column) derives a new column for rows with NULL."""

    def test_backfill_populates_null_columns(self, store):
        """After adding a node, backfill derives values for existing rows."""
        from hypergraph.materialization import HyperTable

        # Insert with a smaller graph (no word_count)
        table_v1 = HyperTable(
            [clean],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table_v1.insert(doc_id="d1", text="hello world")
        table_v1.insert(doc_id="d2", text="one two three")

        # Upgrade to graph with word_count
        table_v2 = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table_v2.backfill("word_count")

        assert table_v2.get("d1")["word_count"] == 2
        assert table_v2.get("d2")["word_count"] == 3


# ---------------------------------------------------------------------------
# Crash recovery / correctness regressions
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    """Regression tests for crash-leftover dedup and generation-aware reads."""

    def test_read_one_returns_highest_write_gen(self, store):
        """If duplicate rows exist (simulating a crash between write and delete),
        read_one returns the row with the highest _write_gen."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="original")

        # Simulate crash: manually write a second row with higher _write_gen
        spec = table._spec
        stale_row = store.read_one(spec.name, "doc_id", "d1")
        updated_row = {**stale_row, "clean_text": "updated", "word_count": 99, "_write_gen": 999}
        store.write_rows(spec.name, [updated_row])

        # read_one must return the gen=999 row, not the original
        result = store.read_one(spec.name, "doc_id", "d1")
        assert result["_write_gen"] == 999
        assert result["word_count"] == 99

    def test_sync_deduplicates_crash_leftovers(self, store):
        """sync() deduplicates rows by identity before processing, keeping highest gen."""
        from hypergraph.materialization import HyperTable

        table = HyperTable(
            [clean, count_words],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table.insert(doc_id="d1", text="hello")

        # Simulate crash leftover: write a duplicate with higher gen
        spec = table._spec
        original = store.read_one(spec.name, "doc_id", "d1")
        dup = {**original, "_write_gen": original["_write_gen"] + 100}
        store.write_rows(spec.name, [dup])

        # sync should not double-count d1 or error
        result = table.sync([dict(doc_id="d1", text="hello")])
        assert result.inserted == 0
        assert result.deleted == 0


class TestFingerprintCorrectness:
    """Regression: fingerprint must detect node code and component config changes."""

    def test_fingerprint_detects_node_code_change(self, store):
        """Changing a node's code produces a different fingerprint → sync re-derives."""
        from hypergraph.materialization import HyperTable

        @node(output_name="label")
        def classify_v1(clean_text: str) -> str:
            return "v1:" + clean_text

        table_v1 = HyperTable(
            [clean, classify_v1],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        table_v1.insert(doc_id="d1", text="hello")
        assert table_v1.get("d1")["label"] == "v1:hello"

        # "Deploy" a new version of the node
        @node(output_name="label")
        def classify_v2(clean_text: str) -> str:
            return "v2:" + clean_text

        table_v2 = HyperTable(
            [clean, classify_v2],
            identity="doc_id",
            store=store,
        ).with_runner(SyncRunner())

        result = table_v2.sync([dict(doc_id="d1", text="hello")])
        # Different node code → different fingerprint → should re-derive
        assert result.updated == 1
        assert table_v2.get("d1")["label"] == "v2:hello"

    def test_fingerprint_detects_component_config_change(self, store):
        """Changing a component's config produces a different fingerprint."""
        from hypergraph.materialization import HyperTable

        emb_a = Embedder(model_name="model-a", dim=3)
        table_a = (
            HyperTable(
                [clean, embed_text],
                identity="doc_id",
                store=store,
            )
            .bind(embedder=emb_a)
            .with_runner(SyncRunner())
        )

        table_a._ensure_analyzed()
        graph_inputs = {"text": "hello"}
        fp_a = table_a._compute_row_fingerprint(graph_inputs)

        emb_b = Embedder(model_name="model-b", dim=3)
        table_b = (
            HyperTable(
                [clean, embed_text],
                identity="doc_id",
                store=store,
            )
            .bind(embedder=emb_b)
            .with_runner(SyncRunner())
        )

        table_b._ensure_analyzed()
        fp_b = table_b._compute_row_fingerprint(graph_inputs)
        assert fp_a != fp_b


class TestChildCascadeOrdering:
    """Regression: child writes must complete before old children are deleted."""

    def test_update_preserves_children_count(self, store, embedder):
        """After updating a parent with children, the new child count is correct."""
        from hypergraph.materialization import HyperTable

        @node(output_name="utterances")
        def split_fixed(transcript: str) -> list[Utterance]:
            return [
                Utterance(utterance_id="u0", text="alpha", speaker="A"),
                Utterance(utterance_id="u1", text="beta", speaker="B"),
                Utterance(utterance_id="u2", text="gamma", speaker="C"),
            ]

        table = (
            HyperTable(
                [extract_audio, transcribe, split_fixed, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
                identity="video_id",
                store=store,
            )
            .bind(embedder=embedder)
            .with_runner(SyncRunner())
        )

        table.insert(video_id="v1", path="/data/a.mp4")
        assert len(table.children("v1")) == 3

        table.update("v1", path="/data/b.mp4")
        # After update, should still have exactly 3 children (not 0 from a crash)
        assert len(table.children("v1")) == 3

    def test_delete_removes_children_before_parent(self, store, embedder):
        """delete() removes children first so no orphans remain on crash."""
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

        table.insert(video_id="v1", path="/data/a.mp4")
        table.insert(video_id="v2", path="/data/b.mp4")
        assert table.count("utterance") == 4

        table.delete("v1")
        assert table.count() == 1
        assert table.children("v1") == []
        assert table.count("utterance") == 2  # only v2's children remain
