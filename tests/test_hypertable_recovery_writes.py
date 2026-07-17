"""Recovery-write contract (#205): unchanged error rows retry during sync.

A root row whose stored ``_status`` is ``"error"`` must not be treated as
converged just because its fingerprint is unchanged — sync retries it. A
successful retry replaces the error state (with current recipe/provenance
stamps); a repeated failure stores or raises per ``on_error``. Successful
unchanged rows remain zero-execution skips. A retried error row can itself
write children, so the retry must also honor the child-generation ordering
contract (child mutations land strictly above every physical child row).
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import TableStore
from hypergraph.runners import AsyncRunner, SyncRunner


class MemoryStore(TableStore):
    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = rows if rows is not None else {}

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
        if op == "in" and current not in value:
            return False
    return True


fail_on_text: set[str] = set()
executions = {"clean": 0, "split": 0, "child": 0}


@node(output_name="clean_text")
def clean(text: str) -> str:
    executions["clean"] += 1
    if text in fail_on_text:
        raise ValueError(f"failed on {text}")
    return text.upper()


class Utterance(TypedDict):
    utterance_id: str
    text: str


@node(output_name="utterances")
def split_words(text: str) -> list[Utterance]:
    executions["split"] += 1
    if text in fail_on_text:
        raise ValueError(f"failed on {text}")
    return [Utterance(utterance_id=f"u{i}", text=word) for i, word in enumerate(text.split())]


@node(output_name="clean_word")
def clean_word(text: str) -> str:
    executions["child"] += 1
    return text.upper()


process_word = Graph([clean_word], name="process_word")


@pytest.fixture(autouse=True)
def _reset():
    fail_on_text.clear()
    executions["clean"] = 0
    executions["child"] = 0


def _root_table(store, runner=None, on_error="store"):
    return Graph([clean], name="doc").as_table(identity="doc_id", store=store, on_error=on_error, runner=runner or SyncRunner())


def _child_table(store, runner=None, on_error="store"):
    return Graph(
        [split_words, process_word.as_node().map_over("utterances", identity="utterance_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, on_error=on_error, runner=runner or SyncRunner())


def _physical(store, table_name):
    return [{key: row.get(key) for key in ("doc_id", "_status", "_error", "_write_gen", "clean_text")} for row in store.rows[table_name]]


# ---------------------------------------------------------------------------
# Retry on sync — success replaces the error state
# ---------------------------------------------------------------------------


def test_sync_retries_unchanged_error_root_row():
    """An unchanged-fingerprint row with stored _status='error' is retried by
    sync; success replaces the error row in the physical store."""

    store = MemoryStore()
    table = _root_table(store)
    fail_on_text.add("bad")
    table.sync([{"doc_id": "d1", "text": "bad"}])
    assert [row["_status"] for row in store.rows["doc"]] == ["error"]

    fail_on_text.discard("bad")
    receipt = table.sync([{"doc_id": "d1", "text": "bad"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("updated", "complete")]
    physical = store.rows["doc"]
    assert len(physical) == 1, f"stale error row must be cleaned up: {_physical(store, 'doc')}"
    assert physical[0]["_status"] == "complete"
    assert physical[0]["_error"] is None
    assert physical[0]["clean_text"] == "BAD"
    assert table.errors() == ()


def test_insert_retries_unchanged_error_root_row():
    """The same recovery through insert(): an unchanged error row re-runs."""

    store = MemoryStore()
    table = _root_table(store)
    fail_on_text.add("bad")
    assert table.insert(doc_id="d1", text="bad").failed

    fail_on_text.discard("bad")
    receipt = table.insert(doc_id="d1", text="bad")

    assert not receipt.failed
    physical = store.rows["doc"]
    assert len(physical) == 1
    assert physical[0]["_status"] == "complete"
    assert table.get("d1")["clean_text"] == "BAD"


# ---------------------------------------------------------------------------
# on_error matrix for the retry itself
# ---------------------------------------------------------------------------


def test_retry_failure_stores_fresh_error_row():
    """on_error='store': a retry that fails again stores the new error state."""

    store = MemoryStore()
    table = _root_table(store)
    fail_on_text.add("bad")
    table.sync([{"doc_id": "d1", "text": "bad"}])
    first_gen = store.rows["doc"][0]["_write_gen"]

    receipt = table.sync([{"doc_id": "d1", "text": "bad"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("updated", "error")]
    physical = store.rows["doc"]
    assert len(physical) == 1, "repeated failure must not accumulate error rows"
    assert physical[0]["_status"] == "error"
    assert "ValueError" in physical[0]["_error"]
    assert physical[0]["_write_gen"] > first_gen
    assert executions["clean"] == 2, "the retry must actually re-execute the graph"


def test_retry_failure_raises_with_on_error_raise():
    """on_error='raise': the retry propagates and the stored error row survives."""

    store = MemoryStore()
    fail_on_text.add("bad")
    _root_table(store, on_error="store").sync([{"doc_id": "d1", "text": "bad"}])
    before = _physical(store, "doc")

    strict = _root_table(store, on_error="raise")
    with pytest.raises(ValueError, match="failed on bad"):
        strict.sync([{"doc_id": "d1", "text": "bad"}])

    assert _physical(store, "doc") == before, "a raised retry must leave the stored row untouched"


# ---------------------------------------------------------------------------
# Successful unchanged rows stay zero-execution skips
# ---------------------------------------------------------------------------


def test_successful_unchanged_rows_stay_zero_execution_skips():
    store = MemoryStore()
    table = _child_table(store)
    table.sync([{"doc_id": "d1", "text": "hello world"}])
    snapshot = {name: [row.copy() for row in rows] for name, rows in store.rows.items()}
    executions["split"] = 0
    executions["child"] = 0

    receipt = table.sync([{"doc_id": "d1", "text": "hello world"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("skipped", "complete")]
    assert (executions["split"], executions["child"]) == (0, 0), "an unchanged complete row must not execute anything"
    assert store.rows == snapshot, "an unchanged complete row must not touch the physical store"


# ---------------------------------------------------------------------------
# A retried error row writes children under the generation-ordering contract
# ---------------------------------------------------------------------------


def test_retried_error_row_writes_children_above_every_physical_generation():
    """The join point of both #205 halves: an error-row retry that produces child
    rows must allocate a child generation strictly greater than every physical
    child row — even when annotation bumps pushed the child counter ahead of the
    root counter."""

    store = MemoryStore()
    table = _child_table(store)
    table.insert(doc_id="d1", text="hello")
    table.child("utterance").set({"utterance_id": "u0"}, note="first")
    table.child("utterance").set({"utterance_id": "u0"}, note="second")
    child_max_before = store.max_write_gen("utterance")
    assert child_max_before > store.max_write_gen(table.table_name), "annotation bumps must outrun the root counter for this witness"

    fail_on_text.add("world")
    assert table.update("d1", text="world").failed
    fail_on_text.discard("world")

    receipt = table.sync([{"doc_id": "d1", "text": "world"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("updated", "complete")]
    physical = store.rows["utterance"]
    gens = [row["_write_gen"] for row in physical]
    assert len(gens) == len(set(gens)), f"equal-generation tie in physical child store: {physical}"
    assert min(gens) > child_max_before
    assert [row["text"] for row in physical] == ["world"]

    fresh = _child_table(MemoryStore(store.rows))
    assert [child["clean_word"] for child in fresh.child("utterance").rows(parent="d1")] == ["WORLD"]


# ---------------------------------------------------------------------------
# Recipe/provenance stamps on retried rows (#275 journaling stays truthful)
# ---------------------------------------------------------------------------


def test_retried_row_carries_current_recipe_and_provenance_stamps():
    store = MemoryStore()
    table = _root_table(store)
    fail_on_text.add("bad")
    table.sync([{"doc_id": "d1", "text": "bad"}])
    fail_on_text.discard("bad")

    table.sync([{"doc_id": "d1", "text": "bad"}])

    row = store.rows["doc"][0]
    assert isinstance(row.get("_recipe_fingerprint"), str) and row["_recipe_fingerprint"]
    assert isinstance(row.get("_provenance_clean_text"), str) and row["_provenance_clean_text"]

    drift = table.recipe_drift()
    assert (drift.total, drift.current, drift.drifted, drift.unknown) == (1, 1, 0, 0)

    explained = table.explain("d1")
    assert "executions" in (explained["clean_text"]["source"] or ""), "the journal must resolve the retried row's recipe to real node source"


# ---------------------------------------------------------------------------
# Async parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_sync_retries_unchanged_error_root_row():
    store = MemoryStore()
    table = _root_table(store, runner=AsyncRunner())
    fail_on_text.add("bad")
    await table.sync([{"doc_id": "d1", "text": "bad"}])
    assert [row["_status"] for row in store.rows["doc"]] == ["error"]

    fail_on_text.discard("bad")
    receipt = await table.sync([{"doc_id": "d1", "text": "bad"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("updated", "complete")]
    physical = store.rows["doc"]
    assert len(physical) == 1
    assert physical[0]["_status"] == "complete"
    assert table.errors() == ()


@pytest.mark.asyncio
async def test_async_retried_error_row_writes_children_above_every_physical_generation():
    store = MemoryStore()
    table = _child_table(store, runner=AsyncRunner())
    await table.insert(doc_id="d1", text="hello")
    table.child("utterance").set({"utterance_id": "u0"}, note="first")
    table.child("utterance").set({"utterance_id": "u0"}, note="second")
    child_max_before = store.max_write_gen("utterance")

    fail_on_text.add("world")
    assert (await table.update("d1", text="world")).failed
    fail_on_text.discard("world")

    await table.sync([{"doc_id": "d1", "text": "world"}])

    physical = store.rows["utterance"]
    gens = [row["_write_gen"] for row in physical]
    assert len(gens) == len(set(gens)), f"equal-generation tie in physical child store: {physical}"
    assert min(gens) > child_max_before
    assert [row["text"] for row in physical] == ["world"]
