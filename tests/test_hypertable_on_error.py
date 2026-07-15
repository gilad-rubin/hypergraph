"""Tests for on_error policy — error rows and partial success."""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import TableStore
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
    table = Graph([split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")]).as_table(
        identity="doc_id", store=store, runner=SyncRunner()
    )

    fail_on_text.add("world")
    with pytest.raises(ValueError, match="Failed on world"):
        table.insert(doc_id="d1", text="hello world")


def test_raise_parent_failure_propagates():
    """Default on_error='raise': parent graph failure raises."""
    store = MemoryStore()
    table = Graph([parent_clean]).as_table(identity="doc_id", store=store, runner=SyncRunner())

    fail_on_text.add("bad")
    with pytest.raises(ValueError, match="Parent failed on bad"):
        table.insert(doc_id="d1", text="bad")


# ---------------------------------------------------------------------------
# on_error="store" — child errors
# ---------------------------------------------------------------------------


def test_store_child_error_writes_error_row():
    """on_error='store': failed child gets error row, sibling succeeds."""
    store = MemoryStore()
    table = Graph([split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")]).as_table(
        identity="doc_id", store=store, on_error="store", runner=SyncRunner()
    )

    fail_on_text.add("world")
    table.insert(doc_id="d1", text="hello world")

    child = table.child("utterance")
    children = child.rows(parent="d1")
    assert len(children) == 2

    by_id = {c["utterance_id"]: c for c in children}
    assert by_id["u0"]["clean_text"] == "HELLO"
    assert by_id["u1"].get("clean_text") is None
    assert len(child.errors()) == 1
    assert child.errors()[0].id == "u1"
    assert "ValueError" in child.errors()[0].error


def test_store_error_child_retried_on_reinsert():
    """on_error='store': error child is retried on next insert, complete child skipped."""
    store = MemoryStore()
    table = Graph([split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")]).as_table(
        identity="doc_id", store=store, on_error="store", runner=SyncRunner()
    )

    fail_on_text.add("world")
    table.insert(doc_id="d1", text="hello world")

    # u1 is error, u0 is complete
    child = table.child("utterance")
    assert len(child.errors()) == 1

    # Fix the failure and re-insert
    fail_on_text.discard("world")
    table.insert(doc_id="d1", text="hello world")

    children = child.rows(parent="d1")
    assert child.errors() == ()
    by_id = {c["utterance_id"]: c for c in children}
    assert by_id["u1"]["clean_text"] == "WORLD"


# ---------------------------------------------------------------------------
# on_error="store" — parent errors
# ---------------------------------------------------------------------------


def test_store_parent_error_writes_error_row():
    """on_error='store': failed parent gets error row with source columns, derived None."""
    store = MemoryStore()
    table = Graph([parent_clean]).as_table(identity="doc_id", store=store, on_error="store", runner=SyncRunner())

    fail_on_text.add("bad")
    receipt = table.insert(doc_id="d1", text="bad")

    row = table.get("d1")
    assert row is not None
    assert receipt.failed
    assert "ValueError" in receipt.error
    assert table.errors()[0].id == "d1"
    assert row["text"] == "bad"  # source preserved
    assert row.get("clean_text") is None  # derived is None


def test_store_parent_error_retried_on_reinsert():
    """on_error='store': error parent is retried on next insert."""
    store = MemoryStore()
    table = Graph([parent_clean]).as_table(identity="doc_id", store=store, on_error="store", runner=SyncRunner())

    fail_on_text.add("bad")
    assert table.insert(doc_id="d1", text="bad").failed

    fail_on_text.discard("bad")
    table.insert(doc_id="d1", text="bad")
    row = table.get("d1")
    assert table.errors() == ()
    assert row["clean_text"] == "BAD"


def test_store_complete_parent_survives_transient_reconcile_failure():
    """on_error='store': re-inserting an unchanged (complete) parent re-runs the parent
    graph only to reconcile children. A transient failure there must NOT downgrade the
    stored-complete parent to an error row."""
    store = MemoryStore()

    @node(output_name="parent_clean_text")
    def clean_parent(text: str) -> str:
        return clean_maybe_fail(text)

    table = Graph([split_words, clean_parent, process_utterance.as_node().map_over("utterances", identity="utterance_id")]).as_table(
        identity="doc_id", store=store, on_error="store", runner=SyncRunner()
    )

    table.insert(doc_id="d1", text="hello world")
    assert table.errors() == ()
    assert table.get("d1")["parent_clean_text"] == "HELLO WORLD"

    # Parent fingerprint is unchanged (parent_skipped), but the parent graph now
    # fails on this text. The complete parent must survive untouched.
    fail_on_text.add("hello world")
    try:
        table.insert(doc_id="d1", text="hello world")
    finally:
        fail_on_text.discard("hello world")

    row = table.get("d1")
    assert row is not None, "complete parent must still exist after a transient reconcile failure"
    assert table.errors() == (), "complete parent must not be downgraded to error"
    assert table.get("d1")["parent_clean_text"] == "HELLO WORLD"


# ---------------------------------------------------------------------------
# on_error propagation through bind/with_runner
# ---------------------------------------------------------------------------


def test_on_error_store_returns_a_failed_receipt():
    store = MemoryStore()

    table = Graph([parent_clean]).as_table(identity="doc_id", store=store, on_error="store", runner=SyncRunner())

    fail_on_text.add("test")
    receipt = table.insert(doc_id="d1", text="test")

    assert receipt.failed
    assert table.errors()[0].id == "d1"


# ---------------------------------------------------------------------------
# include_status on reads
# ---------------------------------------------------------------------------


def test_get_without_include_status_strips_internal():
    """get() without include_status does not expose _status/_error."""
    store = MemoryStore()
    table = Graph([parent_clean]).as_table(identity="doc_id", store=store, runner=SyncRunner())

    table.insert(doc_id="d1", text="hello")
    row = table.get("d1")
    assert "_status" not in row
    assert "_error" not in row


def test_child_errors_returns_typed_rows():
    store = MemoryStore()
    table = Graph([split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")]).as_table(
        identity="doc_id", store=store, on_error="store", runner=SyncRunner()
    )

    fail_on_text.add("world")
    table.insert(doc_id="d1", text="hello world")

    errors = table.child("utterance").errors()
    assert len(errors) == 1
    assert errors[0].id == "u1"
    assert "ValueError" in errors[0].error


def test_errors_returns_typed_parent_rows():
    store = MemoryStore()
    table = Graph([parent_clean]).as_table(identity="doc_id", store=store, on_error="store", runner=SyncRunner())

    fail_on_text.add("bad")
    table.insert(doc_id="d1", text="bad")
    fail_on_text.discard("bad")
    table.insert(doc_id="d2", text="good")

    errors = table.errors()
    assert len(errors) == 1
    assert errors[0].id == "d1"


# ---------------------------------------------------------------------------
# SyncResult.errors
# ---------------------------------------------------------------------------


def test_sync_with_on_error_store_populates_errors():
    """sync() with on_error='store' returns SyncResult with errored count and errors tuple."""
    store = MemoryStore()
    table = Graph([parent_clean]).as_table(identity="doc_id", store=store, on_error="store", runner=SyncRunner())

    fail_on_text.add("bad")
    result = table.sync(
        [
            {"doc_id": "d1", "text": "good"},
            {"doc_id": "d2", "text": "bad"},
            {"doc_id": "d3", "text": "also good"},
        ]
    )

    assert result.inserted == 3
    assert len(result.errors) == 1
    assert result.errors[0].id == "d2"
    assert "ValueError" in result.errors[0].error


def test_sync_with_on_error_raise_propagates():
    """sync() with default on_error='raise' raises on first failure."""
    store = MemoryStore()
    table = Graph([parent_clean]).as_table(identity="doc_id", store=store, runner=SyncRunner())

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
    table = Graph([parent_clean]).as_table(identity="_write_gen", store=store, runner=SyncRunner())
    with pytest.raises(ValueError, match="_write_gen.*reserved"):
        table.insert(_write_gen="d1", text="hello")


def test_reserved_identity_status_rejected():
    """Identity column named _status is rejected at analysis time."""
    store = MemoryStore()
    table = Graph([parent_clean]).as_table(identity="_status", store=store, runner=SyncRunner())
    with pytest.raises(ValueError, match="_status.*reserved"):
        table.insert(_status="d1", text="hello")


def test_non_reserved_underscore_identity_allowed():
    """Identity column named _doc_ref is allowed — not a reserved name."""

    @node(output_name="clean_text")
    def clean_it(text: str) -> str:
        return text.upper()

    store = MemoryStore()
    table = Graph([clean_it]).as_table(identity="_doc_ref", store=store, runner=SyncRunner())
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
    table = Graph([split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")]).as_table(
        identity="doc_id", store=store, on_error="store", runner=AsyncRunner()
    )

    fail_on_text.add("world")
    await table.insert(doc_id="d1", text="hello world")

    child = table.child("utterance")
    children = child.rows(parent="d1")
    assert len(children) == 2

    by_id = {c["utterance_id"]: c for c in children}
    assert by_id["u0"]["clean_text"] == "HELLO"
    assert by_id["u1"].get("clean_text") is None
    assert "ValueError" in child.errors()[0].error


@pytest.mark.asyncio
async def test_async_store_parent_error_writes_error_row():
    """Async on_error='store': failed parent gets error row."""
    store = MemoryStore()
    table = Graph([parent_clean]).as_table(identity="doc_id", store=store, on_error="store", runner=AsyncRunner())

    fail_on_text.add("bad")
    await table.insert(doc_id="d1", text="bad")

    assert table.errors()[0].id == "d1"
    assert "ValueError" in table.errors()[0].error
