"""Tests for on_error policy — error rows and partial success."""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import HyperTable, TableStore
from hypergraph.runners import AsyncRunner, SyncRunner

# ---------------------------------------------------------------------------
# MemoryStore
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
# Test graphs
# ---------------------------------------------------------------------------


class Utterance(TypedDict):
    utterance_id: str
    text: str


fail_on_text: set[str] = set()


@node(output_name="utterances")
def split_words(text: str) -> list[Utterance]:
    return [Utterance(utterance_id=f"u{i}", text=word) for i, word in enumerate(text.split())]


@node(output_name="clean_text")
def clean_maybe_fail(text: str) -> str:
    if text in fail_on_text:
        raise ValueError(f"Failed on {text}")
    return text.upper()


process_utterance = Graph([clean_maybe_fail], name="process_utterance")


@node(output_name="clean_text")
def parent_clean(text: str) -> str:
    if text in fail_on_text:
        raise ValueError(f"Parent failed on {text}")
    return text.upper()


@pytest.fixture(autouse=True)
def _reset_fail_set():
    fail_on_text.clear()


# ---------------------------------------------------------------------------
# on_error="raise" (default) — backward compatibility
# ---------------------------------------------------------------------------


def test_raise_is_default_child_failure_propagates():
    """Default on_error='raise': child failure raises, no rows stored."""
    store = MemoryStore()
    table = HyperTable(
        [split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
        identity="doc_id",
        store=store,
    ).with_runner(SyncRunner())

    fail_on_text.add("world")
    with pytest.raises(ValueError, match="Failed on world"):
        table.insert(doc_id="d1", text="hello world")


def test_raise_parent_failure_propagates():
    """Default on_error='raise': parent graph failure raises."""
    store = MemoryStore()
    table = HyperTable(
        [parent_clean],
        identity="doc_id",
        store=store,
    ).with_runner(SyncRunner())

    fail_on_text.add("bad")
    with pytest.raises(ValueError, match="Parent failed on bad"):
        table.insert(doc_id="d1", text="bad")


# ---------------------------------------------------------------------------
# on_error="store" — child errors
# ---------------------------------------------------------------------------


def test_store_child_error_writes_error_row():
    """on_error='store': failed child gets error row, sibling succeeds."""
    store = MemoryStore()
    table = HyperTable(
        [split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
        identity="doc_id",
        store=store,
        on_error="store",
    ).with_runner(SyncRunner())

    fail_on_text.add("world")
    table.insert(doc_id="d1", text="hello world")

    children = table.children("d1", include_status=True)
    assert len(children) == 2

    by_id = {c["utterance_id"]: c for c in children}
    assert by_id["u0"]["_status"] == "complete"
    assert by_id["u0"]["clean_text"] == "HELLO"

    assert by_id["u1"]["_status"] == "error"
    assert "ValueError" in by_id["u1"]["_error"]
    assert by_id["u1"].get("clean_text") is None


def test_store_error_child_retried_on_reinsert():
    """on_error='store': error child is retried on next insert, complete child skipped."""
    store = MemoryStore()
    table = HyperTable(
        [split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
        identity="doc_id",
        store=store,
        on_error="store",
    ).with_runner(SyncRunner())

    fail_on_text.add("world")
    table.insert(doc_id="d1", text="hello world")

    # u1 is error, u0 is complete
    children = table.children("d1", include_status=True)
    assert any(c["_status"] == "error" for c in children)

    # Fix the failure and re-insert
    fail_on_text.discard("world")
    table.insert(doc_id="d1", text="hello world")

    children = table.children("d1", include_status=True)
    assert all(c["_status"] == "complete" for c in children)
    by_id = {c["utterance_id"]: c for c in children}
    assert by_id["u1"]["clean_text"] == "WORLD"


# ---------------------------------------------------------------------------
# on_error="store" — parent errors
# ---------------------------------------------------------------------------


def test_store_parent_error_writes_error_row():
    """on_error='store': failed parent gets error row with source columns, derived None."""
    store = MemoryStore()
    table = HyperTable(
        [parent_clean],
        identity="doc_id",
        store=store,
        on_error="store",
    ).with_runner(SyncRunner())

    fail_on_text.add("bad")
    table.insert(doc_id="d1", text="bad")

    row = table.get("d1", include_status=True)
    assert row is not None
    assert row["_status"] == "error"
    assert "ValueError" in row["_error"]
    assert row["text"] == "bad"  # source preserved
    assert row.get("clean_text") is None  # derived is None


def test_store_parent_error_retried_on_reinsert():
    """on_error='store': error parent is retried on next insert."""
    store = MemoryStore()
    table = HyperTable(
        [parent_clean],
        identity="doc_id",
        store=store,
        on_error="store",
    ).with_runner(SyncRunner())

    fail_on_text.add("bad")
    table.insert(doc_id="d1", text="bad")
    assert table.get("d1", include_status=True)["_status"] == "error"

    fail_on_text.discard("bad")
    table.insert(doc_id="d1", text="bad")
    row = table.get("d1", include_status=True)
    assert row["_status"] == "complete"
    assert row["clean_text"] == "BAD"


# ---------------------------------------------------------------------------
# on_error propagation through bind/with_runner
# ---------------------------------------------------------------------------


def test_on_error_propagates_through_bind_and_with_runner():
    """on_error policy is preserved through bind() and with_runner()."""
    store = MemoryStore()

    class FakeEmbedder:
        def _config(self):
            return {"model": "test"}

        def embed(self, text: str) -> list[float]:
            return [0.1]

    table = HyperTable(
        [parent_clean],
        identity="doc_id",
        store=store,
        on_error="store",
    )
    bound = table.bind(embedder=FakeEmbedder())
    with_runner = bound.with_runner(SyncRunner())

    fail_on_text.add("test")
    with_runner.insert(doc_id="d1", text="test")
    row = with_runner.get("d1", include_status=True)
    assert row["_status"] == "error"


# ---------------------------------------------------------------------------
# include_status on reads
# ---------------------------------------------------------------------------


def test_get_without_include_status_strips_internal():
    """get() without include_status does not expose _status/_error."""
    store = MemoryStore()
    table = HyperTable(
        [parent_clean],
        identity="doc_id",
        store=store,
    ).with_runner(SyncRunner())

    table.insert(doc_id="d1", text="hello")
    row = table.get("d1")
    assert "_status" not in row
    assert "_error" not in row


def test_filter_children_with_include_status():
    """filter_children with include_status exposes _status/_error."""
    store = MemoryStore()
    table = HyperTable(
        [split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
        identity="doc_id",
        store=store,
        on_error="store",
    ).with_runner(SyncRunner())

    fail_on_text.add("world")
    table.insert(doc_id="d1", text="hello world")

    errors = table.filter_children(
        where=[("_status", "eq", "error")],
        include_status=True,
    )
    assert len(errors) == 1
    assert errors[0]["utterance_id"] == "u1"
    assert errors[0]["_status"] == "error"


def test_filter_with_include_status():
    """filter with include_status exposes _status/_error on parent rows."""
    store = MemoryStore()
    table = HyperTable(
        [parent_clean],
        identity="doc_id",
        store=store,
        on_error="store",
    ).with_runner(SyncRunner())

    fail_on_text.add("bad")
    table.insert(doc_id="d1", text="bad")
    fail_on_text.discard("bad")
    table.insert(doc_id="d2", text="good")

    errors = table.filter(where=[("_status", "eq", "error")], include_status=True)
    assert len(errors) == 1
    assert errors[0]["doc_id"] == "d1"


# ---------------------------------------------------------------------------
# SyncResult.errors
# ---------------------------------------------------------------------------


def test_sync_with_on_error_store_populates_errors():
    """sync() with on_error='store' returns SyncResult with errored count and errors tuple."""
    store = MemoryStore()
    table = HyperTable(
        [parent_clean],
        identity="doc_id",
        store=store,
        on_error="store",
    ).with_runner(SyncRunner())

    fail_on_text.add("bad")
    result = table.sync(
        [
            {"doc_id": "d1", "text": "good"},
            {"doc_id": "d2", "text": "bad"},
            {"doc_id": "d3", "text": "also good"},
        ]
    )

    assert result.inserted == 2
    assert result.errored == 1
    assert len(result.errors) == 1
    assert result.errors[0].identity == {"doc_id": "d2"}
    assert "ValueError" in result.errors[0].error_msg


def test_sync_with_on_error_raise_propagates():
    """sync() with default on_error='raise' raises on first failure."""
    store = MemoryStore()
    table = HyperTable(
        [parent_clean],
        identity="doc_id",
        store=store,
    ).with_runner(SyncRunner())

    fail_on_text.add("bad")
    with pytest.raises(ValueError, match="Parent failed on bad"):
        table.sync(
            [
                {"doc_id": "d1", "text": "good"},
                {"doc_id": "d2", "text": "bad"},
            ]
        )


# ---------------------------------------------------------------------------
# Reserved name validation (identity/source columns)
# ---------------------------------------------------------------------------


def test_reserved_identity_name_rejected():
    """Identity column named _write_gen is rejected at analysis time."""
    store = MemoryStore()
    table = HyperTable([parent_clean], identity="_write_gen", store=store).with_runner(SyncRunner())
    with pytest.raises(ValueError, match="_write_gen.*reserved"):
        table.insert(_write_gen="d1", text="hello")


def test_reserved_identity_status_rejected():
    """Identity column named _status is rejected at analysis time."""
    store = MemoryStore()
    table = HyperTable([parent_clean], identity="_status", store=store).with_runner(SyncRunner())
    with pytest.raises(ValueError, match="_status.*reserved"):
        table.insert(_status="d1", text="hello")


def test_non_reserved_underscore_identity_allowed():
    """Identity column named _doc_ref is allowed — not a reserved name."""

    @node(output_name="clean_text")
    def clean_it(text: str) -> str:
        return text.upper()

    store = MemoryStore()
    table = HyperTable([clean_it], identity="_doc_ref", store=store).with_runner(SyncRunner())
    table.insert(_doc_ref="d1", text="hello")
    row = table.get("d1")
    assert row["clean_text"] == "HELLO"


# ---------------------------------------------------------------------------
# Async parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_store_child_error_writes_error_row():
    """Async on_error='store': failed child gets error row, sibling succeeds."""
    store = MemoryStore()
    table = HyperTable(
        [split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
        identity="doc_id",
        store=store,
        on_error="store",
    ).with_runner(AsyncRunner())

    fail_on_text.add("world")
    await table.insert(doc_id="d1", text="hello world")

    children = table.children("d1", include_status=True)
    assert len(children) == 2

    by_id = {c["utterance_id"]: c for c in children}
    assert by_id["u0"]["_status"] == "complete"
    assert by_id["u1"]["_status"] == "error"
    assert "ValueError" in by_id["u1"]["_error"]


@pytest.mark.asyncio
async def test_async_store_parent_error_writes_error_row():
    """Async on_error='store': failed parent gets error row."""
    store = MemoryStore()
    table = HyperTable(
        [parent_clean],
        identity="doc_id",
        store=store,
        on_error="store",
    ).with_runner(AsyncRunner())

    fail_on_text.add("bad")
    await table.insert(doc_id="d1", text="bad")

    row = table.get("d1", include_status=True)
    assert row["_status"] == "error"
    assert "ValueError" in row["_error"]
