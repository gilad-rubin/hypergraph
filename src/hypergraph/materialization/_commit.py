"""The store boundary for HyperTable write plans.

``WritePlanner`` documents itself as yielding only graph execution effects, but
it reached the store directly from five different responsibilities. Every
physical read and write a write plan makes now goes through ``TableCommitter``,
which owns one protocol end to end: a mutation writes rows at a write
generation strictly above every physical row it could collide with, then
deletes the generations it superseded (the tombstone half). The #204/#205-class
bugs all grew in that protocol, so it lives in one place with the invariant
stated once.

The committer also remembers what it DERIVED into child tables, which is part
of what lets a write plan tell ``SKIPPED`` from ``HEALED`` without re-reading
the store (#204, #314). It distinguishes the two kinds of child write on
purpose: re-stamping an unchanged child row at a newer generation, and
retiring the row it replaces, is bookkeeping; writing a row the plan actually
derived is derivation the receipt must report.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from hypergraph.materialization._schema import RECIPE_COLUMN, TableSpec
from hypergraph.materialization._table_store import RowPredicate, TableStore

Predicate = tuple[tuple[str, str, Any], ...]


def _as_predicate(where: Sequence[tuple[str, str, Any]] | None) -> RowPredicate | None:
    """Hand a plan-built predicate to the store under the store's own alias.

    Plans spell their operators as plain strings from the store's vocabulary;
    this is the one place that says so, instead of every call site carrying a
    Literal annotation it cannot express inline.
    """
    return None if where is None else cast(RowPredicate, list(where))


def dedup_rows(rows: list[dict[str, Any]], identity: str) -> list[dict[str, Any]]:
    """Keep only the highest write generation for each root identity."""
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity_value = str(row.get(identity, ""))
        existing = best.get(identity_value)
        if existing is None or row.get("_write_gen", 0) > existing.get("_write_gen", 0):
            best[identity_value] = row
    return list(best.values())


def dedup_child_rows(rows: list[dict[str, Any]], identity: str) -> list[dict[str, Any]]:
    """Keep only the highest write generation for each parent/child identity."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("_parent_id", "")), str(row.get(identity, "")))
        existing = best.get(key)
        if existing is None or row.get("_write_gen", 0) > existing.get("_write_gen", 0):
            best[key] = row
    return list(best.values())


@dataclass(frozen=True, slots=True)
class ChildWrites:
    """How many child rows a plan DERIVED, and how many of those errored.

    Snapshot it before a stretch of a write plan and subtract with ``since()``
    to learn what derivation that stretch did to child tables. Re-stamping an
    unchanged child row at a newer generation is deliberately not counted: it
    writes a row but derives nothing, so on its own it never turns a skip into
    a repair.
    """

    derived: int = 0
    errored: int = 0

    def since(self, before: ChildWrites) -> ChildWrites:
        return ChildWrites(self.derived - before.derived, self.errored - before.errored)


class ChildGenerations:
    """Per-mutation write generations for child tables.

    Child rows historically inherited the parent table's generation counter, but
    the two counters can diverge — a crash between the child write and the parent
    write leaves child rows one generation ahead, and ``ChildTable.set()`` bumps
    child generations independently. A child upsert that then reuses an existing
    physical generation survives cleanup (which deletes only OLDER generations)
    and the stale row can win the public dedup tie (#205).

    Every child-table mutation therefore allocates a generation strictly greater
    than every physical row currently in that table, never merely the parent's
    counter. Allocation is lazy (a mutation that never touches a child table
    never reads its max) and cached per table, so a mutation's writes and its
    cleanup agree on one generation.
    """

    def __init__(self, commit: TableCommitter, root_gen: int) -> None:
        self._commit = commit
        self._root_gen = root_gen
        self._allocated: dict[str, int] = {}

    def for_table(self, table_name: str) -> int:
        gen = self._allocated.get(table_name)
        if gen is None:
            gen = max(self._root_gen, self._commit.max_write_gen(table_name) + 1)
            self._allocated[table_name] = gen
        return gen


class TableCommitter:
    """Physical reads, writes, generations and tombstones for one HyperTable."""

    def __init__(self, store: TableStore, spec: TableSpec, identity: str) -> None:
        self._store = store
        self._spec = spec
        self._identity = identity
        self._derived_child_writes = 0
        self._child_errors_written = 0

    @property
    def store(self) -> TableStore:
        """The raw store, for the reads that are not row reads."""
        return self._store

    # -- reads ---------------------------------------------------------------

    def read_one(self, table: str, identity_column: str, identity_value: Any) -> dict[str, Any] | None:
        return self._store.read_one(table, identity_column, identity_value)

    def read_rows(
        self,
        table: str,
        where: Predicate | None = None,
        *,
        limit: int | None = None,
        columns: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        predicate = _as_predicate(where)
        projection = list(columns) if columns is not None else None
        if projection is None or not self._store.supports_column_projection():
            rows = self._store.read_rows(table, predicate, limit=limit)
            return self._store._project_rows(rows, projection)
        return self._store.read_rows(table, predicate, limit=limit, columns=projection)

    def column_names(self, table: str) -> Sequence[str]:
        return self._store.column_names(table)

    def max_write_gen(self, table: str) -> int:
        return self._store.max_write_gen(table)

    def next_write_gen(self, table: str | None = None) -> int:
        """The generation a mutation of ``table`` must write at."""
        return self._store.max_write_gen(table or self._spec.name) + 1

    def child_generations(self, root_gen: int) -> ChildGenerations:
        return ChildGenerations(self, root_gen)

    # -- writes --------------------------------------------------------------

    def evolve_schema(self, table: str, columns: Mapping[str, Any]) -> None:
        self._store.evolve_schema(table, dict(columns))

    def write_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self._store.write_rows(table, rows)

    def write_derived_child(self, table: str, row: dict[str, Any], *, errored: bool) -> None:
        """Write a child row this plan derived, and remember that it did.

        ``errored`` marks a child the plan tried and failed to derive: rows were
        written, but nothing was repaired, so the receipt must not claim a heal.
        """
        self.write_rows(table, [row])
        self._derived_child_writes += 1
        if errored:
            self._child_errors_written += 1

    def restamp_child(self, table: str, row: dict[str, Any]) -> None:
        """Re-write an unchanged child row at a newer generation."""
        self.write_rows(table, [row])

    def delete_rows(self, table: str, where: Sequence[tuple[str, str, Any]]) -> None:
        self._store.delete_rows(table, cast(RowPredicate, list(where)))

    # -- tombstones ----------------------------------------------------------

    def cleanup_parent(self, identity_value: Any, write_gen: int) -> None:
        self.delete_rows(
            self._spec.name,
            [(self._identity, "eq", identity_value), ("_write_gen", "lt", write_gen)],
        )

    def cleanup_children(self, identity_value: Any, child_gens: ChildGenerations) -> None:
        for child_spec in self._spec.children:
            self.delete_rows(
                child_spec.name,
                [("_parent_id", "eq", identity_value), ("_write_gen", "lt", child_gens.for_table(child_spec.name))],
            )

    def cleanup_child_row(self, child_spec: TableSpec, identity_value: Any, parent_id: Any, write_gen: int) -> None:
        self.delete_rows(
            child_spec.name,
            [
                (child_spec.identity, "eq", identity_value),
                ("_parent_id", "eq", parent_id),
                ("_write_gen", "lt", write_gen),
            ],
        )

    def refresh_missing_stamps(self, existing: dict[str, Any], rows: Any, provenance: Any) -> None:
        """Re-stamp a stored row (and its unchanged children) with the current recipe.

        Rows written before the table stamped recipes carry no stamp; giving
        them one must not disturb the public view, so every re-stamp goes
        through the same generation-then-tombstone protocol as a real write.
        A child row whose own fingerprint no longer matches is left alone — it
        is stale, and stamping it would claim a freshness it does not have.
        """
        identity_value = existing[self._identity]
        write_gen = self.next_write_gen()
        self.write_rows(self._spec.name, [rows.stamp_existing_row(self._spec.name, existing, write_gen)])
        self.cleanup_parent(identity_value, write_gen)
        for child_spec in self._spec.children:
            if child_spec.child_graph is None:
                continue
            child_gen = self.next_write_gen(child_spec.name)
            stored = self.read_rows(child_spec.name, (("_parent_id", "eq", identity_value),))
            for row in dedup_child_rows(stored, child_spec.identity):
                stamp = row.get(RECIPE_COLUMN)
                if isinstance(stamp, str) and stamp:
                    continue
                inputs = provenance.child_source_inputs(row, child_spec)
                if row.get("_row_fingerprint") != provenance.child_fingerprint(inputs, child_spec):
                    continue
                self.write_rows(child_spec.name, [rows.stamp_existing_row(child_spec.name, row, child_gen, child_spec)])
                self.cleanup_child_row(child_spec, row[child_spec.identity], identity_value, child_gen)

    def delete_row_and_children(self, identity_value: Any) -> None:
        """Remove one root row and every child row linked to it."""
        child_tables = {child_spec.name for child_spec in self._spec.children}
        if self._store.supports_manifests():
            from hypergraph.materialization._branch_registry import registered_child_tables

            child_tables.update(registered_child_tables(self._store, self._spec.name))
        for child_table in sorted(child_tables):
            self.delete_rows(child_table, [("_parent_id", "eq", identity_value)])
        self.delete_rows(self._spec.name, [(self._identity, "eq", identity_value)])

    # -- physical truth ------------------------------------------------------

    @property
    def child_writes(self) -> ChildWrites:
        """Running count of the child rows this committer has derived."""
        return ChildWrites(self._derived_child_writes, self._child_errors_written)
