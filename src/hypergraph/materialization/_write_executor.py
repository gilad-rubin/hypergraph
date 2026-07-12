"""Physical apply owner for HyperTable write actions."""

from __future__ import annotations

from typing import Any

from hypergraph.materialization._provenance import Provenance, normalize_value
from hypergraph.materialization._recipe_journal import RecipeJournal
from hypergraph.materialization._schema import (
    RECIPE_COLUMN,
    TableSpec,
    python_type_to_arrow,
    return_type,
)
from hypergraph.materialization._write_actions import (
    BuildChildRow,
    BuildNodeRow,
    BuildParentRow,
    DeleteRows,
    EvolveBackfillColumn,
    EvolveMetadata,
    MaxWriteGen,
    ReadOne,
    ReadRows,
    RunGraph,
    StampExistingRow,
    WriteAction,
    WriteRows,
    _thaw,
    _thaw_rows,
)


class WriteExecutor:
    """Apply store, schema, recipe-journal, and row-assembly actions once."""

    def __init__(self, store: Any, spec: TableSpec, identity: str, provenance: Provenance):
        self._store = store
        self._spec = spec
        self._identity = identity
        self._provenance = provenance
        self._recipe_column_ready: set[str] = set()
        self._journal_obj: RecipeJournal | None = None

    @property
    def journal(self) -> RecipeJournal:
        if self._journal_obj is None:
            self._journal_obj = RecipeJournal(self._store)
        return self._journal_obj

    def apply(self, action: WriteAction) -> Any:
        """Apply one non-runner action; runner actions are owned by the color driver."""
        if isinstance(action, RunGraph):
            raise TypeError("RunGraph must be executed by the sync/async color driver")
        if isinstance(action, ReadOne):
            return self._store.read_one(action.table, action.identity, action.value)
        if isinstance(action, ReadRows):
            where = list(action.where) if action.where is not None else None
            columns = list(action.columns) if action.columns is not None else None
            if columns is None or not self._store.supports_column_projection():
                rows = self._store.read_rows(action.table, where, limit=action.limit)
                return self._store._project_rows(rows, columns)
            return self._store.read_rows(
                action.table,
                where,
                limit=action.limit,
                columns=columns,
            )
        if isinstance(action, MaxWriteGen):
            return self._store.max_write_gen(action.table)
        if isinstance(action, WriteRows):
            return self._store.write_rows(action.table, _thaw_rows(action.rows))
        if isinstance(action, DeleteRows):
            return self._store.delete_rows(action.table, list(action.where))
        if isinstance(action, EvolveMetadata):
            return self._evolve_for_metadata(_thaw(action.item), table_name=action.table, identity=action.identity)
        if isinstance(action, BuildParentRow):
            return self._build_parent_row(action)
        if isinstance(action, BuildChildRow):
            return self._build_child_row(action)
        if isinstance(action, StampExistingRow):
            return self._stamp_existing_row(action)
        if isinstance(action, BuildNodeRow):
            return self._build_node_row(action)
        if isinstance(action, EvolveBackfillColumn):
            return self._evolve_for_backfill_column(action.column)
        raise TypeError(f"unsupported write action: {type(action).__name__}")

    def _evolve_for_metadata(
        self,
        item: dict[str, Any],
        *,
        table_name: str | None = None,
        identity: str | None = None,
    ) -> None:
        target = table_name or self._spec.name
        identity_column = identity or self._identity
        known_columns = set(self._store.column_names(target))
        if not known_columns:
            sample = self._store.read_rows(target, limit=1)
            known_columns = set(sample[0]) if sample else {column.name for column in self._spec.columns}
        new_metadata = {
            key: python_type_to_arrow(type(value) if value is not None else str)
            for key, value in item.items()
            if key not in known_columns and key != identity_column
        }
        if new_metadata:
            self._store.evolve_schema(target, new_metadata)

    def _ensure_recipe_column(self, table_name: str) -> None:
        if table_name in self._recipe_column_ready:
            return
        physical = self._store.column_names(table_name)
        if physical and RECIPE_COLUMN not in physical:
            self._store.evolve_schema(table_name, {RECIPE_COLUMN: python_type_to_arrow(str)})
        self._recipe_column_ready.add(table_name)

    def _stamp_recipe(self, row: dict[str, Any], table_name: str, child_spec: TableSpec | None = None) -> None:
        if not self._provenance.table_stamps_recipe():
            return
        if child_spec is not None:
            if child_spec.child_graph is None:
                return
            fingerprint = self._provenance.current_child_recipe_fingerprint(child_spec)
        else:
            fingerprint = self._provenance.current_recipe_fingerprint()
        self._ensure_recipe_column(table_name)
        row[RECIPE_COLUMN] = fingerprint

    def _record_node_recipe(self, node: Any) -> str:
        entries = self._provenance.recipe_entries(node)
        for entry in entries:
            self.journal.record(entry.hash, entry.kind, entry.payload)
        return entries[0].hash

    def _provenance_node(self, name: str) -> Any:
        for column in self._spec.columns:
            if column.role == "derived" and column.name == name:
                return column.produced_by
        for child_spec in self._spec.children:
            if child_spec.map_input == name:
                return self._provenance.boundary_node(child_spec)
        return None

    def _build_parent_row(self, action: BuildParentRow) -> dict[str, Any]:
        item = _thaw(action.item)
        graph_inputs = _thaw(action.graph_inputs)
        outputs = _thaw(action.outputs)
        identity_value = item[self._identity]
        row: dict[str, Any] = {self._identity: identity_value}
        row.update({key: value for key, value in item.items() if key != self._identity})

        derived_columns = self._provenance.derived_columns()
        if action.mode == "error":
            for column in derived_columns:
                row[column.name] = None
        else:
            for column in derived_columns:
                if column.name in outputs:
                    row[column.name] = outputs[column.name]

        row["_row_fingerprint"] = self._provenance.root_fingerprint(graph_inputs)
        row["_write_gen"] = action.write_gen
        self._stamp_recipe(row, self._spec.name)

        if action.mode != "error":
            provenances = _thaw(action.provenances) if action.provenances is not None else None
            if provenances is None:
                values = {**{key: value for key, value in item.items() if key != self._identity}, **outputs}
                provenances = {column.name: self._provenance.node_provenance(column.produced_by, values) for column in derived_columns}
                for child_spec in self._spec.children:
                    boundary = self._provenance.boundary_node(child_spec)
                    if boundary is None:
                        continue
                    provenance = self._provenance.node_provenance(boundary, values)
                    if provenance is not None:
                        provenances[child_spec.map_input] = self._provenance.boundary_provenance_value(
                            provenance,
                            outputs.get(child_spec.map_input),
                        )
            for name, provenance in provenances.items():
                row[f"_provenance_{name}"] = provenance
                node = self._provenance_node(name)
                if node is not None:
                    self._record_node_recipe(node)

        if action.mode == "complete":
            row["_status"] = "complete"
            row["_error"] = None
        elif action.mode == "error":
            row["_status"] = "error"
            row["_error"] = action.error
        return row

    def _build_child_row(self, action: BuildChildRow) -> dict[str, Any]:
        item = _thaw(action.item)
        row = {
            action.spec.identity: action.identity,
            "_parent_id": action.parent_id,
            "_write_gen": action.write_gen,
            "_row_fingerprint": action.fingerprint,
            "_status": action.status,
            "_error": action.error,
        }
        self._stamp_recipe(row, action.spec.name, action.spec)
        for key, value in item.items():
            if key != action.spec.identity and key != "_parent_id":
                row[key] = value
        row.update(_thaw(action.outputs))
        for name, provenance in _thaw(action.provenances).items():
            row[f"_provenance_{name}"] = provenance
            for column in action.spec.columns:
                if column.role == "derived" and column.name == name:
                    self._record_node_recipe(column.produced_by)
                    break
        return row

    def _stamp_existing_row(self, action: StampExistingRow) -> dict[str, Any]:
        existing = _thaw(action.row)
        row = {key: normalize_value(value) for key, value in existing.items()} if action.normalize_values else dict(existing)
        self._stamp_recipe(row, action.table, action.child_spec)
        row["_write_gen"] = action.write_gen
        return row

    def _build_node_row(self, action: BuildNodeRow) -> dict[str, Any]:
        existing = _thaw(action.existing)
        outputs = _thaw(action.outputs)
        new_row = {key: normalize_value(value) for key, value in existing.items()}
        for column in self._provenance.node_columns(action.node):
            if column.name in outputs:
                new_row[column.name] = outputs[column.name]
        provenance = self._provenance.node_provenance(action.node, self._provenance.stored_values(new_row))
        for column in self._provenance.node_columns(action.node):
            new_row[f"_provenance_{column.name}"] = provenance
        self._record_node_recipe(action.node)
        new_row["_write_gen"] = action.write_gen
        if self._provenance.row_converged(new_row):
            new_row["_row_fingerprint"] = self._provenance.root_fingerprint(self._provenance.source_inputs(new_row))
            self._stamp_recipe(new_row, self._spec.name)
        return new_row

    def _evolve_for_backfill_column(self, column: str) -> None:
        sample = self._store.read_rows(self._spec.name, limit=1)
        if sample and column not in sample[0]:
            column_type = str
            for spec_column in self._spec.columns:
                if spec_column.name == column and spec_column.role == "derived" and spec_column.produced_by:
                    column_type = return_type(spec_column.produced_by)
                    break
            self._store.evolve_schema(
                self._spec.name,
                {
                    column: python_type_to_arrow(column_type),
                    f"_provenance_{column}": python_type_to_arrow(str),
                },
            )
