"""Turning derived values into the physical rows a HyperTable stores.

Row shape was the largest of the five responsibilities tangled in
``WritePlanner``: which columns a mode nulls, where a provenance stamp goes,
when the recipe journal gains an entry, and which schema evolution has to
happen before a row can be written at all. ``RowBuilder`` owns exactly that —
it never decides *whether* to write a row, only what the row looks like — so a
write plan reads as orchestration and a row-shape question has one place to be
answered.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hypergraph.materialization._commit import TableCommitter, dedup_child_rows
from hypergraph.materialization._provenance import Provenance, normalize_value
from hypergraph.materialization._recipe_journal import RecipeJournal
from hypergraph.materialization._schema import (
    CHANGES_COLUMN,
    QUESTION_COLUMN,
    RECIPE_COLUMN,
    TableSpec,
    dead_bound_names,
    is_internal_column,
    python_type_to_arrow,
    return_type,
)
from hypergraph.materialization._types import (
    ColumnChange,
    RowStatus,
    serialize_changes,
    serialize_question,
)
from hypergraph.runners import PauseInfo


class RowBuilder:
    """Build the stored representation of one parent or child row."""

    def __init__(
        self,
        commit: TableCommitter,
        spec: TableSpec,
        identity: str,
        provenance: Provenance,
    ) -> None:
        self._commit = commit
        self._spec = spec
        self._identity = identity
        self._provenance = provenance
        self._journal = RecipeJournal(commit.store)
        self._recipe_column_ready: set[str] = set()
        self._changes_column_ready: set[str] = set()

    @property
    def journal(self) -> RecipeJournal:
        return self._journal

    # -- schema evolution ----------------------------------------------------

    def evolve_for_metadata(
        self,
        item: Mapping[str, Any],
        *,
        table_name: str | None = None,
        identity: str | None = None,
    ) -> None:
        """Add a column for every unknown key in ``item`` before it is written."""
        target = table_name or self._spec.name
        identity_column = identity or self._identity
        known_columns = set(self._commit.column_names(target))
        if not known_columns:
            sample = self._commit.read_rows(target, limit=1)
            known_columns = set(sample[0]) if sample else {column.name for column in self._spec.columns}
        new_metadata = {
            key: python_type_to_arrow(type(value) if value is not None else str)
            for key, value in item.items()
            if key not in known_columns and key != identity_column
        }
        if new_metadata:
            self._commit.evolve_schema(target, new_metadata)

    def evolve_for_backfill_column(self, column: str) -> None:
        """Add a derived column and its provenance column for a backfill."""
        sample = self._commit.read_rows(self._spec.name, limit=1)
        if sample and column not in sample[0]:
            column_type = str
            for spec_column in self._spec.columns:
                if spec_column.name == column and spec_column.role == "derived" and spec_column.produced_by:
                    column_type = return_type(spec_column.produced_by)
                    break
            self._commit.evolve_schema(
                self._spec.name,
                {
                    column: python_type_to_arrow(column_type),
                    f"_provenance_{column}": python_type_to_arrow(str),
                },
            )

    def _ensure_column(self, table_name: str, column: str, ready: set[str]) -> None:
        if table_name in ready:
            return
        physical = self._commit.column_names(table_name)
        if physical and column not in physical:
            self._commit.evolve_schema(table_name, {column: python_type_to_arrow(str)})
        ready.add(table_name)

    # -- recipe stamps -------------------------------------------------------

    def _stamp_recipe(self, row: dict[str, Any], table_name: str, child_spec: TableSpec | None = None) -> None:
        if not self._provenance.table_stamps_recipe():
            return
        if child_spec is not None:
            if child_spec.child_graph is None:
                return
            fingerprint = self._provenance.current_child_recipe_fingerprint(child_spec)
        else:
            fingerprint = self._provenance.current_recipe_fingerprint()
        self._ensure_column(table_name, RECIPE_COLUMN, self._recipe_column_ready)
        row[RECIPE_COLUMN] = fingerprint

    def record_node_recipe(self, node: Any) -> str:
        entries = self._provenance.recipe_entries(node)
        for entry in entries:
            self._journal.record(entry.hash, entry.kind, entry.payload)
        return entries[0].hash

    def _provenance_nodes(self, name: str) -> tuple[Any, ...]:
        for column in self._spec.columns:
            if column.role in ("derived", "answer") and column.name == name:
                return self._provenance.column_producers(column)
        for child_spec in self._spec.children:
            if child_spec.map_input == name:
                boundary = self._provenance.boundary_node(child_spec)
                return (boundary,) if boundary is not None else ()
        return ()

    def stamp_existing_row(
        self,
        table: str,
        existing: Mapping[str, Any],
        write_gen: int,
        child_spec: TableSpec | None = None,
        *,
        normalize_values: bool = True,
    ) -> dict[str, Any]:
        """Re-stamp a stored row with the current recipe at a new generation."""
        row = {key: normalize_value(value) for key, value in existing.items()} if normalize_values else dict(existing)
        self._stamp_recipe(row, table, child_spec)
        row["_write_gen"] = write_gen
        return row

    # -- rows ----------------------------------------------------------------

    def parent_row(
        self,
        item: Mapping[str, Any],
        source_inputs: Mapping[str, Any],
        outputs: Mapping[str, Any],
        write_gen: int,
        status: RowStatus,
        *,
        provenances: Mapping[str, str] | None = None,
        error: str | None = None,
        pause: PauseInfo | None = None,
        pause_provenance: str | None = None,
        changes: tuple[ColumnChange, ...] = (),
    ) -> dict[str, Any]:
        row: dict[str, Any] = {self._identity: item[self._identity]}
        row.update({key: value for key, value in item.items() if key != self._identity})
        derived_columns = self._provenance.derived_columns()
        if status is RowStatus.ERROR:
            for column in derived_columns:
                row[column.name] = None
        else:
            for column in derived_columns:
                if column.name in outputs:
                    row[column.name] = outputs[column.name]
                elif status is RowStatus.PARTIAL or (status is RowStatus.WAITING and column.role == "answer"):
                    row[column.name] = None
        row["_row_fingerprint"] = self._provenance.root_fingerprint(source_inputs)
        row["_write_gen"] = write_gen
        self._stamp_recipe(row, self._spec.name)

        if status is not RowStatus.ERROR:
            if provenances is None and status is RowStatus.PARTIAL:
                raise RuntimeError("partial row requires the provenances of the columns that survived")
            stamps: Mapping[str, str | None]
            if provenances is None:
                values = {**{key: value for key, value in item.items() if key != self._identity}, **outputs}
                computed: dict[str, str | None] = {
                    column.name: self._provenance.node_provenance(self._provenance.column_producers(column)[0], values) for column in derived_columns
                }
                for child_spec in self._spec.children:
                    boundary = self._provenance.boundary_node(child_spec)
                    if boundary is None or child_spec.map_input is None:
                        continue
                    provenance = self._provenance.node_provenance(boundary, values)
                    if provenance is not None:
                        computed[child_spec.map_input] = self._provenance.boundary_provenance_value(
                            provenance,
                            outputs.get(child_spec.map_input),
                            child_spec.identity,
                        )
                stamps = computed
            else:
                stamps = provenances
            for name, provenance in stamps.items():
                row[f"_provenance_{name}"] = provenance
                for node in self._provenance_nodes(name):
                    self.record_node_recipe(node)

        row["_status"] = status.stored_value
        row["_error"] = error if status in (RowStatus.ERROR, RowStatus.PARTIAL) else None
        if status is RowStatus.WAITING:
            if pause is None or pause_provenance is None:
                raise RuntimeError("waiting row requires a pause and provenance")
            row[QUESTION_COLUMN] = serialize_question(pause, pause_provenance)
        else:
            row[QUESTION_COLUMN] = None
        if status is RowStatus.PARTIAL:
            self._ensure_column(self._spec.name, CHANGES_COLUMN, self._changes_column_ready)
            row[CHANGES_COLUMN] = serialize_changes(changes)
        return row

    def child_row(
        self,
        spec: TableSpec,
        item: Mapping[str, Any],
        identity: Any,
        parent_id: Any,
        fingerprint: str,
        write_gen: int,
        *,
        status: RowStatus,
        error: str | None,
        outputs: Mapping[str, Any] | None = None,
        provenances: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            spec.identity: identity,
            "_parent_id": parent_id,
            "_write_gen": write_gen,
            "_row_fingerprint": fingerprint,
            "_status": status.stored_value,
            "_error": error,
            QUESTION_COLUMN: None,
        }
        self._stamp_recipe(row, spec.name, spec)
        row.update({key: value for key, value in item.items() if key not in (spec.identity, "_parent_id")})
        row.update(outputs or {})
        for name, provenance in (provenances or {}).items():
            row[f"_provenance_{name}"] = provenance
            for column in spec.columns:
                if column.role == "derived" and column.name == name:
                    for node in self._provenance.column_producers(column):
                        self.record_node_recipe(node)
                    break
        return row

    @staticmethod
    def rebuild_child_items(rows: list[dict[str, Any]], child_spec: TableSpec) -> list[dict[str, Any]]:
        """Recover the source items of stored child rows, dropping derived columns.

        A name the child graph binds is recipe, so a legacy store's dead column
        for it must never re-enter the item: it would reach ``child_inputs`` and
        move the child fingerprint, making the same logical row hash differently
        depending on whether it came from a fresh fan-out or a rebuild. Only the
        NULL residue drops — a declared column or a populated user annotation of
        the same name is real data and survives the rebuild as before.
        """
        derived = {column.name for column in child_spec.columns if column.role == "derived"}
        dead = dead_bound_names(child_spec)
        return [
            {
                key: normalize_value(value)
                for key, value in row.items()
                if key not in derived and not (key in dead and value is None) and key != "_parent_id" and not is_internal_column(key)
            }
            for row in dedup_child_rows(rows, child_spec.identity)
        ]

    # -- provenance stamps ---------------------------------------------------

    def provenances_for_values(
        self,
        values: Mapping[str, Any],
        pause: PauseInfo | None = None,
        executed: set[str] | None = None,
        spec: TableSpec | None = None,
    ) -> dict[str, str]:
        provenances: dict[str, str] = {}
        for node in self._provenance.nodes_in_dependency_order(spec):
            if executed is not None and node.name not in executed:
                continue
            provenance = self._provenance.node_provenance(node, values)
            if provenance is None:
                continue
            for column in self._provenance.node_columns(node, spec):
                if column.name in values or (pause is not None and column.name == pause.response_key):
                    provenances[column.name] = provenance
        return provenances

    def child_provenances(self, child_spec: TableSpec, values: dict[str, Any]) -> dict[str, str | None]:
        provenances: dict[str, str | None] = {}
        for node in self._provenance.nodes_in_dependency_order(child_spec):
            provenance = self._provenance.node_provenance(node, values)
            for column in self._provenance.node_columns(node, child_spec):
                provenances[column.name] = provenance
        return provenances
