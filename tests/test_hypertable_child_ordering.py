"""Crash-safety regressions for HyperTable child writes."""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import HyperTable, TableStore
from hypergraph.runners import SyncRunner


class MemoryStore(TableStore):
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.fail_child_writes = False

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
        if self.fail_child_writes and table_name == "utterance":
            raise RuntimeError("child write failed")
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


class Utterance(TypedDict):
    utterance_id: str
    text: str


@node(output_name="utterances")
def split_words(text: str) -> list[Utterance]:
    return [Utterance(utterance_id=f"u{i}", text=word) for i, word in enumerate(text.split())]


@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.upper()


process_utterance = Graph([clean], name="process_utterance")


def test_upsert_writes_new_children_before_deleting_old_children() -> None:
    """A failed child rewrite must not erase the previously readable children."""

    store = MemoryStore()
    table = HyperTable(
        [split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
        identity="doc_id",
        store=store,
    ).with_runner(SyncRunner())

    table.insert(doc_id="d1", text="old words")
    assert [child["text"] for child in table.children("d1")] == ["old", "words"]

    store.fail_child_writes = True
    with pytest.raises(RuntimeError, match="child write failed"):
        table.insert(doc_id="d1", text="new child rows")

    assert [child["text"] for child in table.children("d1")] == ["old", "words"]


def test_children_and_count_deduplicate_crash_leftovers() -> None:
    """Public child reads expose one logical child per identity."""

    store = MemoryStore()
    table = HyperTable(
        [split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
        identity="doc_id",
        store=store,
    ).with_runner(SyncRunner())

    table.insert(doc_id="d1", text="old words")
    stale = store.read_one("utterance", "utterance_id", "u0")
    store.write_rows("utterance", [{**stale, "text": "new", "clean_text": "NEW", "_write_gen": stale["_write_gen"] + 100}])

    children = table.children("d1")

    assert len(children) == 2
    assert {child["utterance_id"] for child in children} == {"u0", "u1"}
    assert next(child for child in children if child["utterance_id"] == "u0")["text"] == "new"
    assert table.count("utterance") == 2
