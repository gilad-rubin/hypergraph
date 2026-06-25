"""HyperTable read-path behavior used by application stores."""

from __future__ import annotations

import pytest

from hypergraph import node
from hypergraph.materialization import HyperTable
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import SyncRunner


@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()


@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())


@pytest.fixture
def table(tmp_path):
    return HyperTable(
        [clean, count_words],
        identity="doc_id",
        store=LanceDBStore(str(tmp_path / "readpath_store")),
    ).with_runner(SyncRunner())


def test_filter_returns_matching_public_rows(table) -> None:
    """filter(where=...) returns public rows matching store predicates."""

    table.insert(doc_id="d1", text="hello world", station="NICU", active=True)
    table.insert(doc_id="d2", text="cardiology", station="PICU", active=True)
    table.insert(doc_id="d3", text="neonatal", station="NICU", active=False)

    rows = table.filter(where=[("station", "eq", "NICU"), ("active", "eq", True)])

    assert [row["doc_id"] for row in rows] == ["d1"]
    assert rows[0]["clean_text"] == "hello world"
    assert all("_write_gen" not in row for row in rows)


def test_set_updates_matching_metadata_rows(table) -> None:
    """set(where=..., fields...) updates metadata without re-deriving rows."""

    table.insert(doc_id="d1", text="hello world", active=False, station="")
    table.insert(doc_id="d2", text="second row", active=False, station="")

    updated = table.set([("active", "eq", False)], active=True, station="NICU")

    assert updated == 2
    rows = table.filter(where=[("active", "eq", True), ("station", "eq", "NICU")])
    assert {row["doc_id"] for row in rows} == {"d1", "d2"}
    assert {row["word_count"] for row in rows} == {2}


def test_set_rejects_content_key_fields(table) -> None:
    """set() is metadata-only; source changes must use update()."""

    table.insert(doc_id="d1", text="hello world", active=False)

    with pytest.raises(ValueError, match="content-key"):
        table.set([("doc_id", "eq", "d1")], text="new source")
