"""Tests for named indexes as persisted query specs (create_index / search).

An index is a projection, not a table: a named, persisted query spec over a
vector column that already lives in the table. For the LanceDB store there is
no separate materialized artifact — LanceDB ANN-searches the column directly.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import HyperTable, TableStore
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


class TestLanceDBStoreSearchAbsentVsError:
    """LanceDBStore.search: an absent table is an empty result; a corrupt or
    unreadable one re-raises.

    A table LanceDB never created (no on-disk ``<name>.lance`` directory) means
    "no rows have ever been written" — a documented empty result. But a table
    whose directory exists yet fails to open is corrupt / permission-denied — a
    real failure that must surface, not be swallowed into ``[]``. The error
    message can't tell them apart (LanceDB raises the same "was not found"
    ValueError for a corrupt table), so the store keys off the directory.
    """

    def test_absent_table_returns_empty(self, tmp_path):
        store = LanceDBStore(str(tmp_path / "absent_store"))
        # Never created -> no <name>.lance directory -> genuinely absent.
        assert store.search("never_written", query_vector=[1.0, 0.0, 0.0]) == []

    def test_corrupt_table_reraises(self, tmp_path, monkeypatch):
        """A table whose directory exists but won't open is corrupt -> re-raise."""
        store = LanceDBStore(str(tmp_path / "corrupt_store"))
        # Simulate the on-disk directory being present (table was created)...
        (store._path / "some_table.lance").mkdir(parents=True)

        # ...but open_table fails as LanceDB does for a corrupt table: the same
        # "was not found" ValueError it also raises for a truly absent table.
        def boom(name):
            raise ValueError(f"Table '{name}' was not found")

        monkeypatch.setattr(store._db, "open_table", boom)
        with pytest.raises(ValueError, match="was not found"):
            store.search("some_table", query_vector=[1.0, 0.0, 0.0])

    def test_permission_error_reraises(self, tmp_path, monkeypatch):
        """A non-ValueError open failure (permissions, etc.) must re-raise too."""
        store = LanceDBStore(str(tmp_path / "perm_store"))
        (store._path / "some_table.lance").mkdir(parents=True)

        def boom(name):
            raise RuntimeError("permission denied")

        monkeypatch.setattr(store._db, "open_table", boom)
        with pytest.raises(RuntimeError, match="permission denied"):
            store.search("some_table", query_vector=[1.0, 0.0, 0.0])


class ManifestlessStore(TableStore):
    """A store that supports rows but leaves the manifest hooks as base no-ops.

    Legitimate for a backend that never uses indexes. But if such a store is
    asked to ``create_index``, it must fail loud at use time instead of
    silently "succeeding" and having ``list_indexes`` return nothing.
    """

    def __init__(self) -> None:
        self._tables: dict[str, list[dict]] = {}

    def open(self, spec, children):
        result = {spec.name: [c.name for c in spec.columns]}
        self._tables.setdefault(spec.name, [])
        for child in children:
            result[child.name] = [c.name for c in child.columns]
            self._tables.setdefault(child.name, [])
        return result

    def count(self, table_name):
        return len(self._tables.get(table_name, []))

    def read_rows(self, table_name, where=None, *, limit=None):
        rows = list(self._tables.get(table_name, []))
        return rows[:limit] if limit is not None else rows

    def read_one(self, table_name, identity_column, identity_value):
        matches = [r for r in self._tables.get(table_name, []) if r.get(identity_column) == identity_value]
        return max(matches, key=lambda r: r.get("_write_gen", 0)) if matches else None

    def write_rows(self, table_name, rows):
        self._tables.setdefault(table_name, []).extend(rows)

    def delete_rows(self, table_name, where):
        return 0

    def max_write_gen(self, table_name):
        rows = self._tables.get(table_name, [])
        return max((r.get("_write_gen", 0) for r in rows), default=0)

    def evolve_schema(self, table_name, new_columns):
        return list(new_columns.keys())

    # NOTE: save_manifest / load_manifest are intentionally NOT overridden.


class TestManifestlessStoreFailsLoudOnCreateIndex:
    def _table(self):
        store = ManifestlessStore()
        table = make_table(store)
        table.insert(DOCS)
        return store, table

    def test_create_index_raises_naming_store_and_capability(self):
        _store, table = self._table()

        with pytest.raises(NotImplementedError, match="ManifestlessStore") as exc:
            table.create_index("main", vector="vec")
        assert "save_manifest" in str(exc.value)

    def test_no_index_is_silently_recorded(self):
        """The failure must fire before the index is half-written."""
        store, table = self._table()

        with pytest.raises(NotImplementedError):
            table.create_index("main", vector="vec")
        # A fresh instance over the same store sees no phantom index.
        fresh = make_table(store)
        assert fresh.list_indexes() == []
