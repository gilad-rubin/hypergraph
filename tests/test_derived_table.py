"""Integration tests for DerivedTable against real LanceDB."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pytest

from hypergraph.materialization import (
    ChainedTableError,
    ContentKey,
    DerivationError,
    Identity,
    SyncResult,
)
from hypergraph.materialization._store import clear_store_cache
from hypergraph.materialization._table import DerivedTable

# ---------------------------------------------------------------------------
# Test entities and derive functions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Utterance:
    utt_id: Annotated[str, Identity]
    text: Annotated[str, ContentKey]
    speaker: str


@dataclass(frozen=True)
class EmbeddedUtterance:
    utt_id: str
    text: str
    vector: list[float]


@dataclass(frozen=True)
class TopicUtterance:
    utt_id: str
    text: str
    topics: list[str]


class Embedder:
    def __init__(self, model_name: str = "test-model"):
        self.model_name = model_name
        self.call_count = 0

    def _config(self):
        return {"model": self.model_name}

    def embed(self, text: str) -> list[float]:
        self.call_count += 1
        return [float(ord(c)) for c in text[:3]]


class TopicExtractor:
    def __init__(self, model_name: str = "test-topics"):
        self.model_name = model_name

    def _config(self):
        return {"model": self.model_name}

    def extract(self, text: str) -> list[str]:
        return [w for w in text.split()[:2]]


class BadComponent:
    def __init__(self, x):
        self.x = x


def embed(utt: Utterance, embedder: Embedder) -> EmbeddedUtterance:
    return EmbeddedUtterance(
        utt_id=utt.utt_id,
        text=utt.text,
        vector=embedder.embed(utt.text),
    )


def extract_topics(utt: Utterance, extractor: TopicExtractor) -> TopicUtterance:
    return TopicUtterance(
        utt_id=utt.utt_id,
        text=utt.text,
        topics=extractor.extract(utt.text),
    )


def failing_embed(utt: Utterance, embedder: Embedder) -> EmbeddedUtterance:
    if "fail" in utt.text.lower():
        raise ValueError(f"Cannot embed: {utt.text}")
    return EmbeddedUtterance(
        utt_id=utt.utt_id,
        text=utt.text,
        vector=embedder.embed(utt.text),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_stores():
    """Clear store cache between tests."""
    clear_store_cache()
    yield
    clear_store_cache()


@pytest.fixture
def store_path(tmp_path):
    """Fresh LanceDB store path for each test."""
    return str(tmp_path / "test_store")


@pytest.fixture
def embedder():
    return Embedder()


@pytest.fixture
def extractor():
    return TopicExtractor()


@pytest.fixture
def table(store_path, embedder):
    """A root DerivedTable for basic tests."""
    return DerivedTable(
        source=Utterance,
        output=EmbeddedUtterance,
        derive=embed,
        components={"embedder": embedder},
        store=store_path,
    )


# ---------------------------------------------------------------------------
# TEST: Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_component_without_config_raises(self, store_path):
        with pytest.raises(TypeError, match="_config"):
            DerivedTable(
                source=Utterance,
                output=EmbeddedUtterance,
                derive=embed,
                components={"embedder": BadComponent("x")},
                store=store_path,
            )

    def test_valid_construction(self, table):
        assert table.is_root

    def test_no_components_ok(self, store_path):
        def simple_derive(utt: Utterance) -> EmbeddedUtterance:
            return EmbeddedUtterance(utt_id=utt.utt_id, text=utt.text, vector=[0.0])

        t = DerivedTable(
            source=Utterance,
            output=EmbeddedUtterance,
            derive=simple_derive,
            components={},
            store=store_path,
        )
        assert t.is_root


# ---------------------------------------------------------------------------
# TEST: Insert
# ---------------------------------------------------------------------------


class TestInsert:
    def test_insert_new_items(self, table):
        table.insert(
            [
                Utterance("u1", "hello world", "alice"),
                Utterance("u2", "goodbye world", "bob"),
            ]
        )
        assert table.count() == 2

    def test_insert_upsert_changed_content(self, table):
        table.insert([Utterance("u1", "hello", "alice")])
        assert table.count() == 1

        table.insert([Utterance("u1", "changed text", "alice")])
        assert table.count() == 1
        row = table.get(utt_id="u1")
        assert row is not None
        assert row.text == "changed text"

    def test_insert_unchanged_skips(self, table, embedder):
        table.insert([Utterance("u1", "hello", "alice")])
        initial_calls = embedder.call_count

        table.insert([Utterance("u1", "hello", "alice")])
        assert embedder.call_count == initial_calls
        assert table.count() == 1

    def test_insert_mixed_new_and_unchanged(self, table, embedder):
        table.insert(
            [
                Utterance("u1", "hello", "alice"),
                Utterance("u2", "world", "bob"),
            ]
        )
        initial_calls = embedder.call_count

        table.insert(
            [
                Utterance("u1", "hello", "alice"),  # unchanged
                Utterance("u3", "new item", "carol"),  # new
            ]
        )
        assert table.count() == 3
        assert embedder.call_count == initial_calls + 1


# ---------------------------------------------------------------------------
# TEST: Partial update
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_root_table(self, table):
        table.insert([Utterance("u1", "hello", "alice")])
        table.update(utt_id="u1", text="updated text")

        row = table.get(utt_id="u1")
        assert row is not None
        assert row.text == "updated text"

    def test_update_preserves_non_overridden_fields(self, table):
        table.insert([Utterance("u1", "hello", "alice")])
        table.update(utt_id="u1", text="updated")

        row = table.get(utt_id="u1")
        assert row is not None
        assert row.text == "updated"


# ---------------------------------------------------------------------------
# TEST: Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_single(self, table):
        table.insert(
            [
                Utterance("u1", "hello", "alice"),
                Utterance("u2", "world", "bob"),
            ]
        )
        table.delete(utt_id="u1")
        assert table.count() == 1
        assert table.get(utt_id="u1") is None

    def test_delete_multiple(self, table):
        table.insert(
            [
                Utterance("u1", "a", "x"),
                Utterance("u2", "b", "x"),
                Utterance("u3", "c", "x"),
            ]
        )
        table.delete(utt_id=["u1", "u2"])
        assert table.count() == 1


# ---------------------------------------------------------------------------
# TEST: Sync
# ---------------------------------------------------------------------------


class TestSync:
    def test_sync_reconciles(self, table):
        table.insert(
            [
                Utterance("u1", "hello", "alice"),
                Utterance("u2", "world", "bob"),
            ]
        )

        result = table.sync(
            [
                Utterance("u1", "hello", "alice"),  # unchanged
                Utterance("u2", "changed", "bob"),  # changed
                Utterance("u3", "new", "carol"),  # new
            ]
        )

        assert isinstance(result, SyncResult)
        assert result.skipped == 1  # u1
        assert result.updated == 1  # u2
        assert result.inserted == 1  # u3
        assert result.deleted == 0  # nothing removed
        assert table.count() == 3

    def test_sync_deletes_missing(self, table):
        table.insert(
            [
                Utterance("u1", "a", "x"),
                Utterance("u2", "b", "x"),
                Utterance("u3", "c", "x"),
            ]
        )

        result = table.sync(
            [
                Utterance("u1", "a", "x"),
            ]
        )
        assert result.deleted == 2
        assert table.count() == 1

    def test_sync_idempotent(self, table):
        items = [
            Utterance("u1", "a", "x"),
            Utterance("u2", "b", "y"),
        ]
        table.insert(items)
        result = table.sync(items)

        assert result.inserted == 0
        assert result.updated == 0
        assert result.deleted == 0
        assert result.skipped == 2


# ---------------------------------------------------------------------------
# TEST: Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_on_error_ignore_stores_error_rows(self, store_path, embedder):
        t = DerivedTable(
            source=Utterance,
            output=EmbeddedUtterance,
            derive=failing_embed,
            components={"embedder": embedder},
            store=store_path,
        )

        t.insert(
            [
                Utterance("u1", "normal text", "alice"),
                Utterance("u2", "will fail here", "bob"),
                Utterance("u3", "also normal", "carol"),
            ],
            on_error="ignore",
        )

        assert t.count() == 2
        assert t.count(include_errors=True) == 3

        errored = t.errors()
        assert len(errored) == 1
        assert errored[0].identity == {"utt_id": "u2"}
        assert errored[0].error_type == "ValueError"

    def test_on_error_raise_commits_successes(self, store_path, embedder):
        t = DerivedTable(
            source=Utterance,
            output=EmbeddedUtterance,
            derive=failing_embed,
            components={"embedder": embedder},
            store=store_path,
        )

        with pytest.raises(DerivationError) as exc_info:
            t.insert(
                [
                    Utterance("u1", "normal", "alice"),
                    Utterance("u2", "will fail", "bob"),
                    Utterance("u3", "also good", "carol"),
                ]
            )

        e = exc_info.value
        assert {"utt_id": "u2"} in e.failed
        assert {"utt_id": "u1"} in e.succeeded
        assert {"utt_id": "u3"} in e.succeeded

        assert t.get(utt_id="u1") is not None
        assert t.get(utt_id="u2") is None  # not written
        assert t.get(utt_id="u3") is not None

    def test_get_returns_none_for_error_row(self, store_path, embedder):
        t = DerivedTable(
            source=Utterance,
            output=EmbeddedUtterance,
            derive=failing_embed,
            components={"embedder": embedder},
            store=store_path,
        )
        t.insert(
            [
                Utterance("u1", "will fail", "x"),
            ],
            on_error="ignore",
        )

        assert t.get(utt_id="u1") is None
        assert t.count() == 0
        assert t.count(include_errors=True) == 1


# ---------------------------------------------------------------------------
# TEST: Queries
# ---------------------------------------------------------------------------


class TestQueries:
    def test_get_by_identity(self, table):
        table.insert([Utterance("u1", "hello", "alice")])
        row = table.get(utt_id="u1")
        assert row is not None
        assert isinstance(row, EmbeddedUtterance)
        assert row.utt_id == "u1"

    def test_get_missing_returns_none(self, table):
        assert table.get(utt_id="nonexistent") is None

    def test_filter(self, table):
        table.insert(
            [
                Utterance("u1", "hello", "alice"),
                Utterance("u2", "world", "bob"),
                Utterance("u3", "hello again", "alice"),
            ]
        )
        results = table.filter(text="hello")
        assert len(results) == 1
        assert results[0].utt_id == "u1"

    def test_count(self, table):
        assert table.count() == 0
        table.insert([Utterance("u1", "a", "x"), Utterance("u2", "b", "y")])
        assert table.count() == 2

    def test_errors_empty_when_no_errors(self, table):
        table.insert([Utterance("u1", "hello", "x")])
        assert table.errors() == []


# ---------------------------------------------------------------------------
# TEST: Multiple derivations from same source
# ---------------------------------------------------------------------------


class TestMultipleDerivations:
    def test_independent_tables(self, store_path, embedder, extractor):
        embeddings = DerivedTable(
            source=Utterance,
            output=EmbeddedUtterance,
            derive=embed,
            components={"embedder": embedder},
            store=store_path,
        )
        topics = DerivedTable(
            source=Utterance,
            output=TopicUtterance,
            derive=extract_topics,
            components={"extractor": extractor},
            store=store_path,
        )

        embeddings.insert([Utterance("u1", "hello world", "alice")])
        assert embeddings.count() == 1
        assert topics.count() == 0

        topics.insert([Utterance("u1", "hello world", "alice")])
        assert topics.count() == 1


# ---------------------------------------------------------------------------
# TEST: Versioning
# ---------------------------------------------------------------------------


class TestVersioning:
    def test_version_increments(self, table):
        v0 = table.version
        table.insert([Utterance("u1", "hello", "alice")])
        v1 = table.version
        assert v1 == v0 + 1

    def test_at_returns_snapshot(self, table):
        table.insert([Utterance("u1", "hello", "alice")])
        v1 = table.version

        table.insert([Utterance("u2", "world", "bob")])
        assert table.count() == 2

        old = table.at(v1)
        assert old.count() == 1

    def test_revert(self, table):
        table.insert([Utterance("u1", "hello", "alice")])

        table.insert([Utterance("u2", "world", "bob")])
        assert table.count() == 2

        table.revert()
        assert table.count() == 1


# ---------------------------------------------------------------------------
# TEST: Recompute
# ---------------------------------------------------------------------------


class TestRecompute:
    def test_recompute_with_new_component(self, table, embedder):
        table.insert([Utterance("u1", "hello", "alice")])

        new_embedder = Embedder("large-model")
        table.recompute(components={"embedder": new_embedder})

        recomputed = table.get(utt_id="u1")
        assert recomputed.vector is not None

    def test_recompute_errors_only(self, store_path, embedder):
        call_tracker = {"count": 0}

        def sometimes_fail(utt: Utterance, embedder: Embedder) -> EmbeddedUtterance:
            call_tracker["count"] += 1
            if utt.text == "fail_first_time" and call_tracker["count"] <= 2:
                raise ValueError("temporary failure")
            return EmbeddedUtterance(utt_id=utt.utt_id, text=utt.text, vector=embedder.embed(utt.text))

        t = DerivedTable(
            source=Utterance,
            output=EmbeddedUtterance,
            derive=sometimes_fail,
            components={"embedder": embedder},
            store=store_path,
        )

        t.insert(
            [
                Utterance("u1", "good text", "x"),
                Utterance("u2", "fail_first_time", "x"),
            ],
            on_error="ignore",
        )

        assert t.count() == 1
        assert len(t.errors()) == 1

        t.recompute(errors_only=True)
        assert t.count() == 2
        assert len(t.errors()) == 0


# ---------------------------------------------------------------------------
# TEST: Chained table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnrichedUtterance:
    utt_id: str
    text: str
    enriched: str


def enrich(emb: EmbeddedUtterance) -> EnrichedUtterance:
    return EnrichedUtterance(
        utt_id=emb.utt_id,
        text=emb.text,
        enriched=f"enriched: {emb.text}",
    )


class TestChainedTable:
    def test_chained_rejects_insert(self, table):
        chained = DerivedTable(
            source=table,
            output=EnrichedUtterance,
            derive=enrich,
            components={},
            store=table._store_path,
        )
        with pytest.raises(ChainedTableError):
            chained.insert([])

    def test_chained_rejects_delete(self, table):
        chained = DerivedTable(
            source=table,
            output=EnrichedUtterance,
            derive=enrich,
            components={},
            store=table._store_path,
        )
        with pytest.raises(ChainedTableError):
            chained.delete(utt_id="u1")

    def test_chained_rejects_sync(self, table):
        chained = DerivedTable(
            source=table,
            output=EnrichedUtterance,
            derive=enrich,
            components={},
            store=table._store_path,
        )
        with pytest.raises(ChainedTableError):
            chained.sync([])

    def test_cascade_on_insert(self, table):
        chained = DerivedTable(
            source=table,
            output=EnrichedUtterance,
            derive=enrich,
            components={},
            store=table._store_path,
        )
        table.insert([Utterance("u1", "hello", "alice")])

        assert table.count() == 1
        assert chained.count() == 1
        row = chained.get(utt_id="u1")
        assert row is not None
        assert row.enriched == "enriched: hello"

    def test_cascade_delete(self, table):
        chained = DerivedTable(
            source=table,
            output=EnrichedUtterance,
            derive=enrich,
            components={},
            store=table._store_path,
        )
        table.insert(
            [
                Utterance("u1", "hello", "alice"),
                Utterance("u2", "world", "bob"),
            ]
        )
        assert chained.count() == 2

        table.delete(utt_id="u1")
        assert table.count() == 1
        assert chained.count() == 1
        assert chained.get(utt_id="u1") is None

    def test_cascade_upsert(self, table):
        chained = DerivedTable(
            source=table,
            output=EnrichedUtterance,
            derive=enrich,
            components={},
            store=table._store_path,
        )
        table.insert([Utterance("u1", "hello", "alice")])
        assert chained.get(utt_id="u1").enriched == "enriched: hello"

        table.insert([Utterance("u1", "changed", "alice")])
        assert chained.get(utt_id="u1").enriched == "enriched: changed"
        assert chained.count() == 1

    def test_chained_recompute(self, table):
        chained = DerivedTable(
            source=table,
            output=EnrichedUtterance,
            derive=enrich,
            components={},
            store=table._store_path,
        )
        table.insert([Utterance("u1", "hello", "alice")])
        assert chained.count() == 1

        chained.recompute()
        assert chained.count() == 1
        assert chained.get(utt_id="u1").enriched == "enriched: hello"

    def test_chained_recompute_orphan_cleanup(self, table):
        chained = DerivedTable(
            source=table,
            output=EnrichedUtterance,
            derive=enrich,
            components={},
            store=table._store_path,
        )
        table.insert([Utterance("u1", "hello", "alice")])
        assert chained.count() == 1

        table.delete(utt_id="u1")
        assert table.count() == 0

        chained.recompute()
        assert chained.count() == 0


# ---------------------------------------------------------------------------
# TEST: 1:N explosion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Document:
    doc_id: Annotated[str, Identity]
    text: Annotated[str, ContentKey]


@dataclass(frozen=True)
class Chunk:
    chunk_id: Annotated[str, Identity]
    doc_id: str
    text: str
    index: int


def split_document(doc: Document) -> list[Chunk]:
    words = doc.text.split()
    return [
        Chunk(
            chunk_id=f"{doc.doc_id}_c{i}",
            doc_id=doc.doc_id,
            text=w,
            index=i,
        )
        for i, w in enumerate(words)
    ]


class TestExplosion:
    def test_one_to_many(self, store_path):
        chunks = DerivedTable(
            source=Document,
            output=Chunk,
            derive=split_document,
            components={},
            store=store_path,
        )
        chunks.insert([Document("d1", "hello world foo")])
        assert chunks.count() == 3

    def test_delete_cascades_explosion(self, store_path):
        chunks = DerivedTable(
            source=Document,
            output=Chunk,
            derive=split_document,
            components={},
            store=store_path,
        )
        chunks.insert(
            [
                Document("d1", "hello world"),
                Document("d2", "foo bar baz"),
            ]
        )
        assert chunks.count() == 5

        chunks.delete(doc_id="d1")
        assert chunks.count() == 3
        remaining = chunks.filter(doc_id="d1")
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# TEST: drop()
# ---------------------------------------------------------------------------


class TestDrop:
    def test_drop_removes_table(self, table):
        table.insert([Utterance("u1", "hello", "alice")])
        assert table.count() == 1
        table.drop()
        assert table.count() == 0


# ---------------------------------------------------------------------------
# TEST: Circular dependency detection
# ---------------------------------------------------------------------------


class TestCircularDependency:
    def test_circular_rejected(self, store_path, embedder):
        t1 = DerivedTable(
            source=Utterance,
            output=EmbeddedUtterance,
            derive=embed,
            components={"embedder": embedder},
            store=store_path,
        )
        t2 = DerivedTable(
            source=t1,
            output=EnrichedUtterance,
            derive=enrich,
            components={},
            store=store_path,
        )
        with pytest.raises(ValueError, match="Circular dependency"):
            DerivedTable(
                source=t2,
                output=EmbeddedUtterance,
                derive=embed,
                components={"embedder": embedder},
                store=store_path,
            )


# ---------------------------------------------------------------------------
# TEST: Public import path
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_import_from_package(self):
        from hypergraph.materialization import DerivedTable as DT

        assert DT is DerivedTable
