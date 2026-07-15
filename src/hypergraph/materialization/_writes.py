"""Pure write plans and row normalization for HyperTable."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from dataclasses import dataclass
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
from hypergraph.materialization._recipe_journal import RecipeJournal
from hypergraph.materialization._schema import (
    QUESTION_COLUMN,
    RECIPE_COLUMN,
    TableSpec,
    input_names,
    is_internal_column,
    python_type_to_arrow,
    return_type,
)
from hypergraph.materialization._types import (
    RowReceipt,
    RowStatus,
    TableReceipt,
    WriteOutcome,
    deserialize_question,
    serialize_question,
)
from hypergraph.materialization._write_actions import RunGraph, WriteOperation, _Predicate
from hypergraph.runners import PauseInfo


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


@dataclass(frozen=True)
class _PausedConvergence:
    pause: PauseInfo
    outputs: dict[str, Any]
    provenances: dict[str, str]
    provenance: str


def _run_values(result: Any) -> dict[str, Any]:
    if hasattr(result, "values") and isinstance(result.values, dict):
        return result.values
    if isinstance(result, dict):
        return result
    return {}


def _run_pause(result: Any) -> PauseInfo | None:
    if getattr(result, "paused", False):
        return getattr(result, "pause", None)
    return None


class WritePlanner:
    """Emit immutable actions for every mutating HyperTable operation."""

    def __init__(
        self,
        graph: Any,
        store: Any,
        spec: TableSpec,
        identity: str,
        components: Mapping[str, Any],
        on_error: Literal["raise", "store"],
        provenance: Provenance,
    ):
        self._graph = graph
        self._store = store
        self._spec = spec
        self._identity = identity
        self._components = dict(components)
        self._on_error = on_error
        self._provenance = provenance
        self._recipe_column_ready: set[str] = set()
        self._journal = RecipeJournal(store)

    @property
    def journal(self) -> RecipeJournal:
        return self._journal

    def _read_rows(
        self,
        table: str,
        where: tuple[tuple[str, str, Any], ...] | None = None,
        *,
        limit: int | None = None,
        columns: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        predicate = list(where) if where is not None else None
        projection = list(columns) if columns is not None else None
        if projection is None or not self._store.supports_column_projection():
            rows = self._store.read_rows(table, predicate, limit=limit)
            return self._store._project_rows(rows, projection)
        return self._store.read_rows(table, predicate, limit=limit, columns=projection)

    def _evolve_for_metadata(
        self,
        item: Mapping[str, Any],
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
            self._journal.record(entry.hash, entry.kind, entry.payload)
        return entries[0].hash

    def _provenance_node(self, name: str) -> Any:
        for column in self._spec.columns:
            if column.role in ("derived", "answer") and column.name == name:
                producer = column.produced_by
                return producer[0] if isinstance(producer, tuple) else producer
        for child_spec in self._spec.children:
            if child_spec.map_input == name:
                return self._provenance.boundary_node(child_spec)
        return None

    def _build_parent_row(
        self,
        item: Mapping[str, Any],
        source_inputs: Mapping[str, Any],
        outputs: Mapping[str, Any],
        write_gen: int,
        mode: Literal["complete", "waiting", "error"],
        *,
        provenances: Mapping[str, str] | None = None,
        error: str | None = None,
        pause: PauseInfo | None = None,
        pause_provenance: str | None = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {self._identity: item[self._identity]}
        row.update({key: value for key, value in item.items() if key != self._identity})
        derived_columns = self._provenance.derived_columns()
        if mode == "error":
            for column in derived_columns:
                row[column.name] = None
        else:
            for column in derived_columns:
                if column.name in outputs:
                    row[column.name] = outputs[column.name]
                elif mode == "waiting" and column.role == "answer":
                    row[column.name] = None
        row["_row_fingerprint"] = self._provenance.root_fingerprint(source_inputs)
        row["_write_gen"] = write_gen
        self._stamp_recipe(row, self._spec.name)

        if mode != "error":
            if provenances is None:
                values = {**{key: value for key, value in item.items() if key != self._identity}, **outputs}
                provenances = {
                    column.name: self._provenance.node_provenance(self._provenance.column_producers(column)[0], values) for column in derived_columns
                }
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

        row["_status"] = mode
        row["_error"] = error if mode == "error" else None
        if mode == "waiting":
            if pause is None or pause_provenance is None:
                raise RuntimeError("waiting row requires a pause and provenance")
            row[QUESTION_COLUMN] = serialize_question(pause, pause_provenance)
        else:
            row[QUESTION_COLUMN] = None
        return row

    def _build_child_row(
        self,
        spec: TableSpec,
        item: Mapping[str, Any],
        identity: Any,
        parent_id: Any,
        fingerprint: str,
        write_gen: int,
        *,
        status: Literal["complete", "error"],
        error: str | None,
        outputs: Mapping[str, Any] | None = None,
        provenances: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            spec.identity: identity,
            "_parent_id": parent_id,
            "_write_gen": write_gen,
            "_row_fingerprint": fingerprint,
            "_status": status,
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
                    self._record_node_recipe(column.produced_by)
                    break
        return row

    def _stamp_existing_row(
        self,
        table: str,
        existing: Mapping[str, Any],
        write_gen: int,
        child_spec: TableSpec | None = None,
        *,
        normalize_values: bool = True,
    ) -> dict[str, Any]:
        row = {key: normalize_value(value) for key, value in existing.items()} if normalize_values else dict(existing)
        self._stamp_recipe(row, table, child_spec)
        row["_write_gen"] = write_gen
        return row

    def _build_node_row(
        self,
        existing: Mapping[str, Any],
        node: Any,
        outputs: Mapping[str, Any],
        write_gen: int,
    ) -> dict[str, Any]:
        row = {key: normalize_value(value) for key, value in existing.items()}
        for column in self._provenance.node_columns(node):
            if column.name in outputs:
                row[column.name] = outputs[column.name]
        provenance = self._provenance.node_provenance(node, self._provenance.stored_values(row))
        for column in self._provenance.node_columns(node):
            row[f"_provenance_{column.name}"] = provenance
        self._record_node_recipe(node)
        row["_write_gen"] = write_gen
        if self._provenance.row_converged(row):
            row["_row_fingerprint"] = self._provenance.root_fingerprint(self._provenance.source_inputs(row))
            self._stamp_recipe(row, self._spec.name)
        return row

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

    def _graph_inputs(self, item: Mapping[str, Any], provided: set[str] | None = None) -> dict[str, Any]:
        required = input_names(self._graph.inputs.required)
        answers = {column.name for column in self._spec.columns if column.role == "answer"}
        accepted_answers = answers if provided is None else answers & provided
        accepted = required | accepted_answers
        return {key: value for key, value in item.items() if key != self._identity and key in accepted}

    def _source_inputs(self, item: Mapping[str, Any]) -> dict[str, Any]:
        sources = {column.name for column in self._spec.columns if column.role == "source"}
        return {key: value for key, value in item.items() if key in sources}

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
        provided: set[str] | None = None,
    ) -> Generator[RunGraph, Any, ReconcileResult | _PausedConvergence | None]:
        target = spec or self._spec
        boundary_counts: dict[str, int] = {}
        for child_spec in target.children:
            rows = self._read_rows(
                child_spec.name,
                (("_parent_id", "eq", item[target.identity]),),
            )
            boundary_counts[child_spec.name] = len(dedup_child_rows(rows, child_spec.identity))
        provided_names = provided if provided is not None else set(item) - {target.identity}
        incoming = {key: value for key, value in item.items() if key in provided_names and key != target.identity}
        state = self._provenance.start_reconcile(target, existing, incoming, boundary_counts)
        while True:
            state, step = self._provenance.next_reconcile_step(state)
            if isinstance(step, ReconcileUnavailable):
                return None
            if isinstance(step, ReconcileComplete):
                return step.result
            result = yield RunGraph(
                self._provenance.column_graph(step.node),
                step.input_values(),
            )
            pause = _run_pause(result)
            if pause is not None:
                provenances = dict(state.provenances)
                provenances[pause.response_key] = step.provenance
                return _PausedConvergence(
                    pause=pause,
                    outputs=dict(state.outputs),
                    provenances=provenances,
                    provenance=step.provenance,
                )
            outputs = _run_values(result)
            state = self._provenance.apply_reconcile_result(state, step, outputs)

    def _cleanup_parent(self, identity_value: Any, write_gen: int) -> None:
        self._store.delete_rows(
            self._spec.name,
            [(self._identity, "eq", identity_value), ("_write_gen", "lt", write_gen)],
        )

    def _cleanup_children(self, identity_value: Any, write_gen: int) -> None:
        for child_spec in self._spec.children:
            self._store.delete_rows(
                child_spec.name,
                [("_parent_id", "eq", identity_value), ("_write_gen", "lt", write_gen)],
            )

    def _refresh_missing_stamps(
        self,
        existing: dict[str, Any],
    ) -> None:
        identity_value = existing[self._identity]
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        new_row = self._stamp_existing_row(self._spec.name, existing, write_gen)
        self._store.write_rows(self._spec.name, [new_row])
        self._cleanup_parent(identity_value, write_gen)
        for child_spec in self._spec.children:
            if child_spec.child_graph is None:
                continue
            child_gen = self._store.max_write_gen(child_spec.name) + 1
            rows = self._read_rows(child_spec.name, (("_parent_id", "eq", identity_value),))
            for row in dedup_child_rows(rows, child_spec.identity):
                stamp = row.get(RECIPE_COLUMN)
                if isinstance(stamp, str) and stamp:
                    continue
                inputs = self._provenance.child_source_inputs(row, child_spec)
                if row.get("_row_fingerprint") != self._provenance.child_fingerprint(inputs, child_spec):
                    continue
                new_child = self._stamp_existing_row(
                    child_spec.name,
                    row,
                    child_gen,
                    child_spec,
                )
                self._store.write_rows(child_spec.name, [new_child])
                self._store.delete_rows(
                    child_spec.name,
                    [
                        (child_spec.identity, "eq", row[child_spec.identity]),
                        ("_parent_id", "eq", identity_value),
                        ("_write_gen", "lt", child_gen),
                    ],
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
    ) -> dict[str, Any]:
        return self._build_child_row(
            child_spec,
            child_item,
            child_identity,
            parent_id,
            fingerprint,
            write_gen,
            status=status,
            error=error,
            outputs=outputs,
            provenances=provenances,
        )

    def _insert_children_items(
        self,
        parent_id: Any,
        child_items: list[Any],
        child_spec: TableSpec,
        write_gen: int,
    ) -> Generator[RunGraph, Any, None]:
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
            existing_rows = self._read_rows(
                child_spec.name,
                (
                    ("_parent_id", "eq", parent_id),
                    (child_spec.identity, "eq", child_identity),
                ),
            )
            existing = max(existing_rows, key=lambda row: row.get("_write_gen", 0)) if existing_rows else None
            if existing is not None and existing.get("_row_fingerprint") == fingerprint and existing.get("_status") in (None, "complete"):
                if self._provenance.row_missing_stamp(existing, RECIPE_COLUMN):
                    bumped = self._stamp_existing_row(
                        child_spec.name,
                        existing,
                        write_gen,
                        child_spec,
                        normalize_values=False,
                    )
                else:
                    bumped = dict(existing)
                    bumped["_write_gen"] = write_gen
                self._store.write_rows(child_spec.name, [bumped])
                continue

            row: dict[str, Any] | None = None
            if existing is not None and existing.get("_status") in (None, "complete"):
                try:
                    reconciled = yield from self._reconcile(child_item, existing, child_spec)
                except Exception as error:
                    if self._on_error == "raise":
                        raise
                    row = self._build_child_action(
                        child_spec,
                        child_item,
                        child_identity,
                        parent_id,
                        fingerprint,
                        write_gen,
                        status="error",
                        error=f"{type(error).__name__}: {error}",
                    )
                    self._store.write_rows(child_spec.name, [row])
                    continue
                if isinstance(reconciled, ReconcileResult):
                    row = self._build_child_action(
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
                    child_outputs = _run_values((yield RunGraph(bound_graph, child_inputs)))
                except Exception as error:
                    if self._on_error == "raise":
                        raise
                    row = self._build_child_action(
                        child_spec,
                        child_item,
                        child_identity,
                        parent_id,
                        fingerprint,
                        write_gen,
                        status="error",
                        error=f"{type(error).__name__}: {error}",
                    )
                    self._store.write_rows(child_spec.name, [row])
                    continue
                row = self._build_child_action(
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
            self._store.write_rows(child_spec.name, [row])

    def _insert_children(
        self,
        parent_id: Any,
        outputs: Mapping[str, Any],
        child_spec: TableSpec,
        write_gen: int,
    ) -> Generator[RunGraph, Any, None]:
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
    ) -> Generator[RunGraph, Any, str]:
        outputs = reconciled.output_values()
        provenances = reconciled.provenance_values()
        identity_value = item[self._identity]
        for selection in reconciled.children:
            if isinstance(selection, RebuildChildren):
                rows = self._read_rows(selection.spec.name, (("_parent_id", "eq", identity_value),))
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
            self._evolve_for_metadata(item)
            row = self._build_parent_row(
                item,
                graph_inputs,
                outputs,
                write_gen,
                "complete",
                provenances=provenances,
            )
            self._store.write_rows(self._spec.name, [row])
            self._cleanup_parent(identity_value, write_gen)
        self._cleanup_children(identity_value, write_gen)
        return "skipped" if parent_skipped else "updated"

    def _provenances_for_values(self, values: Mapping[str, Any], pause: PauseInfo | None = None) -> dict[str, str]:
        provenances: dict[str, str] = {}
        for node in self._provenance.nodes_in_dependency_order():
            provenance = self._provenance.node_provenance(node, values)
            if provenance is None:
                continue
            for column in self._provenance.node_columns(node):
                if column.name in values or (pause is not None and column.name == pause.response_key):
                    provenances[column.name] = provenance
        return provenances

    def _waiting_receipt(self, identity_value: Any, outcome: WriteOutcome, row: Mapping[str, Any]) -> RowReceipt:
        pause, _provenance = deserialize_question(row["_question"])
        return RowReceipt(str(identity_value), outcome, RowStatus.WAITING, pause=pause)

    def _write_waiting_parent(
        self,
        item: dict[str, Any],
        source_inputs: dict[str, Any],
        outputs: Mapping[str, Any],
        provenances: Mapping[str, str],
        pause: PauseInfo,
        pause_provenance: str,
        write_gen: int,
        existing: dict[str, Any] | None,
        outcome: WriteOutcome,
    ) -> RowReceipt:
        identity_value = item[self._identity]
        self._evolve_for_metadata(item)
        row = self._build_parent_row(
            item,
            source_inputs,
            outputs,
            write_gen,
            "waiting",
            provenances=provenances,
            pause=pause,
            pause_provenance=pause_provenance,
        )
        self._store.write_rows(self._spec.name, [row])
        if existing is not None:
            self._cleanup_parent(identity_value, write_gen)
            self._cleanup_children(identity_value, write_gen)
        return RowReceipt(str(identity_value), outcome, RowStatus.WAITING, pause=pause)

    def _error_parent(
        self,
        item: dict[str, Any],
        graph_inputs: dict[str, Any],
        write_gen: int,
        error: Exception,
        existing: dict[str, Any] | None,
    ) -> None:
        self._evolve_for_metadata(item)
        row = self._build_parent_row(
            item,
            graph_inputs,
            {},
            write_gen,
            "error",
            error=f"{type(error).__name__}: {error}",
        )
        self._store.write_rows(self._spec.name, [row])
        if existing is not None:
            self._cleanup_parent(item[self._identity], write_gen)

    def _insert_one(
        self,
        item: dict[str, Any],
        write_gen: int,
        provided: set[str] | None = None,
    ) -> Generator[RunGraph, Any, RowReceipt]:
        identity_value = item[self._identity]
        provided_names = provided if provided is not None else set(item) - {self._identity}
        graph_inputs = self._graph_inputs(item, provided_names)
        source_inputs = self._source_inputs(item)
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        outcome = WriteOutcome.UPDATED if existing is not None else WriteOutcome.INSERTED
        fingerprint = self._provenance.root_fingerprint(source_inputs)
        answer_names = {column.name for column in self._spec.columns if column.role == "answer"}
        answer_provided = bool(answer_names & provided_names)
        if existing is not None and existing.get("_row_fingerprint") == fingerprint and existing.get("_status") == "waiting" and not answer_provided:
            return self._waiting_receipt(identity_value, WriteOutcome.SKIPPED, existing)
        parent_skipped = self._parent_skipped(existing, fingerprint)
        if answer_provided:
            parent_skipped = False
        if parent_skipped and not self._spec.children:
            if self._provenance.row_missing_stamp(existing, RECIPE_COLUMN):
                self._refresh_missing_stamps(existing)
            return RowReceipt(str(identity_value), WriteOutcome.SKIPPED, RowStatus.COMPLETE)

        if self._can_reconcile(existing):
            try:
                reconciled = yield from self._reconcile(item, existing, provided=provided_names)
            except Exception as error:
                if self._on_error == "raise":
                    raise
                if parent_skipped:
                    return RowReceipt(str(identity_value), WriteOutcome.SKIPPED, RowStatus.COMPLETE)
                self._error_parent(item, source_inputs, write_gen, error, existing)
                return RowReceipt(str(identity_value), outcome, RowStatus.ERROR, error=f"{type(error).__name__}: {error}")
            if isinstance(reconciled, _PausedConvergence):
                return self._write_waiting_parent(
                    item,
                    source_inputs,
                    reconciled.outputs,
                    reconciled.provenances,
                    reconciled.pause,
                    reconciled.provenance,
                    write_gen,
                    existing,
                    outcome,
                )
            if reconciled is not None:
                reconciled_outcome = yield from self._apply_reconciled(
                    item,
                    source_inputs,
                    existing,
                    reconciled,
                    parent_skipped,
                    write_gen,
                )
                return RowReceipt(
                    str(identity_value),
                    WriteOutcome.SKIPPED if reconciled_outcome == "skipped" else outcome,
                    RowStatus.COMPLETE,
                )

        try:
            result = yield RunGraph(self._graph, graph_inputs)
        except Exception as error:
            if self._on_error == "raise":
                raise
            if parent_skipped:
                return RowReceipt(str(identity_value), WriteOutcome.SKIPPED, RowStatus.COMPLETE)
            self._error_parent(item, source_inputs, write_gen, error, existing)
            return RowReceipt(str(identity_value), outcome, RowStatus.ERROR, error=f"{type(error).__name__}: {error}")

        outputs = _run_values(result)
        pause = _run_pause(result)
        if pause is not None:
            provenances = self._provenances_for_values({**item, **outputs}, pause)
            pause_provenance = provenances.get(pause.response_key)
            if pause_provenance is None:
                raise RuntimeError(f"could not compute provenance for interrupt answer {pause.response_key!r}")
            return self._write_waiting_parent(
                item,
                source_inputs,
                outputs,
                provenances,
                pause,
                pause_provenance,
                write_gen,
                existing,
                outcome,
            )

        if parent_skipped:
            for child_spec in self._spec.children:
                yield from self._insert_children(identity_value, outputs, child_spec, write_gen)
            self._cleanup_children(identity_value, write_gen)
            return RowReceipt(str(identity_value), WriteOutcome.SKIPPED, RowStatus.COMPLETE)

        for child_spec in self._spec.children:
            yield from self._insert_children(identity_value, outputs, child_spec, write_gen)
        self._evolve_for_metadata(item)
        row = self._build_parent_row(
            item,
            source_inputs,
            outputs,
            write_gen,
            "complete",
        )
        self._store.write_rows(self._spec.name, [row])
        if existing is not None:
            self._cleanup_parent(identity_value, write_gen)
            self._cleanup_children(identity_value, write_gen)
        return RowReceipt(str(identity_value), outcome, RowStatus.COMPLETE)

    def insert(self, items: list[dict[str, Any]]) -> WriteOperation:
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        receipts: list[RowReceipt] = []
        for item in items:
            receipts.append((yield from self._insert_one(item, write_gen)))
        return TableReceipt(tuple(receipts))

    def _prepare_update(
        self,
        identity_value: str,
        changes: dict[str, Any],
    ) -> tuple[dict[str, Any], bool, int]:
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        if existing is None:
            raise KeyError(identity_value)
        item: dict[str, Any] = {self._identity: identity_value}
        for column in self._spec.columns:
            if column.role in ("source", "answer") and column.name in existing and not self._provenance.column_is_null(existing[column.name]):
                item[column.name] = normalize_value(existing[column.name])
        spec_columns = {column.name for column in self._spec.columns}
        for key, value in existing.items():
            if key not in spec_columns and not is_internal_column(key):
                item[key] = normalize_value(value)
        item.update(changes)
        derivation_inputs = {column.name for column in self._spec.columns if column.role in ("source", "answer")}
        needs_rederive = any(key in derivation_inputs for key in changes)
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        return item, needs_rederive, write_gen

    def update(self, identity_value: str, changes: dict[str, Any]) -> WriteOperation:
        item, needs_rederive, write_gen = self._prepare_update(identity_value, changes)
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        if not needs_rederive:
            self._evolve_for_metadata({self._identity: identity_value, **changes})
            row = {key: normalize_value(value) for key, value in existing.items()}
            row.update(changes)
            row["_write_gen"] = write_gen
            self._store.write_rows(self._spec.name, [row])
            self._cleanup_parent(identity_value, write_gen)
            status = (
                RowStatus.ERROR
                if existing.get("_status") == "error"
                else RowStatus.WAITING
                if existing.get("_status") == "waiting"
                else RowStatus.COMPLETE
            )
            pause = deserialize_question(existing["_question"])[0] if status is RowStatus.WAITING else None
            return RowReceipt(
                str(identity_value),
                WriteOutcome.SKIPPED,
                status,
                pause=pause,
                error=existing.get("_error") if status is RowStatus.ERROR else None,
            )

        return (yield from self._insert_one(item, write_gen, provided=set(changes)))

    def delete(self, identity_value: str) -> WriteOperation:
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        if existing is None:
            return
        for child_spec in self._spec.children:
            self._store.delete_rows(child_spec.name, [("_parent_id", "eq", identity_value)])
        self._store.delete_rows(self._spec.name, [(self._identity, "eq", identity_value)])
        if False:
            yield RunGraph(self._graph, {})

    def _row_unchanged(self, item: dict[str, Any], existing: dict[str, Any]) -> bool:
        inputs = self._source_inputs(item)
        return existing.get("_row_fingerprint") == self._provenance.root_fingerprint(inputs)

    def sync(self, items: list[dict[str, Any]]) -> WriteOperation:
        rows = self._read_rows(self._spec.name)
        existing_by_id = {str(row[self._identity]): row for row in dedup_rows(rows, self._identity) if row.get(self._identity) is not None}
        incoming_ids: set[str] = set()
        receipts: list[RowReceipt] = []
        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for item in items:
            identity_value = str(item[self._identity])
            incoming_ids.add(identity_value)
            existing = existing_by_id.get(identity_value)
            if existing is None:
                receipts.append((yield from self._insert_one(item, write_gen)))
            elif self._row_unchanged(item, existing) and existing.get("_status") in (None, "complete"):
                if self._provenance.row_missing_stamp(existing, RECIPE_COLUMN):
                    self._refresh_missing_stamps(existing)
                receipts.append(RowReceipt(identity_value, WriteOutcome.SKIPPED, RowStatus.COMPLETE))
            else:
                if self._row_unchanged(item, existing):
                    receipts.append((yield from self._insert_one(item, write_gen)))
                else:
                    changes = {key: value for key, value in item.items() if key != self._identity}
                    receipts.append((yield from self.update(identity_value, changes)))

        deleted = 0
        for identity_value in existing_by_id:
            if identity_value not in incoming_ids:
                yield from self.delete(identity_value)
                deleted += 1
        return TableReceipt(tuple(receipts), deleted=deleted)

    def set_rows(self, where: _Predicate, fields: dict[str, Any]) -> WriteOperation:
        content_keys = {column.name for column in self._spec.columns if column.content_key}
        blocked = sorted(content_keys.intersection(fields))
        if blocked:
            raise ValueError(f"set() cannot update content-key fields: {', '.join(blocked)}")
        rows = dedup_rows(self._read_rows(self._spec.name, where), self._identity)
        if not rows:
            return 0
        self._evolve_for_metadata({self._identity: rows[0][self._identity], **fields})
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        updated = []
        for row in rows:
            new_row = {key: normalize_value(value) for key, value in row.items()}
            new_row.update(fields)
            new_row["_write_gen"] = write_gen
            updated.append(new_row)
        self._store.write_rows(self._spec.name, updated)
        for row in rows:
            self._store.delete_rows(
                self._spec.name,
                [(self._identity, "eq", row[self._identity]), ("_write_gen", "lt", write_gen)],
            )
        if False:
            yield RunGraph(self._graph, {})
        return len(updated)

    def derive_column(self, column: str, *, backfill: bool) -> WriteOperation:
        if backfill:
            self._evolve_for_backfill_column(column)
        node = self._provenance.producing_node(column)
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        rows = dedup_rows(self._read_rows(self._spec.name), self._identity)
        receipts: list[RowReceipt] = []
        for existing in rows:
            if backfill and not self._provenance.column_is_null(existing.get(column)):
                receipts.append(RowReceipt(str(existing[self._identity]), WriteOutcome.SKIPPED, RowStatus.COMPLETE))
                continue
            values = self._provenance.stored_values(existing)
            outputs = _run_values(
                (
                    yield RunGraph(
                        self._provenance.column_graph(node),
                        self._provenance.node_inputs(node, values),
                    )
                )
            )
            new_row = self._build_node_row(existing, node, outputs, write_gen)
            self._store.write_rows(self._spec.name, [new_row])
            self._store.delete_rows(
                self._spec.name,
                [
                    (self._identity, "eq", existing[self._identity]),
                    ("_write_gen", "lt", write_gen),
                ],
            )
            receipts.append(RowReceipt(str(existing[self._identity]), WriteOutcome.UPDATED, RowStatus.COMPLETE))
        return TableReceipt(tuple(receipts))
