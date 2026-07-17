"""Unchanged-parent sync heals physically missing child rows (#204 / #238).

A successful parent row must not make known child loss permanent. When
``sync()`` meets an unchanged parent fingerprint it now compares each child
table's recorded fan-out count (the ``<provenance>#<count>`` value stored on
the parent row) against the physical deduplicated child rows:

- all children present  -> today's fast path: ``skipped``, zero derivation
  executions, zero writes (byte-identical store);
- child rows missing    -> the fan-out boundary re-runs once to regenerate the
  item list, ONLY the missing children run the child graph, present children
  and parent derived columns are not re-derived, and the receipt reports
  ``healed`` — never ``skipped`` on a path that wrote rows.

Healed child writes honor the #205 generation-ordering contract: the child
generation lands strictly above every physical row in that child table.
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


class AccessProbeStore(MemoryStore):
    """Counts reads and writes per table — the detection-cost witness."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        super().__init__(rows)
        self.reads: dict[str, int] = {}
        self.writes: dict[str, int] = {}
        self.deletes: dict[str, int] = {}

    def read_rows(self, table_name, where=None, *, limit=None):
        self.reads[table_name] = self.reads.get(table_name, 0) + 1
        return super().read_rows(table_name, where, limit=limit)

    def write_rows(self, table_name, rows):
        self.writes[table_name] = self.writes.get(table_name, 0) + 1
        super().write_rows(table_name, rows)

    def delete_rows(self, table_name, where):
        self.deletes[table_name] = self.deletes.get(table_name, 0) + 1
        return super().delete_rows(table_name, where)


class Utterance(TypedDict):
    utterance_id: str
    text: str


executions = {"clean": 0, "split": 0, "child": 0}


@node(output_name="clean_text")
def clean(text: str) -> str:
    executions["clean"] += 1
    return text.upper()


@node(output_name="utterances")
def split_words(text: str) -> list[Utterance]:
    executions["split"] += 1
    return [Utterance(utterance_id=f"u{i}", text=word) for i, word in enumerate(text.split())]


@node(output_name="clean_word")
def clean_word(text: str) -> str:
    executions["child"] += 1
    return text.upper()


process_word = Graph([clean_word], name="process_word")


@pytest.fixture(autouse=True)
def _reset():
    executions["clean"] = 0
    executions["split"] = 0
    executions["child"] = 0


def _table(store, runner=None):
    return Graph(
        [clean, split_words, process_word.as_node().map_over("utterances", identity="utterance_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, runner=runner or SyncRunner())


def _child_ids(store):
    return sorted(row["utterance_id"] for row in store.rows["utterance"])


# ---------------------------------------------------------------------------
# 1. RED: unchanged parent + physically deleted child row is rebuilt by sync()
# ---------------------------------------------------------------------------


def test_sync_heals_physically_missing_child_row_under_unchanged_parent():
    """On master the unchanged-parent fast path never inspects the child table,
    so a physically deleted child row stays missing forever. After the fix the
    same sync() call rebuilds it."""

    store = MemoryStore()
    _table(store).sync([{"doc_id": "d1", "text": "alpha beta gamma"}])
    assert _child_ids(store) == ["u0", "u1", "u2"]

    store.delete_rows("utterance", [("utterance_id", "eq", "u1")])
    assert _child_ids(store) == ["u0", "u2"]

    # A FRESH handle over the same physical rows: detection must read the
    # store, not same-handle caches.
    fresh = _table(MemoryStore(store.rows))
    receipt = fresh.sync([{"doc_id": "d1", "text": "alpha beta gamma"}])

    assert _child_ids(store) == ["u0", "u1", "u2"], "sync() must rebuild the physically missing child row"
    healed = next(row for row in store.rows["utterance"] if row["utterance_id"] == "u1")
    assert healed["clean_word"] == "BETA"
    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("healed", "complete")]


def test_sync_rebuilds_fully_truncated_child_table():
    """Even total child loss (table truncated) is healed under an unchanged parent."""

    store = MemoryStore()
    _table(store).sync([{"doc_id": "d1", "text": "alpha beta gamma"}])
    store.delete_rows("utterance", [("_parent_id", "eq", "d1")])
    assert store.rows["utterance"] == []

    fresh = _table(MemoryStore(store.rows))
    receipt = fresh.sync([{"doc_id": "d1", "text": "alpha beta gamma"}])

    assert _child_ids(store) == ["u0", "u1", "u2"]
    assert {row["utterance_id"]: row["clean_word"] for row in store.rows["utterance"]} == {"u0": "ALPHA", "u1": "BETA", "u2": "GAMMA"}
    assert receipt.healed == 1


# ---------------------------------------------------------------------------
# 2. Fast path preserved: all children present -> zero executions, zero writes
# ---------------------------------------------------------------------------


def test_unchanged_parent_with_all_children_present_stays_zero_execution_and_zero_write():
    store = MemoryStore()
    table = _table(store)
    table.sync([{"doc_id": "d1", "text": "alpha beta gamma"}])
    snapshot = {name: [row.copy() for row in rows] for name, rows in store.rows.items()}
    executions["clean"] = executions["split"] = executions["child"] = 0

    receipt = table.sync([{"doc_id": "d1", "text": "alpha beta gamma"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("skipped", "complete")]
    assert (executions["clean"], executions["split"], executions["child"]) == (0, 0, 0), "the fast path must stay zero-execution"
    assert store.rows == snapshot, "the fast path must stay zero-write: store byte-identical, no generation inflation"


def test_fast_path_detection_is_one_child_table_read_per_unchanged_parent():
    """Detection-cost honesty: the fast path now performs exactly one read of
    each child table per unchanged parent row (the completeness probe) and
    still writes and deletes nothing."""

    seed = MemoryStore()
    _table(seed).sync([{"doc_id": "d1", "text": "alpha beta gamma"}])

    probe = AccessProbeStore(seed.rows)
    table = _table(probe)
    probe.reads.clear()
    probe.writes.clear()
    probe.deletes.clear()

    table.sync([{"doc_id": "d1", "text": "alpha beta gamma"}])

    assert probe.reads.get("utterance", 0) == 1, "detection is one per-child-table read per unchanged parent"
    assert probe.writes == {}, "the fast path must not write"
    assert probe.deletes == {}, "the fast path must not delete"


# ---------------------------------------------------------------------------
# 3. Partial heal: only the MISSING children derive
# ---------------------------------------------------------------------------


def test_partial_heal_derives_only_missing_children():
    store = MemoryStore()
    _table(store).sync([{"doc_id": "d1", "text": "alpha beta gamma"}])
    parent_gen_before = store.read_one("doc", "doc_id", "d1")["_write_gen"]
    present_values = {row["utterance_id"]: row["clean_word"] for row in store.rows["utterance"] if row["utterance_id"] != "u1"}

    store.delete_rows("utterance", [("utterance_id", "eq", "u1")])
    executions["clean"] = executions["split"] = executions["child"] = 0

    fresh = _table(MemoryStore(store.rows))
    fresh.sync([{"doc_id": "d1", "text": "alpha beta gamma"}])

    # Only the missing child runs the child graph; the fan-out boundary re-runs
    # exactly once to regenerate the item list (physically missing rows exist
    # nowhere else); parent derived columns are not re-derived.
    assert executions["child"] == 1, "only the MISSING child may derive"
    assert executions["split"] == 1, "the boundary regenerates the item list exactly once"
    assert executions["clean"] == 0, "parent derived columns must not re-derive"
    assert store.read_one("doc", "doc_id", "d1")["_write_gen"] == parent_gen_before, "the parent row must not be rewritten"
    assert {row["utterance_id"]: row["clean_word"] for row in store.rows["utterance"] if row["utterance_id"] != "u1"} == present_values, (
        "present children keep their derived values"
    )


# ---------------------------------------------------------------------------
# 4. Truthful receipts: healed is distinct, skipped never writes
# ---------------------------------------------------------------------------


def test_healing_sync_reports_healed_not_skipped():
    store = MemoryStore()
    table = _table(store)
    table.sync([{"doc_id": "d1", "text": "alpha beta"}, {"doc_id": "d2", "text": "delta"}])
    store.delete_rows("utterance", [("_parent_id", "eq", "d1"), ("utterance_id", "eq", "u1")])

    fresh = _table(MemoryStore(store.rows))
    receipt = fresh.sync([{"doc_id": "d1", "text": "alpha beta"}, {"doc_id": "d2", "text": "delta"}])

    outcomes = {row.id: row.outcome.value for row in receipt.receipts}
    assert outcomes == {"d1": "healed", "d2": "skipped"}, "the repaired row reports the repair distinctly; untouched rows stay skipped"
    assert receipt.healed == 1
    assert receipt.skipped == 1
    assert all(row.status.value == "complete" for row in receipt.receipts)


# ---------------------------------------------------------------------------
# 5. Generation safety: healed children land above every physical child row
# ---------------------------------------------------------------------------


def test_healed_children_allocate_generation_above_physical_child_max():
    """The #205 ordering contract under healing: even when annotation bumps have
    pushed the child counter ahead of the root counter, the heal allocates
    strictly above every physical child row — no tie can survive cleanup."""

    store = MemoryStore()
    table = _table(store)
    table.sync([{"doc_id": "d1", "text": "alpha beta gamma"}])
    table.child("utterance").set({"utterance_id": "u0"}, note="first")
    table.child("utterance").set({"utterance_id": "u0"}, note="second")
    child_max_before = store.max_write_gen("utterance")
    assert child_max_before > store.max_write_gen("doc"), "annotation bumps must outrun the root counter for this witness"

    store.delete_rows("utterance", [("utterance_id", "eq", "u1")])

    fresh = _table(MemoryStore(store.rows))
    receipt = fresh.sync([{"doc_id": "d1", "text": "alpha beta gamma"}])

    assert receipt.healed == 1
    physical = store.rows["utterance"]
    by_key: dict[tuple[Any, Any], list[int]] = {}
    for row in physical:
        by_key.setdefault((row["_parent_id"], row["utterance_id"]), []).append(row["_write_gen"])
    for key, gens in by_key.items():
        assert len(gens) == len(set(gens)), f"equal-generation tie for {key}: {gens}"
    assert min(row["_write_gen"] for row in physical) > child_max_before, (
        "every child write in the healing mutation lands strictly above the pre-heal physical max"
    )

    reader = _table(MemoryStore(store.rows))
    assert [child["clean_word"] for child in reader.child("utterance").rows(parent="d1")] == ["ALPHA", "BETA", "GAMMA"]


# ---------------------------------------------------------------------------
# LanceDB: the heal path against a real store through fresh connections
# ---------------------------------------------------------------------------


def test_sync_heals_missing_child_row_lancedb_fresh_handles(tmp_path):
    from hypergraph.materialization import LanceDBStore

    path = str(tmp_path / "healing_store")
    _table(LanceDBStore(path)).sync([{"doc_id": "d1", "text": "alpha beta gamma"}])

    damage_store = LanceDBStore(path)
    damage_store.delete_rows("utterance", [("utterance_id", "eq", "u1")])
    child_max_before = damage_store.max_write_gen("utterance")
    assert sorted(row["utterance_id"] for row in damage_store.read_rows("utterance")) == ["u0", "u2"]

    heal_store = LanceDBStore(path)
    receipt = _table(heal_store).sync([{"doc_id": "d1", "text": "alpha beta gamma"}])
    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("healed", "complete")]

    read_store = LanceDBStore(path)
    physical = read_store.read_rows("utterance")
    assert sorted(row["utterance_id"] for row in physical) == ["u0", "u1", "u2"]
    by_key: dict[tuple[Any, Any], list[int]] = {}
    for row in physical:
        by_key.setdefault((row["_parent_id"], row["utterance_id"]), []).append(row["_write_gen"])
    for key, gens in by_key.items():
        assert len(gens) == len(set(gens)), f"equal-generation tie for {key}: {gens}"
    assert min(row["_write_gen"] for row in physical) > child_max_before, "the healing mutation lands strictly above the pre-heal physical max"

    inspector = _table(read_store)
    assert [child["clean_word"] for child in inspector.child("utterance").rows(parent="d1")] == ["ALPHA", "BETA", "GAMMA"]


def test_sync_fast_path_stays_zero_write_lancedb(tmp_path):
    from hypergraph.materialization import LanceDBStore

    path = str(tmp_path / "fastpath_store")
    _table(LanceDBStore(path)).sync([{"doc_id": "d1", "text": "alpha beta"}])

    snapshot_store = LanceDBStore(path)
    snapshot = {
        name: sorted((sorted(row.items(), key=lambda kv: kv[0]) for row in snapshot_store.read_rows(name)), key=str) for name in ("doc", "utterance")
    }
    executions["clean"] = executions["split"] = executions["child"] = 0

    receipt = _table(LanceDBStore(path)).sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("skipped", "complete")]
    assert (executions["clean"], executions["split"], executions["child"]) == (0, 0, 0)
    verify_store = LanceDBStore(path)
    after = {
        name: sorted((sorted(row.items(), key=lambda kv: kv[0]) for row in verify_store.read_rows(name)), key=str) for name in ("doc", "utterance")
    }
    assert after == snapshot, "the fast path must not write to the physical store"


# ---------------------------------------------------------------------------
# Async parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_sync_heals_missing_child_row():
    store = MemoryStore()
    await _table(store, runner=AsyncRunner()).sync([{"doc_id": "d1", "text": "alpha beta gamma"}])
    store.delete_rows("utterance", [("utterance_id", "eq", "u1")])
    executions["clean"] = executions["split"] = executions["child"] = 0

    fresh = _table(MemoryStore(store.rows), runner=AsyncRunner())
    receipt = await fresh.sync([{"doc_id": "d1", "text": "alpha beta gamma"}])

    assert _child_ids(store) == ["u0", "u1", "u2"]
    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("healed", "complete")]
    assert (executions["clean"], executions["split"], executions["child"]) == (0, 1, 1)


@pytest.mark.asyncio
async def test_async_fast_path_stays_zero_execution_and_zero_write():
    store = MemoryStore()
    table = _table(store, runner=AsyncRunner())
    await table.sync([{"doc_id": "d1", "text": "alpha beta gamma"}])
    snapshot = {name: [row.copy() for row in rows] for name, rows in store.rows.items()}
    executions["clean"] = executions["split"] = executions["child"] = 0

    receipt = await table.sync([{"doc_id": "d1", "text": "alpha beta gamma"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("skipped", "complete")]
    assert (executions["clean"], executions["split"], executions["child"]) == (0, 0, 0)
    assert store.rows == snapshot
