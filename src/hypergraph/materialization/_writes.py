"""Pure write plans and row normalization for HyperTable."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from typing import Any, Literal

from hypergraph.materialization._provenance import (
    DerivedChildren,
    Provenance,
    RebuildChildren,
    ReconcileComplete,
    ReconcileResult,
    ReconcileUnavailable,
    normalize_value,
)
from hypergraph.materialization._schema import (
    RECIPE_COLUMN,
    TableSpec,
    input_names,
    is_internal_column,
)
from hypergraph.materialization._types import ErrorRow, SyncResult
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
    WriteOperation,
    WriteRows,
    _freeze,
    _Predicate,
)


def normalize_to_dict(item: Any) -> dict[str, Any]:
    """Convert a mapped child item to a plain dict if it is not one already."""
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="python")
    if hasattr(item, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(item)
    return dict(item)


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


class WritePlanner:
    """Emit immutable actions for every mutating HyperTable operation."""

    def __init__(
        self,
        graph: Any,
        spec: TableSpec,
        identity: str,
        components: Mapping[str, Any],
        on_error: Literal["raise", "store"],
        provenance: Provenance,
    ):
        self._graph = graph
        self._spec = spec
        self._identity = identity
        self._components = dict(components)
        self._on_error = on_error
        self._provenance = provenance

    def _graph_inputs(self, item: Mapping[str, Any]) -> dict[str, Any]:
        required = input_names(self._graph.inputs.required)
        return {key: value for key, value in item.items() if key != self._identity and key in required}

    @staticmethod
    def _parent_skipped(existing: dict[str, Any] | None, fingerprint: str) -> bool:
        if existing is None or existing.get("_row_fingerprint") != fingerprint:
            return False
        return existing.get("_status") in (None, "complete")

    @staticmethod
    def _can_reconcile(existing: dict[str, Any] | None) -> bool:
        return existing is not None and existing.get("_status") != "error"

    def _reconcile(
        self,
        item: dict[str, Any],
        existing: dict[str, Any],
        spec: TableSpec | None = None,
    ) -> Generator[WriteAction, Any, ReconcileResult | None]:
        target = spec or self._spec
        boundary_counts: dict[str, int] = {}
        for child_spec in target.children:
            rows = yield ReadRows(
                child_spec.name,
                (("_parent_id", "eq", item[target.identity]),),
            )
            boundary_counts[child_spec.name] = len(dedup_child_rows(rows, child_spec.identity))
        incoming = {key: value for key, value in item.items() if key != target.identity}
        state = self._provenance.start_reconcile(target, existing, incoming, boundary_counts)
        while True:
            state, step = self._provenance.next_reconcile_step(state)
            if isinstance(step, ReconcileUnavailable):
                return None
            if isinstance(step, ReconcileComplete):
                return step.result
            outputs = yield RunGraph(
                self._provenance.column_graph(step.node),
                _freeze(step.input_values()),
            )
            state = self._provenance.apply_reconcile_result(state, step, outputs)

    def _cleanup_parent(self, identity_value: Any, write_gen: int) -> DeleteRows:
        return DeleteRows(
            self._spec.name,
            ((self._identity, "eq", identity_value), ("_write_gen", "lt", write_gen)),
        )

    def _cleanup_children(self, identity_value: Any, write_gen: int) -> Generator[WriteAction, Any, None]:
        for child_spec in self._spec.children:
            yield DeleteRows(
                child_spec.name,
                (("_parent_id", "eq", identity_value), ("_write_gen", "lt", write_gen)),
            )

    def _refresh_missing_stamps(
        self,
        existing: dict[str, Any],
    ) -> Generator[WriteAction, Any, None]:
        identity_value = existing[self._identity]
        write_gen = (yield MaxWriteGen(self._spec.name)) + 1
        new_row = yield StampExistingRow(self._spec.name, _freeze(existing), write_gen)
        yield WriteRows.from_rows(self._spec.name, [new_row])
        yield self._cleanup_parent(identity_value, write_gen)
        for child_spec in self._spec.children:
            if child_spec.child_graph is None:
                continue
            child_gen = (yield MaxWriteGen(child_spec.name)) + 1
            rows = yield ReadRows(child_spec.name, (("_parent_id", "eq", identity_value),))
            for row in dedup_child_rows(rows, child_spec.identity):
                stamp = row.get(RECIPE_COLUMN)
                if isinstance(stamp, str) and stamp:
                    continue
                inputs = self._provenance.child_source_inputs(row, child_spec)
                if row.get("_row_fingerprint") != self._provenance.child_fingerprint(inputs, child_spec):
                    continue
                new_child = yield StampExistingRow(
                    child_spec.name,
                    _freeze(row),
                    child_gen,
                    child_spec,
                )
                yield WriteRows.from_rows(child_spec.name, [new_child])
                yield DeleteRows(
                    child_spec.name,
                    (
                        (child_spec.identity, "eq", row[child_spec.identity]),
                        ("_parent_id", "eq", identity_value),
                        ("_write_gen", "lt", child_gen),
                    ),
                )

    def _bind_child_components(self, child_graph: Any) -> Any:
        if not self._components:
            return child_graph
        valid_inputs = set(child_graph.inputs.all)
        bindings = {key: value for key, value in self._components.items() if key in valid_inputs}
        return child_graph.bind(**bindings) if bindings else child_graph

    @staticmethod
    def _child_items(outputs: Mapping[str, Any], child_spec: TableSpec) -> list[Any] | None:
        if not child_spec.child_graph:
            return None
        child_items = outputs.get(child_spec.map_input)
        if not child_items or not isinstance(child_items, list):
            return None
        return child_items

    def _child_provenances(self, child_spec: TableSpec, values: dict[str, Any]) -> dict[str, str]:
        provenances: dict[str, str] = {}
        for node in self._provenance.nodes_in_dependency_order(child_spec):
            provenance = self._provenance.node_provenance(node, values)
            for column in self._provenance.node_columns(node, child_spec):
                provenances[column.name] = provenance
        return provenances

    @staticmethod
    def _rebuild_child_items(rows: list[dict[str, Any]], child_spec: TableSpec) -> list[dict[str, Any]]:
        derived = {column.name for column in child_spec.columns if column.role == "derived"}
        return [
            {key: normalize_value(value) for key, value in row.items() if key not in derived and key != "_parent_id" and not is_internal_column(key)}
            for row in dedup_child_rows(rows, child_spec.identity)
        ]

    def _build_child_action(
        self,
        child_spec: TableSpec,
        child_item: dict[str, Any],
        child_identity: Any,
        parent_id: Any,
        fingerprint: str,
        write_gen: int,
        *,
        status: Literal["complete", "error"],
        error: str | None,
        outputs: Mapping[str, Any] | None = None,
        provenances: Mapping[str, Any] | None = None,
    ) -> BuildChildRow:
        return BuildChildRow(
            child_spec,
            _freeze(child_item),
            child_identity,
            parent_id,
            fingerprint,
            write_gen,
            status,
            error,
            _freeze(outputs or {}),
            _freeze(provenances or {}),
        )

    def _insert_children_items(
        self,
        parent_id: Any,
        child_items: list[Any],
        child_spec: TableSpec,
        write_gen: int,
    ) -> Generator[WriteAction, Any, None]:
        if child_spec.child_graph is None:
            return
        bound_graph = self._bind_child_components(child_spec.child_graph)
        for raw_item in child_items:
            child_item = normalize_to_dict(raw_item)
            child_identity = child_item.get(child_spec.identity, "")
            child_inputs = {
                column.name: child_item[column.name]
                for column in child_spec.columns
                if column.role == "source" and column.content_key and column.name in child_item
            }
            fingerprint = self._provenance.child_fingerprint(child_inputs, child_spec)
            existing_rows = yield ReadRows(
                child_spec.name,
                (
                    ("_parent_id", "eq", parent_id),
                    (child_spec.identity, "eq", child_identity),
                ),
            )
            existing = max(existing_rows, key=lambda row: row.get("_write_gen", 0)) if existing_rows else None
            if existing is not None and existing.get("_row_fingerprint") == fingerprint and existing.get("_status") in (None, "complete"):
                if self._provenance.row_missing_stamp(existing, RECIPE_COLUMN):
                    bumped = yield StampExistingRow(
                        child_spec.name,
                        _freeze(existing),
                        write_gen,
                        child_spec,
                        False,
                    )
                else:
                    bumped = dict(existing)
                    bumped["_write_gen"] = write_gen
                yield WriteRows.from_rows(child_spec.name, [bumped])
                continue

            row: dict[str, Any] | None = None
            if existing is not None and existing.get("_status") in (None, "complete"):
                try:
                    reconciled = yield from self._reconcile(child_item, existing, child_spec)
                except Exception as error:
                    if self._on_error == "raise":
                        raise
                    row = yield self._build_child_action(
                        child_spec,
                        child_item,
                        child_identity,
                        parent_id,
                        fingerprint,
                        write_gen,
                        status="error",
                        error=f"{type(error).__name__}: {error}",
                    )
                    yield WriteRows.from_rows(child_spec.name, [row])
                    continue
                if reconciled is not None:
                    row = yield self._build_child_action(
                        child_spec,
                        child_item,
                        child_identity,
                        parent_id,
                        fingerprint,
                        write_gen,
                        status="complete",
                        error=None,
                        outputs=reconciled.output_values(),
                        provenances=reconciled.provenance_values(),
                    )

            if row is None:
                try:
                    child_outputs = yield RunGraph(bound_graph, _freeze(child_inputs))
                except Exception as error:
                    if self._on_error == "raise":
                        raise
                    row = yield self._build_child_action(
                        child_spec,
                        child_item,
                        child_identity,
                        parent_id,
                        fingerprint,
                        write_gen,
                        status="error",
                        error=f"{type(error).__name__}: {error}",
                    )
                    yield WriteRows.from_rows(child_spec.name, [row])
                    continue
                row = yield self._build_child_action(
                    child_spec,
                    child_item,
                    child_identity,
                    parent_id,
                    fingerprint,
                    write_gen,
                    status="complete",
                    error=None,
                    outputs=child_outputs,
                    provenances=self._child_provenances(child_spec, {**child_item, **child_outputs}),
                )
            yield WriteRows.from_rows(child_spec.name, [row])

    def _insert_children(
        self,
        parent_id: Any,
        outputs: Mapping[str, Any],
        child_spec: TableSpec,
        write_gen: int,
    ) -> Generator[WriteAction, Any, None]:
        child_items = self._child_items(outputs, child_spec)
        if child_items is not None:
            yield from self._insert_children_items(parent_id, child_items, child_spec, write_gen)

    def _apply_reconciled(
        self,
        item: dict[str, Any],
        graph_inputs: dict[str, Any],
        existing: dict[str, Any],
        reconciled: ReconcileResult,
        parent_skipped: bool,
        write_gen: int,
    ) -> Generator[WriteAction, Any, str]:
        outputs = reconciled.output_values()
        provenances = reconciled.provenance_values()
        identity_value = item[self._identity]
        for selection in reconciled.children:
            if isinstance(selection, RebuildChildren):
                rows = yield ReadRows(selection.spec.name, (("_parent_id", "eq", identity_value),))
                child_items = self._rebuild_child_items(rows, selection.spec)
            elif isinstance(selection, DerivedChildren):
                child_items = list(selection.items)
            else:
                raise TypeError(f"unsupported child selection: {type(selection).__name__}")
            yield from self._insert_children_items(
                identity_value,
                child_items,
                selection.spec,
                write_gen,
            )
        provenance_changed = any(existing.get(f"_provenance_{name}") != provenance for name, provenance in provenances.items())
        rewrite_parent = not parent_skipped or provenance_changed or self._provenance.row_missing_stamp(existing, RECIPE_COLUMN)
        if rewrite_parent:
            yield EvolveMetadata(_freeze(item))
            row = yield BuildParentRow(
                _freeze(item),
                _freeze(graph_inputs),
                _freeze(outputs),
                write_gen,
                "complete",
                _freeze(provenances),
            )
            yield WriteRows.from_rows(self._spec.name, [row])
            yield self._cleanup_parent(identity_value, write_gen)
        yield from self._cleanup_children(identity_value, write_gen)
        return "skipped" if parent_skipped else "updated"

    def _error_parent(
        self,
        item: dict[str, Any],
        graph_inputs: dict[str, Any],
        write_gen: int,
        error: Exception,
        existing: dict[str, Any] | None,
    ) -> Generator[WriteAction, Any, None]:
        yield EvolveMetadata(_freeze(item))
        row = yield BuildParentRow(
            _freeze(item),
            _freeze(graph_inputs),
            (),
            write_gen,
            "error",
            error=f"{type(error).__name__}: {error}",
        )
        yield WriteRows.from_rows(self._spec.name, [row])
        if existing is not None:
            yield self._cleanup_parent(item[self._identity], write_gen)

    def _insert_one(
        self,
        item: dict[str, Any],
        write_gen: int,
    ) -> Generator[WriteAction, Any, str]:
        identity_value = item[self._identity]
        graph_inputs = self._graph_inputs(item)
        existing = yield ReadOne(self._spec.name, self._identity, identity_value)
        parent_skipped = self._parent_skipped(existing, self._provenance.root_fingerprint(graph_inputs))
        if parent_skipped and not self._spec.children:
            if self._provenance.row_missing_stamp(existing, RECIPE_COLUMN):
                yield from self._refresh_missing_stamps(existing)
            return "skipped"

        if self._can_reconcile(existing):
            try:
                reconciled = yield from self._reconcile(item, existing)
            except Exception as error:
                if self._on_error == "raise":
                    raise
                if parent_skipped:
                    return "skipped"
                yield from self._error_parent(item, graph_inputs, write_gen, error, existing)
                return "errored"
            if reconciled is not None:
                return (
                    yield from self._apply_reconciled(
                        item,
                        graph_inputs,
                        existing,
                        reconciled,
                        parent_skipped,
                        write_gen,
                    )
                )

        try:
            outputs = yield RunGraph(self._graph, _freeze(graph_inputs))
        except Exception as error:
            if self._on_error == "raise":
                raise
            if parent_skipped:
                return "skipped"
            yield from self._error_parent(item, graph_inputs, write_gen, error, existing)
            return "errored"

        if parent_skipped:
            for child_spec in self._spec.children:
                yield from self._insert_children(identity_value, outputs, child_spec, write_gen)
            yield from self._cleanup_children(identity_value, write_gen)
            return "skipped"

        for child_spec in self._spec.children:
            yield from self._insert_children(identity_value, outputs, child_spec, write_gen)
        yield EvolveMetadata(_freeze(item))
        row = yield BuildParentRow(
            _freeze(item),
            _freeze(graph_inputs),
            _freeze(outputs),
            write_gen,
            "complete",
        )
        yield WriteRows.from_rows(self._spec.name, [row])
        if existing is not None:
            yield self._cleanup_parent(identity_value, write_gen)
            yield from self._cleanup_children(identity_value, write_gen)
        return "updated" if existing is not None else "inserted"

    def insert(self, items: list[dict[str, Any]]) -> WriteOperation:
        write_gen = (yield MaxWriteGen(self._spec.name)) + 1
        for item in items:
            yield from self._insert_one(item, write_gen)

    def _prepare_update(
        self,
        identity_value: str,
        changes: dict[str, Any],
    ) -> Generator[WriteAction, Any, tuple[dict[str, Any], bool, int]]:
        existing = yield ReadOne(self._spec.name, self._identity, identity_value)
        if existing is None:
            raise KeyError(identity_value)
        item: dict[str, Any] = {self._identity: identity_value}
        for column in self._spec.columns:
            if column.role == "source" and column.name in existing:
                item[column.name] = normalize_value(existing[column.name])
        spec_columns = {column.name for column in self._spec.columns}
        for key, value in existing.items():
            if key not in spec_columns and not is_internal_column(key):
                item[key] = normalize_value(value)
        item.update(changes)
        source_names = {column.name for column in self._spec.columns if column.role == "source"}
        needs_rederive = any(key in source_names for key in changes)
        write_gen = (yield MaxWriteGen(self._spec.name)) + 1
        return item, needs_rederive, write_gen

    def update(self, identity_value: str, changes: dict[str, Any]) -> WriteOperation:
        item, needs_rederive, write_gen = yield from self._prepare_update(identity_value, changes)
        existing = yield ReadOne(self._spec.name, self._identity, identity_value)
        if not needs_rederive:
            yield EvolveMetadata(_freeze({self._identity: identity_value, **changes}))
            row = {key: normalize_value(value) for key, value in existing.items()}
            row.update(changes)
            row["_write_gen"] = write_gen
            yield WriteRows.from_rows(self._spec.name, [row])
            yield self._cleanup_parent(identity_value, write_gen)
            return

        graph_inputs = self._graph_inputs(item)
        if self._can_reconcile(existing):
            reconciled = yield from self._reconcile(item, existing)
            if reconciled is not None:
                yield from self._apply_reconciled(
                    item,
                    graph_inputs,
                    existing,
                    reconciled,
                    False,
                    write_gen,
                )
                return

        outputs = yield RunGraph(self._graph, _freeze(graph_inputs))
        yield EvolveMetadata(_freeze(item))
        row = yield BuildParentRow(
            _freeze(item),
            _freeze(graph_inputs),
            _freeze(outputs),
            write_gen,
            "update",
        )
        for child_spec in self._spec.children:
            yield from self._insert_children(identity_value, outputs, child_spec, write_gen)
        yield WriteRows.from_rows(self._spec.name, [row])
        yield from self._cleanup_children(identity_value, write_gen)
        yield self._cleanup_parent(identity_value, write_gen)

    def delete(self, identity_value: str) -> WriteOperation:
        existing = yield ReadOne(self._spec.name, self._identity, identity_value)
        if existing is None:
            return
        for child_spec in self._spec.children:
            yield DeleteRows(child_spec.name, (("_parent_id", "eq", identity_value),))
        yield DeleteRows(self._spec.name, ((self._identity, "eq", identity_value),))

    def _row_unchanged(self, item: dict[str, Any], existing: dict[str, Any]) -> bool:
        inputs = self._graph_inputs(item)
        return existing.get("_row_fingerprint") == self._provenance.root_fingerprint(inputs)

    def _error_row(self, identity_value: str) -> Generator[WriteAction, Any, ErrorRow | None]:
        row = yield ReadOne(self._spec.name, self._identity, identity_value)
        if not row:
            return None
        error = row.get("_error", "")
        return ErrorRow(
            identity={self._identity: identity_value},
            error_type=error.split(":")[0] if error else "Unknown",
            error_msg=error,
        )

    def sync(self, items: list[dict[str, Any]]) -> WriteOperation:
        rows = yield ReadRows(self._spec.name)
        existing_by_id = {str(row[self._identity]): row for row in dedup_rows(rows, self._identity) if row.get(self._identity) is not None}
        incoming_ids: set[str] = set()
        inserted = updated = skipped = errored = 0
        errors: list[ErrorRow] = []
        write_gen = (yield MaxWriteGen(self._spec.name)) + 1

        for item in items:
            identity_value = str(item[self._identity])
            incoming_ids.add(identity_value)
            existing = existing_by_id.get(identity_value)
            if existing is None:
                result = yield from self._insert_one(item, write_gen)
                if result == "errored":
                    errored += 1
                    error = yield from self._error_row(identity_value)
                    if error is not None:
                        errors.append(error)
                else:
                    inserted += 1
            elif self._row_unchanged(item, existing):
                if self._provenance.row_missing_stamp(existing, RECIPE_COLUMN):
                    yield from self._refresh_missing_stamps(existing)
                skipped += 1
            else:
                changes = {key: value for key, value in item.items() if key != self._identity}
                yield from self.update(identity_value, changes)
                updated += 1

        deleted = 0
        for identity_value in existing_by_id:
            if identity_value not in incoming_ids:
                yield from self.delete(identity_value)
                deleted += 1
        return SyncResult(inserted, updated, deleted, skipped, errored, tuple(errors))

    def set_rows(self, where: _Predicate, fields: dict[str, Any]) -> WriteOperation:
        content_keys = {column.name for column in self._spec.columns if column.content_key}
        blocked = sorted(content_keys.intersection(fields))
        if blocked:
            raise ValueError(f"set() cannot update content-key fields: {', '.join(blocked)}")
        rows = dedup_rows((yield ReadRows(self._spec.name, where)), self._identity)
        if not rows:
            return 0
        yield EvolveMetadata(_freeze({self._identity: rows[0][self._identity], **fields}))
        write_gen = (yield MaxWriteGen(self._spec.name)) + 1
        updated = []
        for row in rows:
            new_row = {key: normalize_value(value) for key, value in row.items()}
            new_row.update(fields)
            new_row["_write_gen"] = write_gen
            updated.append(new_row)
        yield WriteRows.from_rows(self._spec.name, updated)
        for row in rows:
            yield DeleteRows(
                self._spec.name,
                ((self._identity, "eq", row[self._identity]), ("_write_gen", "lt", write_gen)),
            )
        return len(updated)

    def set_children(self, where: _Predicate, fields: dict[str, Any]) -> WriteOperation:
        if not self._spec.children:
            return 0
        child_spec = self._spec.children[0]
        rows = dedup_child_rows(
            (yield ReadRows(child_spec.name, where)),
            child_spec.identity,
        )
        if not rows:
            return 0
        yield EvolveMetadata(
            _freeze({child_spec.identity: rows[0][child_spec.identity], **fields}),
            child_spec.name,
            child_spec.identity,
        )
        write_gen = (yield MaxWriteGen(child_spec.name)) + 1
        updated = []
        for row in rows:
            new_row = {key: normalize_value(value) for key, value in row.items()}
            new_row.update(fields)
            new_row["_write_gen"] = write_gen
            updated.append(new_row)
        yield WriteRows.from_rows(child_spec.name, updated)
        for row in rows:
            yield DeleteRows(
                child_spec.name,
                (
                    (child_spec.identity, "eq", row[child_spec.identity]),
                    ("_parent_id", "eq", row["_parent_id"]),
                    ("_write_gen", "lt", write_gen),
                ),
            )
        return len(updated)

    def derive_column(self, column: str, *, backfill: bool) -> WriteOperation:
        if backfill:
            yield EvolveBackfillColumn(column)
        node = self._provenance.producing_node(column)
        write_gen = (yield MaxWriteGen(self._spec.name)) + 1
        rows = dedup_rows((yield ReadRows(self._spec.name)), self._identity)
        for existing in rows:
            if backfill and not self._provenance.column_is_null(existing.get(column)):
                continue
            values = self._provenance.stored_values(existing)
            outputs = yield RunGraph(
                self._provenance.column_graph(node),
                _freeze(self._provenance.node_inputs(node, values)),
            )
            new_row = yield BuildNodeRow(_freeze(existing), node, _freeze(outputs), write_gen)
            yield WriteRows.from_rows(self._spec.name, [new_row])
            yield DeleteRows(
                self._spec.name,
                (
                    (self._identity, "eq", existing[self._identity]),
                    ("_write_gen", "lt", write_gen),
                ),
            )
