"""Tests for child row fingerprints — re-insert skips unchanged children."""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import HyperTable, TableStore
from hypergraph.runners import SyncRunner

# ---------------------------------------------------------------------------
# MemoryStore — minimal in-memory store for isolated tests
# ---------------------------------------------------------------------------


class MemoryStore(TableStore):
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}

    def open(self, spec, children):
        self.rows.setdefault(spec.name, [])
        for child in children:
            self.rows.setdefault(child.name, [])
        return {name: list(rows[0].keys()) if rows else [] for name, rows in self.rows.items()}

    def count(self, table_name):
        return len(self.rows.get(table_name, []))

    def read_rows(self, table_name, where=None, *, limit=None):
        rows = [row.copy() for row in self.rows.get(table_name, []) if _matches(row, where or [])]
        return rows[:limit] if limit is not None else rows

    def read_one(self, table_name, identity_column, identity_value):
        rows = self.read_rows(table_name, [(identity_column, "eq", identity_value)])
        if not rows:
            return None
        return max(rows, key=lambda row: row.get("_write_gen", 0))

    def write_rows(self, table_name, rows):
        self.rows.setdefault(table_name, []).extend(row.copy() for row in rows)

    def delete_rows(self, table_name, where):
        existing = self.rows.get(table_name, [])
        keep = [row for row in existing if not _matches(row, where)]
        self.rows[table_name] = keep
        return len(existing) - len(keep)

    def max_write_gen(self, table_name):
        return max((row.get("_write_gen", 0) for row in self.rows.get(table_name, [])), default=0)

    def evolve_schema(self, table_name, new_columns):
        return []


def _matches(row: dict[str, Any], where) -> bool:
    for col, op, value in where:
        current = row.get(col)
        if op == "eq" and current != value:
            return False
        if op == "lt" and not (current is not None and current < value):
            return False
    return True


# ---------------------------------------------------------------------------
# Test graph: parent produces children, child graph derives a column
# ---------------------------------------------------------------------------


class Utterance(TypedDict):
    utterance_id: str
    text: str


call_count: dict[str, int] = {}


@node(output_name="utterances")
def split_words(text: str) -> list[Utterance]:
    return [Utterance(utterance_id=f"u{i}", text=word) for i, word in enumerate(text.split())]


@node(output_name="clean_text")
def clean(text: str) -> str:
    call_count["clean"] = call_count.get("clean", 0) + 1
    return text.upper()


process_utterance = Graph([clean], name="process_utterance")


@pytest.fixture(autouse=True)
def _reset_call_count():
    call_count.clear()


def _make_table(store: MemoryStore) -> HyperTable:
    return Graph([split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")]).as_table(
        identity="doc_id", store=store, runner=SyncRunner()
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reinsert_skips_unchanged_children():
    """Re-inserting the same parent skips children whose fingerprints match."""
    store = MemoryStore()
    table = _make_table(store)

    table.insert(doc_id="d1", text="hello world")
    first_count = call_count.get("clean", 0)
    assert first_count == 2  # two words → two child graph runs

    # Re-insert same parent with same input
    table.insert(doc_id="d1", text="hello world")
    second_count = call_count.get("clean", 0)
    # Children should be skipped — no additional clean() calls
    assert second_count == first_count

    # Children should still be readable
    children = table.child(table.child_table_names[0]).rows(parent="d1")
    assert len(children) == 2


def test_changed_child_source_re_derives_only_that_child():
    """When one child's source changes, only that child re-derives."""
    store = MemoryStore()
    table = _make_table(store)

    table.insert(doc_id="d1", text="hello world")
    first_count = call_count.get("clean", 0)
    assert first_count == 2

    # "hello earth" → u0 text="hello" (unchanged), u1 text="earth" (changed)
    table.insert(doc_id="d1", text="hello earth")
    second_count = call_count.get("clean", 0)
    # Only u1 re-derives — u0 has same fingerprint and is skipped
    assert second_count == first_count + 1

    children = table.child(table.child_table_names[0]).rows(parent="d1")
    texts = {c["utterance_id"]: c["clean_text"] for c in children}
    assert texts["u0"] == "HELLO"
    assert texts["u1"] == "EARTH"


def test_skipped_children_survive_write_gen_cleanup():
    """Children skipped by fingerprint must not be deleted by _write_gen cleanup."""
    store = MemoryStore()
    table = _make_table(store)

    table.insert(doc_id="d1", text="hello world")
    assert len(table.child(table.child_table_names[0]).rows(parent="d1")) == 2

    # Re-insert same parent — children should be skipped but survive cleanup
    table.insert(doc_id="d1", text="hello world")
    children = table.child(table.child_table_names[0]).rows(parent="d1")
    assert len(children) == 2
    assert {c["utterance_id"] for c in children} == {"u0", "u1"}


def test_parent_skip_still_reconciles_children():
    """When parent fingerprint matches, children are still checked and reconciled."""
    store = MemoryStore()
    table = _make_table(store)

    table.insert(doc_id="d1", text="hello world")
    assert len(table.child(table.child_table_names[0]).rows(parent="d1")) == 2

    # Manually delete one child to simulate a crash
    store.delete_rows("utterance", [("_parent_id", "eq", "d1"), ("utterance_id", "eq", "u1")])
    assert len(table.child(table.child_table_names[0]).rows(parent="d1")) == 1

    # Re-insert same parent — parent matches, but missing child should be re-derived
    table.insert(doc_id="d1", text="hello world")
    children = table.child(table.child_table_names[0]).rows(parent="d1")
    assert len(children) == 2
    assert {c["utterance_id"] for c in children} == {"u0", "u1"}
