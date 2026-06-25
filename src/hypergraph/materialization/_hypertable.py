"""HyperTable: a Hypergraph graph where each node output is a stored column."""

from __future__ import annotations

import math
from typing import Any

from hypergraph import Graph
from hypergraph.materialization._fingerprint import compute_child_fingerprint, compute_provenance, compute_row_fingerprint
from hypergraph.materialization._schema import (
    STATUS_COLUMNS,
    TableSpec,
    analyze_table,
    input_names,
    is_internal_column,
    python_type_to_arrow,
    return_type,
)
from hypergraph.materialization._types import ErrorRow, SyncResult


def _normalize_to_dict(item: Any) -> dict[str, Any]:
    """Convert a mapped child item to a plain dict if it isn't one already."""
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="python")
    if hasattr(item, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(item)
    return dict(item)


def _normalize_value(v: Any) -> Any:
    """Convert numpy/arrow types back to Python-native for the public API."""
    import numpy as np

    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    return v


def _dedup_rows(rows: list[dict[str, Any]], identity: str) -> list[dict[str, Any]]:
    """Keep only the highest _write_gen per identity (crash-leftover dedup)."""
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        id_val = str(row.get(identity, ""))
        existing = best.get(id_val)
        if existing is None or row.get("_write_gen", 0) > existing.get("_write_gen", 0):
            best[id_val] = row
    return list(best.values())


def _dedup_child_rows(rows: list[dict[str, Any]], identity: str) -> list[dict[str, Any]]:
    """Keep only the highest _write_gen per parent+child identity."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        id_val = str(row.get(identity, ""))
        parent_val = str(row.get("_parent_id", ""))
        key = (parent_val, id_val)
        existing = best.get(key)
        if existing is None or row.get("_write_gen", 0) > existing.get("_write_gen", 0):
            best[key] = row
    return list(best.values())


def _public_row(row: dict[str, Any], *, include_status: bool = False) -> dict[str, Any]:
    result = {}
    for k, v in row.items():
        if include_status and k in STATUS_COLUMNS:
            if k == "_status" and v is None:
                result[k] = "complete"
            else:
                result[k] = _normalize_value(v)
        elif not is_internal_column(k):
            result[k] = _normalize_value(v)
    return result


def _where_predicate(where: Any) -> list[tuple[str, str, Any]]:
    if where is None:
        return []
    if isinstance(where, dict):
        return [(key, "eq", value) for key, value in where.items()]
    return list(where)


class HyperTable:
    """A Hypergraph graph where each node output is a stored column."""

    def __init__(
        self,
        nodes: list,
        *,
        identity: str,
        store: Any,
        on_error: str = "raise",
        _components: dict[str, Any] | None = None,
        _runner: Any | None = None,
        _graph: Graph | None = None,
    ):
        if on_error not in ("raise", "store"):
            raise ValueError(f"on_error must be 'raise' or 'store', got {on_error!r}")
        self._nodes = nodes
        self._identity = identity
        self._store = store
        self._on_error = on_error
        self._components = _components or {}
        self._runner = _runner
        self._graph = _graph
        self._spec: TableSpec | None = None
        self._analyzed = False

    def bind(self, **components: Any) -> HyperTable:
        merged = {**self._components, **components}
        return HyperTable(
            self._nodes,
            identity=self._identity,
            store=self._store,
            on_error=self._on_error,
            _components=merged,
            _runner=self._runner,
        )

    def with_runner(self, runner: Any) -> HyperTable:
        return HyperTable(
            self._nodes,
            identity=self._identity,
            store=self._store,
            on_error=self._on_error,
            _components=self._components,
            _runner=runner,
        )

    def _ensure_analyzed(self):
        if self._analyzed:
            return
        self._build_graph()
        self._analyze_graph()
        self._resolve_store()
        self._analyzed = True

    def _build_graph(self):
        if self._graph is not None:
            return
        plain_nodes = []
        self._map_over_nodes = []
        for n in self._nodes:
            if hasattr(n, "_map_config") and n._map_config:
                self._map_over_nodes.append(n)
            else:
                plain_nodes.append(n)

        self._graph = Graph(plain_nodes, name=f"hypertable_{self._identity}")
        if self._components:
            valid_inputs = set(self._graph.inputs.all)
            root_binds = {k: v for k, v in self._components.items() if k in valid_inputs}
            if root_binds:
                self._graph = self._graph.bind(**root_binds)

    def _analyze_graph(self):
        self._spec = analyze_table(self._graph, self._identity, self._components, getattr(self, "_map_over_nodes", []))

    def _resolve_store(self):
        from hypergraph.materialization._table_store import TableStore

        if not isinstance(self._store, TableStore):
            raise TypeError(f"store must be a TableStore instance (e.g. LanceDBStore), got {type(self._store)}")

        self._store.open(self._spec, self._spec.children)

    def _require_runner(self):
        if self._runner is None:
            raise RuntimeError("No runner set. Call .with_runner(SyncRunner()) before write operations.")

    def _is_async_runner(self) -> bool:
        from hypergraph.runners import AsyncRunner

        return isinstance(self._runner, AsyncRunner)

    # --- Shared helpers ---

    def _graph_required_inputs(self) -> set[str]:
        return input_names(self._graph.inputs.required)

    def _extract_graph_inputs(self, item: dict[str, Any]) -> dict[str, Any]:
        required = self._graph_required_inputs()
        return {k: v for k, v in item.items() if k != self._identity and k in required}

    def _extract_outputs(self, result: Any) -> dict[str, Any]:
        if hasattr(result, "values") and isinstance(result.values, dict):
            return result.values
        if isinstance(result, dict):
            return result
        return {}

    def _build_row(
        self,
        item: dict[str, Any],
        graph_inputs: dict[str, Any],
        outputs: dict[str, Any],
        write_gen: int,
    ) -> dict[str, Any]:
        identity_value = item[self._identity]
        row: dict[str, Any] = {self._identity: identity_value}
        row.update({k: v for k, v in item.items() if k != self._identity})

        derived_cols = [c for c in self._spec.columns if c.role == "derived"]
        for col in derived_cols:
            if col.name in outputs:
                row[col.name] = outputs[col.name]

        row["_row_fingerprint"] = self._compute_row_fingerprint(graph_inputs)
        row["_write_gen"] = write_gen

        for col in derived_cols:
            row[f"_provenance_{col.name}"] = compute_provenance(col.name, graph_inputs)

        return row

    def _write_error_row(
        self,
        item: dict[str, Any],
        graph_inputs: dict[str, Any],
        write_gen: int,
        error: Exception,
        existing: dict[str, Any] | None,
    ) -> None:
        identity_value = item[self._identity]
        self._evolve_for_metadata(item)

        row: dict[str, Any] = {self._identity: identity_value}
        row.update({k: v for k, v in item.items() if k != self._identity})

        derived_cols = [c for c in self._spec.columns if c.role == "derived"]
        for col in derived_cols:
            row[col.name] = None

        row["_row_fingerprint"] = self._compute_row_fingerprint(graph_inputs)
        row["_write_gen"] = write_gen
        row["_status"] = "error"
        row["_error"] = f"{type(error).__name__}: {error}"

        self._store.write_rows(self._spec.name, [row])

        if existing is not None:
            self._store.delete_rows(
                self._spec.name,
                [
                    (self._identity, "eq", identity_value),
                    ("_write_gen", "lt", write_gen),
                ],
            )

    def _evolve_for_metadata(self, item: dict[str, Any], *, table_name: str | None = None, identity: str | None = None) -> None:
        """Add schema columns for metadata keys the store hasn't seen."""
        store = self._store
        target = table_name or self._spec.name
        id_col = identity or self._identity
        sample = store.read_rows(target, limit=1)
        known_cols = set(sample[0].keys()) if sample else {c.name for c in self._spec.columns}
        new_meta = {k: python_type_to_arrow(type(v) if v is not None else str) for k, v in item.items() if k not in known_cols and k != id_col}
        if new_meta:
            store.evolve_schema(target, new_meta)

    def _get_derived_column_type(self, column_name: str) -> type:
        for c in self._spec.columns:
            if c.name == column_name and c.role == "derived" and c.produced_by:
                return return_type(c.produced_by)
        return str

    # --- Public API ---

    def visualize(self, *, include_children: bool = True, **kwargs) -> Any:
        self._ensure_analyzed()
        if not include_children or not self._spec.children:
            return self._graph.visualize(**kwargs)
        from hypergraph.graph import Graph as _Graph

        all_nodes = list(self._graph.nodes.values()) if isinstance(self._graph.nodes, dict) else []
        for map_node in getattr(self, "_map_over_nodes", []):
            all_nodes.append(map_node)
        combined = _Graph(all_nodes, name=self._spec.name)
        if self._components:
            valid_inputs = set(combined.inputs.all)
            binds = {k: v for k, v in self._components.items() if k in valid_inputs}
            if binds:
                combined = combined.bind(**binds)
        return combined.visualize(depth=1, **kwargs)

    def count(self, child_table: str | None = None) -> int:
        self._ensure_analyzed()
        if child_table:
            for child in self._spec.children:
                if child.name == child_table:
                    return len(_dedup_child_rows(self._store.read_rows(child.name), child.identity))
            return 0
        return len(_dedup_rows(self._store.read_rows(self._spec.name), self._identity))

    def get(self, identity_value: str, *, include_status: bool = False) -> dict[str, Any] | None:
        self._ensure_analyzed()
        row = self._store.read_one(self._spec.name, self._identity, identity_value)
        if row is None:
            return None
        return _public_row(row, include_status=include_status)

    def children(self, parent_id: str, *, include_status: bool = False) -> list[dict[str, Any]]:
        self._ensure_analyzed()
        if not self._spec.children:
            return []
        child_spec = self._spec.children[0]
        rows = self._store.read_rows(child_spec.name, [("_parent_id", "eq", parent_id)])
        rows = _dedup_child_rows(rows, child_spec.identity)
        return [_public_row(row, include_status=include_status) for row in rows]

    def filter(self, where: Any = None, *, limit: int | None = None, include_status: bool = False) -> list[dict[str, Any]]:
        """Return public rows matching a store predicate."""
        self._ensure_analyzed()
        rows = self._store.read_rows(self._spec.name, _where_predicate(where), limit=limit)
        rows = _dedup_rows(rows, self._identity)
        return [_public_row(row, include_status=include_status) for row in rows]

    def delete_children(self, where: Any = None) -> int:
        """Delete child rows matching a predicate. Returns count deleted."""
        self._ensure_analyzed()
        if not self._spec.children:
            return 0
        child_spec = self._spec.children[0]
        return self._store.delete_rows(child_spec.name, _where_predicate(where))

    def filter_children(self, where: Any = None, *, limit: int | None = None, include_status: bool = False) -> list[dict[str, Any]]:
        """Return child rows matching a store predicate."""
        self._ensure_analyzed()
        if not self._spec.children:
            return []
        child_spec = self._spec.children[0]
        rows = self._store.read_rows(child_spec.name, _where_predicate(where), limit=limit)
        rows = _dedup_child_rows(rows, child_spec.identity)
        return [_public_row(row, include_status=include_status) for row in rows]

    def set_children(self, where: Any = None, **fields: Any) -> int:
        """Bulk metadata update for child rows matching a predicate."""
        self._ensure_analyzed()
        if not self._spec.children:
            return 0
        child_spec = self._spec.children[0]
        table_name = child_spec.name
        identity = child_spec.identity
        rows = _dedup_child_rows(
            self._store.read_rows(table_name, _where_predicate(where)),
            identity,
        )
        if not rows:
            return 0
        self._evolve_for_metadata(
            {identity: rows[0][identity], **fields},
            table_name=table_name,
            identity=identity,
        )
        write_gen = self._store.max_write_gen(table_name) + 1
        updated: list[dict[str, Any]] = []
        for row in rows:
            new_row = {k: _normalize_value(v) for k, v in row.items()}
            new_row.update(fields)
            new_row["_write_gen"] = write_gen
            updated.append(new_row)
        self._store.write_rows(table_name, updated)
        for row in rows:
            self._store.delete_rows(
                table_name,
                [
                    (identity, "eq", row[identity]),
                    ("_parent_id", "eq", row["_parent_id"]),
                    ("_write_gen", "lt", write_gen),
                ],
            )
        return len(updated)

    def set(self, where: Any, **fields: Any) -> Any:
        """Bulk metadata update for all rows matching a predicate."""
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._set_async(where, **fields)
        return self._set_sync(where, **fields)

    def _set_sync(self, where: Any, **fields: Any) -> int:
        content_key_fields = {c.name for c in self._spec.columns if c.content_key}
        blocked = sorted(content_key_fields.intersection(fields))
        if blocked:
            raise ValueError(f"set() cannot update content-key fields: {', '.join(blocked)}")

        rows = _dedup_rows(self._store.read_rows(self._spec.name, _where_predicate(where)), self._identity)
        if not rows:
            return 0

        self._evolve_for_metadata({self._identity: rows[0][self._identity], **fields})
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        updated_rows: list[dict[str, Any]] = []
        for row in rows:
            new_row = {k: _normalize_value(v) for k, v in row.items()}
            new_row.update(fields)
            new_row["_write_gen"] = write_gen
            updated_rows.append(new_row)

        self._store.write_rows(self._spec.name, updated_rows)
        for row in rows:
            self._store.delete_rows(
                self._spec.name,
                [
                    (self._identity, "eq", row[self._identity]),
                    ("_write_gen", "lt", write_gen),
                ],
            )
        return len(updated_rows)

    async def _set_async(self, where: Any, **fields: Any) -> int:
        return self._set_sync(where, **fields)

    def _insert_items(self, *args, **kwargs) -> list[dict[str, Any]]:
        if args and isinstance(args[0], list):
            return args[0]
        if kwargs:
            return [kwargs]
        raise ValueError("insert() requires kwargs or a list of dicts")

    def insert(self, *args, **kwargs) -> Any:
        self._require_runner()
        self._ensure_analyzed()
        items = self._insert_items(*args, **kwargs)
        if self._is_async_runner():
            return self._insert_async(items)
        return self._insert_sync(items)

    def _insert_sync(self, items: list[dict[str, Any]]) -> None:
        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for item in items:
            self._insert_one(item, write_gen)

    async def _insert_async(self, items: list[dict[str, Any]]) -> None:
        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for item in items:
            await self._insert_one_async(item, write_gen)

    def _plan_insert(self, item: dict[str, Any], graph_inputs: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """Read the current row and decide whether the parent can be skipped.

        A parent is skippable when its fingerprint is unchanged and its last write
        completed. Returns ``(existing_row, parent_skipped)``.
        """
        existing = self._store.read_one(self._spec.name, self._identity, item[self._identity])
        parent_skipped = False
        if existing is not None and existing.get("_row_fingerprint") == self._compute_row_fingerprint(graph_inputs):
            status = existing.get("_status")
            parent_skipped = status is None or status == "complete"
        return existing, parent_skipped

    def _write_parent_row(self, item: dict[str, Any], graph_inputs: dict[str, Any], outputs: dict[str, Any], write_gen: int) -> None:
        """Persist the parent row for a completed derivation."""
        self._evolve_for_metadata(item)
        row = self._build_row(item, graph_inputs, outputs, write_gen)
        row["_status"] = "complete"
        row["_error"] = None
        self._store.write_rows(self._spec.name, [row])

    def _cleanup_old_parent_gens(self, identity_value: Any, write_gen: int) -> None:
        self._store.delete_rows(
            self._spec.name,
            [(self._identity, "eq", identity_value), ("_write_gen", "lt", write_gen)],
        )

    def _cleanup_old_child_gens(self, identity_value: Any, write_gen: int) -> None:
        for child_spec in self._spec.children:
            self._store.delete_rows(
                child_spec.name,
                [("_parent_id", "eq", identity_value), ("_write_gen", "lt", write_gen)],
            )

    def _insert_one(self, item: dict[str, Any], write_gen: int) -> str:
        """Insert or upsert a single row. Returns 'inserted', 'updated', 'skipped', or 'errored'."""
        identity_value = item[self._identity]
        graph_inputs = self._extract_graph_inputs(item)
        existing, parent_skipped = self._plan_insert(item, graph_inputs)
        if parent_skipped and not self._spec.children:
            return "skipped"

        try:
            outputs = self._extract_outputs(self._runner.run(self._graph, **graph_inputs))
        except Exception as e:
            if self._on_error == "raise":
                raise
            if parent_skipped:
                # Parent is complete and unchanged; a transient failure while
                # re-deriving solely to reconcile children must not downgrade the
                # stored-complete parent to an error row.
                return "skipped"
            self._write_error_row(item, graph_inputs, write_gen, e, existing)
            return "errored"

        if parent_skipped:
            # Parent unchanged — reconcile children only (don't rewrite parent).
            for child_spec in self._spec.children:
                self._insert_children(identity_value, outputs, child_spec, write_gen)
            self._cleanup_old_child_gens(identity_value, write_gen)
            return "skipped"

        self._write_parent_row(item, graph_inputs, outputs, write_gen)
        for child_spec in self._spec.children:
            self._insert_children(identity_value, outputs, child_spec, write_gen)
        if existing is not None:
            self._cleanup_old_parent_gens(identity_value, write_gen)
            self._cleanup_old_child_gens(identity_value, write_gen)
        return "updated" if existing is not None else "inserted"

    async def _insert_one_async(self, item: dict[str, Any], write_gen: int) -> str:
        """Async twin of ``_insert_one`` — identical except the awaited graph run."""
        identity_value = item[self._identity]
        graph_inputs = self._extract_graph_inputs(item)
        existing, parent_skipped = self._plan_insert(item, graph_inputs)
        if parent_skipped and not self._spec.children:
            return "skipped"

        try:
            outputs = self._extract_outputs(await self._runner.run(self._graph, **graph_inputs))
        except Exception as e:
            if self._on_error == "raise":
                raise
            if parent_skipped:
                # Parent is complete and unchanged; a transient failure while
                # re-deriving solely to reconcile children must not downgrade the
                # stored-complete parent to an error row.
                return "skipped"
            self._write_error_row(item, graph_inputs, write_gen, e, existing)
            return "errored"

        if parent_skipped:
            for child_spec in self._spec.children:
                await self._insert_children_async(identity_value, outputs, child_spec, write_gen)
            self._cleanup_old_child_gens(identity_value, write_gen)
            return "skipped"

        self._write_parent_row(item, graph_inputs, outputs, write_gen)
        for child_spec in self._spec.children:
            await self._insert_children_async(identity_value, outputs, child_spec, write_gen)
        if existing is not None:
            self._cleanup_old_parent_gens(identity_value, write_gen)
            self._cleanup_old_child_gens(identity_value, write_gen)
        return "updated" if existing is not None else "inserted"

    def _bind_child_components(self, child_graph: Any) -> Any:
        if not self._components:
            return child_graph
        valid_inputs = set(child_graph.inputs.all)
        binds = {key: value for key, value in self._components.items() if key in valid_inputs}
        return child_graph.bind(**binds) if binds else child_graph

    def _child_items(self, outputs: dict, child_spec: TableSpec) -> list | None:
        """The mapped child items for a child table, or None if there's nothing to process."""
        if not child_spec.child_graph:
            return None
        child_items = outputs.get(child_spec.map_input)
        if not child_items or not isinstance(child_items, list):
            return None
        return child_items

    def _plan_child(
        self, child_spec: TableSpec, child_item: dict[str, Any], parent_id: str, write_gen: int
    ) -> tuple[str, dict[str, Any], str] | None:
        """Compute ``(child_identity, child_inputs, fingerprint)`` for a child item.

        If the child is unchanged and complete, bump its ``_write_gen`` so it
        survives cleanup and return None to signal a skip.
        """
        child_identity = child_item.get(child_spec.identity, "")
        child_inputs = {
            col.name: child_item[col.name] for col in child_spec.columns if col.role == "source" and col.content_key and col.name in child_item
        }
        new_fingerprint = self._compute_child_fingerprint(child_inputs, child_spec)

        existing_children = self._store.read_rows(
            child_spec.name,
            [("_parent_id", "eq", parent_id), (child_spec.identity, "eq", child_identity)],
        )
        existing_child = max(existing_children, key=lambda r: r.get("_write_gen", 0)) if existing_children else None
        if existing_child is not None and existing_child.get("_row_fingerprint") == new_fingerprint:
            status = existing_child.get("_status")
            if status is None or status == "complete":
                bumped = dict(existing_child)
                bumped["_write_gen"] = write_gen
                self._store.write_rows(child_spec.name, [bumped])
                return None
        return child_identity, child_inputs, new_fingerprint

    def _child_row(
        self,
        child_spec: TableSpec,
        child_item: dict[str, Any],
        child_identity: str,
        parent_id: str,
        new_fingerprint: str,
        write_gen: int,
        *,
        status: str,
        error: str | None,
        outputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Assemble a child row from its source item plus (on success) its derived outputs."""
        row = {
            child_spec.identity: child_identity,
            "_parent_id": parent_id,
            "_write_gen": write_gen,
            "_row_fingerprint": new_fingerprint,
            "_status": status,
            "_error": error,
        }
        for k, v in child_item.items():
            if k != child_spec.identity and k != "_parent_id":
                row[k] = v
        for k, v in (outputs or {}).items():
            row[k] = v
        return row

    def _insert_children(self, parent_id: str, outputs: dict, child_spec: TableSpec, write_gen: int) -> None:
        child_items = self._child_items(outputs, child_spec)
        if child_items is None:
            return
        bound_graph = self._bind_child_components(child_spec.child_graph)
        for raw_item in child_items:
            child_item = _normalize_to_dict(raw_item)
            plan = self._plan_child(child_spec, child_item, parent_id, write_gen)
            if plan is None:
                continue
            child_identity, child_inputs, new_fingerprint = plan
            try:
                child_outputs = self._extract_outputs(self._runner.run(bound_graph, **child_inputs))
            except Exception as e:
                if self._on_error == "raise":
                    raise
                row = self._child_row(
                    child_spec, child_item, child_identity, parent_id, new_fingerprint, write_gen, status="error", error=f"{type(e).__name__}: {e}"
                )
                self._store.write_rows(child_spec.name, [row])
                continue
            row = self._child_row(
                child_spec, child_item, child_identity, parent_id, new_fingerprint, write_gen, status="complete", error=None, outputs=child_outputs
            )
            self._store.write_rows(child_spec.name, [row])

    async def _insert_children_async(self, parent_id: str, outputs: dict, child_spec: TableSpec, write_gen: int) -> None:
        child_items = self._child_items(outputs, child_spec)
        if child_items is None:
            return
        bound_graph = self._bind_child_components(child_spec.child_graph)
        for raw_item in child_items:
            child_item = _normalize_to_dict(raw_item)
            plan = self._plan_child(child_spec, child_item, parent_id, write_gen)
            if plan is None:
                continue
            child_identity, child_inputs, new_fingerprint = plan
            try:
                child_outputs = self._extract_outputs(await self._runner.run(bound_graph, **child_inputs))
            except Exception as e:
                if self._on_error == "raise":
                    raise
                row = self._child_row(
                    child_spec, child_item, child_identity, parent_id, new_fingerprint, write_gen, status="error", error=f"{type(e).__name__}: {e}"
                )
                self._store.write_rows(child_spec.name, [row])
                continue
            row = self._child_row(
                child_spec, child_item, child_identity, parent_id, new_fingerprint, write_gen, status="complete", error=None, outputs=child_outputs
            )
            self._store.write_rows(child_spec.name, [row])

    def update(self, identity_value: str, **changes: Any) -> Any:
        """Update a row. Re-derives downstream if source columns changed."""
        self._require_runner()
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._update_async(identity_value, **changes)
        return self._update_sync(identity_value, **changes)

    def _prepare_update(self, identity_value: str, changes: dict[str, Any]) -> tuple[dict[str, Any], bool, int]:
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        if existing is None:
            raise KeyError(identity_value)

        item: dict[str, Any] = {self._identity: identity_value}
        for c in self._spec.columns:
            if c.role == "source" and c.name in existing:
                item[c.name] = _normalize_value(existing[c.name])
        spec_col_names = {c.name for c in self._spec.columns}
        for k, v in existing.items():
            if k not in spec_col_names and not is_internal_column(k):
                item[k] = _normalize_value(v)
        item.update(changes)

        source_names = {c.name for c in self._spec.columns if c.role == "source"}
        needs_rederive = any(k in source_names for k in changes)

        write_gen = self._store.max_write_gen(self._spec.name) + 1
        return item, needs_rederive, write_gen

    def _row_with_changes(self, identity_value: str, changes: dict[str, Any], write_gen: int) -> dict[str, Any]:
        """Next generation of a row with metadata changes applied (no re-derivation)."""
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        row = {k: _normalize_value(v) for k, v in existing.items()}
        row.update(changes)
        row["_write_gen"] = write_gen
        return row

    def _update_sync(self, identity_value: str, **changes: Any) -> None:
        item, needs_rederive, write_gen = self._prepare_update(identity_value, changes)
        if needs_rederive:
            graph_inputs = self._extract_graph_inputs(item)
            outputs = self._extract_outputs(self._runner.run(self._graph, **graph_inputs))
            self._evolve_for_metadata(item)
            row = self._build_row(item, graph_inputs, outputs, write_gen)
        else:
            outputs = None
            row = self._row_with_changes(identity_value, changes, write_gen)

        self._store.write_rows(self._spec.name, [row])

        if needs_rederive:
            # Write new children BEFORE deleting old ones — crash-safe ordering.
            for child_spec in self._spec.children:
                self._insert_children(identity_value, outputs, child_spec, write_gen)
            self._cleanup_old_child_gens(identity_value, write_gen)
        self._cleanup_old_parent_gens(identity_value, write_gen)

    async def _update_async(self, identity_value: str, **changes: Any) -> None:
        item, needs_rederive, write_gen = self._prepare_update(identity_value, changes)
        if needs_rederive:
            graph_inputs = self._extract_graph_inputs(item)
            outputs = self._extract_outputs(await self._runner.run(self._graph, **graph_inputs))
            self._evolve_for_metadata(item)
            row = self._build_row(item, graph_inputs, outputs, write_gen)
        else:
            outputs = None
            row = self._row_with_changes(identity_value, changes, write_gen)

        self._store.write_rows(self._spec.name, [row])

        if needs_rederive:
            for child_spec in self._spec.children:
                await self._insert_children_async(identity_value, outputs, child_spec, write_gen)
            self._cleanup_old_child_gens(identity_value, write_gen)
        self._cleanup_old_parent_gens(identity_value, write_gen)

    def delete(self, identity_value: str) -> Any:
        """Delete a row and cascade-delete its children."""
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._delete_async(identity_value)
        return self._delete_sync(identity_value)

    def _delete_sync(self, identity_value: str) -> None:
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        if existing is None:
            return

        for child_spec in self._spec.children:
            self._store.delete_rows(child_spec.name, [("_parent_id", "eq", identity_value)])
        self._store.delete_rows(self._spec.name, [(self._identity, "eq", identity_value)])

    async def _delete_async(self, identity_value: str) -> None:
        self._delete_sync(identity_value)

    def sync(self, items: list[dict[str, Any]]) -> Any:
        """Reconcile: insert new, update changed, delete missing, skip unchanged."""
        self._require_runner()
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._sync_async(items)
        return self._sync_sync(items)

    def _sync_existing_index(self) -> dict[str, dict[str, Any]]:
        """Map identity -> newest stored row, for reconciliation."""
        rows = _dedup_rows(self._store.read_rows(self._spec.name), self._identity)
        return {str(row[self._identity]): row for row in rows if row.get(self._identity) is not None}

    def _row_unchanged(self, item: dict[str, Any], existing: dict[str, Any]) -> bool:
        return existing.get("_row_fingerprint") == self._compute_row_fingerprint(self._extract_graph_inputs(item))

    def _error_row_for(self, id_val: str) -> ErrorRow | None:
        """Build an ErrorRow from the persisted error row for a failed identity."""
        row = self._store.read_one(self._spec.name, self._identity, id_val)
        if not row:
            return None
        err = row.get("_error", "")
        return ErrorRow(
            identity={self._identity: id_val},
            error_type=err.split(":")[0] if err else "Unknown",
            error_msg=err,
        )

    def _sync_sync(self, items: list[dict[str, Any]]) -> Any:
        existing_by_id = self._sync_existing_index()
        incoming_ids: set[str] = set()
        inserted = updated = skipped = errored = 0
        errors: list[ErrorRow] = []
        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for item in items:
            id_val = str(item[self._identity])
            incoming_ids.add(id_val)
            existing = existing_by_id.get(id_val)
            if existing is None:
                if self._insert_one(item, write_gen) == "errored":
                    errored += 1
                    err = self._error_row_for(id_val)
                    if err is not None:
                        errors.append(err)
                else:
                    inserted += 1
            elif self._row_unchanged(item, existing):
                skipped += 1
            else:
                self.update(id_val, **{k: v for k, v in item.items() if k != self._identity})
                updated += 1

        deleted = 0
        for id_val in existing_by_id:
            if id_val not in incoming_ids:
                self.delete(id_val)
                deleted += 1

        return SyncResult(inserted=inserted, updated=updated, deleted=deleted, skipped=skipped, errored=errored, errors=tuple(errors))

    async def _sync_async(self, items: list[dict[str, Any]]) -> Any:
        existing_by_id = self._sync_existing_index()
        incoming_ids: set[str] = set()
        inserted = updated = skipped = errored = 0
        errors: list[ErrorRow] = []
        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for item in items:
            id_val = str(item[self._identity])
            incoming_ids.add(id_val)
            existing = existing_by_id.get(id_val)
            if existing is None:
                if await self._insert_one_async(item, write_gen) == "errored":
                    errored += 1
                    err = self._error_row_for(id_val)
                    if err is not None:
                        errors.append(err)
                else:
                    inserted += 1
            elif self._row_unchanged(item, existing):
                skipped += 1
            else:
                await self._update_async(id_val, **{k: v for k, v in item.items() if k != self._identity})
                updated += 1

        deleted = 0
        for id_val in existing_by_id:
            if id_val not in incoming_ids:
                await self._delete_async(id_val)
                deleted += 1

        return SyncResult(inserted=inserted, updated=updated, deleted=deleted, skipped=skipped, errored=errored, errors=tuple(errors))

    # --- Column re-derivation (recompute / backfill) ---

    def _source_inputs_from_row(self, existing: dict[str, Any]) -> dict[str, Any]:
        """Reconstruct graph inputs from a stored row's source columns."""
        return {c.name: _normalize_value(existing[c.name]) for c in self._spec.columns if c.role == "source" and c.name in existing}

    def _persist_rederived_column(
        self,
        existing: dict[str, Any],
        column: str,
        graph_inputs: dict[str, Any],
        outputs: dict[str, Any],
        write_gen: int,
    ) -> None:
        """Write a new generation of one row with `column` re-derived, then drop the old one."""
        new_row = {k: _normalize_value(v) for k, v in existing.items()}
        if column in outputs:
            new_row[column] = outputs[column]
        new_row["_write_gen"] = write_gen
        new_row[f"_provenance_{column}"] = compute_provenance(column, graph_inputs)

        self._store.write_rows(self._spec.name, [new_row])
        self._store.delete_rows(
            self._spec.name,
            [
                (self._identity, "eq", existing[self._identity]),
                ("_write_gen", "lt", write_gen),
            ],
        )

    def _evolve_for_backfill_column(self, column: str) -> None:
        """Add `column` (and its provenance) to the schema if it doesn't exist yet."""
        sample = self._store.read_rows(self._spec.name, limit=1)
        if sample and column not in sample[0]:
            col_type = self._get_derived_column_type(column)
            self._store.evolve_schema(
                self._spec.name,
                {column: python_type_to_arrow(col_type), f"_provenance_{column}": python_type_to_arrow(str)},
            )

    @staticmethod
    def _column_is_null(value: Any) -> bool:
        return value is None or (isinstance(value, float) and math.isnan(value))

    def recompute(self, column: str) -> Any:
        """Re-derive one column for all rows using current bound components."""
        self._require_runner()
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._recompute_async(column)
        return self._recompute_sync(column)

    def _recompute_sync(self, column: str) -> None:
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        for existing in _dedup_rows(self._store.read_rows(self._spec.name), self._identity):
            graph_inputs = self._source_inputs_from_row(existing)
            outputs = self._extract_outputs(self._runner.run(self._graph, **graph_inputs))
            self._persist_rederived_column(existing, column, graph_inputs, outputs, write_gen)

    async def _recompute_async(self, column: str) -> None:
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        for existing in _dedup_rows(self._store.read_rows(self._spec.name), self._identity):
            graph_inputs = self._source_inputs_from_row(existing)
            outputs = self._extract_outputs(await self._runner.run(self._graph, **graph_inputs))
            self._persist_rederived_column(existing, column, graph_inputs, outputs, write_gen)

    def backfill(self, column: str) -> Any:
        """Derive a new column for existing rows that have NULL."""
        self._require_runner()
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._backfill_async(column)
        return self._backfill_sync(column)

    def _backfill_sync(self, column: str) -> None:
        self._evolve_for_backfill_column(column)
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        for existing in _dedup_rows(self._store.read_rows(self._spec.name), self._identity):
            if not self._column_is_null(existing.get(column)):
                continue
            graph_inputs = self._source_inputs_from_row(existing)
            outputs = self._extract_outputs(self._runner.run(self._graph, **graph_inputs))
            self._persist_rederived_column(existing, column, graph_inputs, outputs, write_gen)

    async def _backfill_async(self, column: str) -> None:
        self._evolve_for_backfill_column(column)
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        for existing in _dedup_rows(self._store.read_rows(self._spec.name), self._identity):
            if not self._column_is_null(existing.get(column)):
                continue
            graph_inputs = self._source_inputs_from_row(existing)
            outputs = self._extract_outputs(await self._runner.run(self._graph, **graph_inputs))
            self._persist_rederived_column(existing, column, graph_inputs, outputs, write_gen)

    # --- Fingerprint and provenance ---

    def _compute_row_fingerprint(self, graph_inputs: dict) -> str:
        return compute_row_fingerprint(self._graph, self._components, graph_inputs)

    def _compute_child_fingerprint(self, child_inputs: dict, child_spec: TableSpec) -> str:
        return compute_child_fingerprint(child_spec.child_graph, self._components, child_inputs)
