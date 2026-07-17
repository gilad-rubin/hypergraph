"""Crash-safety regressions for HyperTable child writes."""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import TableStore
from hypergraph.runners import SyncRunner


class MemoryStore(TableStore):
    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = rows if rows is not None else {}
        self.fail_child_writes = False
        self.fail_writes_for: str | None = None

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
        if self.fail_writes_for == table_name:
            raise RuntimeError(f"simulated crash writing {table_name}")
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
    table = Graph([split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")]).as_table(
        identity="doc_id", store=store, runner=SyncRunner()
    )

    table.insert(doc_id="d1", text="old words")
    assert [child["text"] for child in table.child(table.child_table_names[0]).rows(parent="d1")] == ["old", "words"]

    store.fail_child_writes = True
    with pytest.raises(RuntimeError, match="child write failed"):
        table.insert(doc_id="d1", text="new child rows")

    assert [child["text"] for child in table.child(table.child_table_names[0]).rows(parent="d1")] == ["old", "words"]


# ---------------------------------------------------------------------------
# Child generation ordering (#205): every child-table mutation must allocate a
# generation strictly greater than every physical row currently in that table,
# not merely greater than the parent table's previous generation. A crash
# between the child write and the parent write leaves an orphaned child one
# generation ahead of the root counter; the repair write must beat it, or the
# stale row ties, survives cleanup (which deletes only OLDER generations), and
# can win the public dedup.
# ---------------------------------------------------------------------------


@node(output_name="items")
def one_item(text: str) -> list[Utterance]:
    return [Utterance(utterance_id="u0", text=text)]


def _single_child_table(store, runner=None):
    child_graph = Graph([clean], name="utterance_graph")
    return Graph(
        [one_item, child_graph.as_node().map_over("items", identity="utterance_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, runner=runner or SyncRunner())


def test_child_repair_after_crash_beats_orphaned_generation() -> None:
    """ALPHA -> BETA (crash before parent write) -> GAMMA: the GAMMA child row must
    land at a generation strictly greater than the orphaned BETA row, so cleanup
    removes BETA and no equal-generation tie survives in the physical store."""

    store = MemoryStore()
    table = _single_child_table(store)
    table.insert(doc_id="d1", text="ALPHA")

    store.fail_writes_for = table.table_name
    with pytest.raises(RuntimeError, match="simulated crash"):
        table.insert(doc_id="d1", text="BETA")
    store.fail_writes_for = None

    orphan = max(store.rows["utterance"], key=lambda row: row["_write_gen"])
    assert orphan["text"] == "BETA"
    orphan_gen = orphan["_write_gen"]
    assert store.max_write_gen(table.table_name) < orphan_gen  # child counter ran ahead

    # Repair through a FRESH handle over the same physical rows — same-handle
    # caching must not mask the tie.
    fresh = _single_child_table(MemoryStore(store.rows))
    fresh.insert(doc_id="d1", text="GAMMA")

    physical = store.rows["utterance"]
    gens = [row["_write_gen"] for row in physical]
    assert len(gens) == len(set(gens)), f"equal-generation tie in physical store: {physical}"
    assert [row["text"] for row in physical] == ["GAMMA"], "stale orphan must be cleaned up"
    assert min(gens) > orphan_gen

    inspector = _single_child_table(MemoryStore(store.rows))
    visible = inspector.child("utterance").rows(parent="d1")
    assert [child["clean_text"] for child in visible] == ["GAMMA"]


def test_annotation_bump_then_content_change_does_not_tie() -> None:
    """ChildTable.set() advances the child generation counter independently of the
    root table; the next content-changing upsert must still allocate a strictly
    newer generation, or the annotated stale row ties and can shadow new content."""

    store = MemoryStore()
    table = _single_child_table(store)
    table.insert(doc_id="d1", text="alpha")
    table.child("utterance").set({"utterance_id": "u0"}, note="keep")
    annotated_gen = max(row["_write_gen"] for row in store.rows["utterance"])

    table.insert(doc_id="d1", text="beta")

    physical = store.rows["utterance"]
    gens = [row["_write_gen"] for row in physical]
    assert len(gens) == len(set(gens)), f"equal-generation tie in physical store: {physical}"
    assert [row["text"] for row in physical] == ["beta"]
    assert min(gens) > annotated_gen

    fresh = _single_child_table(MemoryStore(store.rows))
    assert [child["clean_text"] for child in fresh.child("utterance").rows(parent="d1")] == ["BETA"]


class GenerationProbeStore(MemoryStore):
    """Direct-store probe: a child-table write may never land at a generation that
    ties or regresses against a physical row already present for the same
    (parent, child) identity."""

    def write_rows(self, table_name, rows):
        if table_name == "utterance":
            for row in rows:
                key = (row.get("_parent_id"), row.get("utterance_id"))
                for existing in self.rows.get(table_name, []):
                    if (existing.get("_parent_id"), existing.get("utterance_id")) == key:
                        assert existing["_write_gen"] < row["_write_gen"], (
                            f"child write generation tie/regression for {key}: existing gen {existing['_write_gen']}, incoming gen {row['_write_gen']}"
                        )
        super().write_rows(table_name, rows)


def test_interleaved_child_mutations_never_tie_in_physical_store() -> None:
    """Two sync passes interleaved with a crash-orphaned child write and an
    annotation bump: no child mutation may ever tie in the physical store."""

    store = GenerationProbeStore()
    table = Graph([split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")]).as_table(
        identity="doc_id", store=store, runner=SyncRunner()
    )

    table.sync([{"doc_id": "d1", "text": "alpha one"}, {"doc_id": "d2", "text": "beta"}])

    store.fail_writes_for = table.table_name
    with pytest.raises(RuntimeError, match="simulated crash"):
        table.insert(doc_id="d1", text="gamma two")
    store.fail_writes_for = None

    table.child("utterance").set({"utterance_id": "u0"}, note="keep")

    fresh = Graph([split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")]).as_table(
        identity="doc_id", store=MemoryStore(store.rows), runner=SyncRunner()
    )
    fresh.sync([{"doc_id": "d1", "text": "delta three"}, {"doc_id": "d2", "text": "epsilon"}])

    by_key: dict[tuple[Any, Any], list[int]] = {}
    for row in store.rows["utterance"]:
        by_key.setdefault((row["_parent_id"], row["utterance_id"]), []).append(row["_write_gen"])
    for key, gens in by_key.items():
        assert len(gens) == len(set(gens)), f"equal-generation tie for {key}: {gens}"

    visible = {(child["doc_id"], child["utterance_id"]): child["text"] for child in fresh.child("utterance").rows()}
    assert visible == {("d1", "u0"): "delta", ("d1", "u1"): "three", ("d2", "u0"): "epsilon"}


def test_child_repair_after_crash_fresh_lancedb_handles(tmp_path) -> None:
    """The fresh-handle falsifier on a real store: separate LanceDB connections for
    the crash, the repair, and the read must expose only the repaired child."""

    from hypergraph.materialization import LanceDBStore

    class CrashingLanceStore(LanceDBStore):
        fail_root = False

        def write_rows(self, table_name, rows):
            if self.fail_root and table_name == "doc":
                raise RuntimeError("simulated crash before parent write")
            super().write_rows(table_name, rows)

    path = str(tmp_path / "ordering_store")
    store = CrashingLanceStore(path)
    table = _single_child_table(store)
    table.insert(doc_id="d1", text="ALPHA")

    store.fail_root = True
    with pytest.raises(RuntimeError, match="simulated crash"):
        table.insert(doc_id="d1", text="BETA")

    repair_store = LanceDBStore(path)
    repair = _single_child_table(repair_store)
    repair.insert(doc_id="d1", text="GAMMA")

    read_store = LanceDBStore(path)
    physical = read_store.read_rows("utterance")
    gens = [row["_write_gen"] for row in physical]
    assert len(gens) == len(set(gens)), f"equal-generation tie in physical store: {physical}"
    assert [row["text"] for row in physical] == ["GAMMA"]

    inspector = _single_child_table(read_store)
    assert [child["clean_text"] for child in inspector.child("utterance").rows(parent="d1")] == ["GAMMA"]


def test_children_and_count_deduplicate_crash_leftovers() -> None:
    """Public child reads expose one logical child per identity."""

    store = MemoryStore()
    table = Graph([split_words, process_utterance.as_node().map_over("utterances", identity="utterance_id")]).as_table(
        identity="doc_id", store=store, runner=SyncRunner()
    )

    table.insert(doc_id="d1", text="old words")
    stale = store.read_one("utterance", "utterance_id", "u0")
    store.write_rows("utterance", [{**stale, "text": "new", "clean_text": "NEW", "_write_gen": stale["_write_gen"] + 100}])

    children = table.child(table.child_table_names[0]).rows(parent="d1")

    assert len(children) == 2
    assert {child["utterance_id"] for child in children} == {"u0", "u1"}
    assert next(child for child in children if child["utterance_id"] == "u0")["text"] == "new"
    assert table.child("utterance").count() == 2
