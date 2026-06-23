"""HyperTable: a Hypergraph graph where each node output is a stored column."""

from __future__ import annotations

import hashlib
import json
import math
import typing
from dataclasses import dataclass, field
from typing import Any

from hypergraph import Graph
from hypergraph.materialization._keys import compute_definition_hash


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


def _is_internal(k: str) -> bool:
    return k in ("_row_fingerprint", "_write_gen") or k.startswith("_provenance_")


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


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _normalize_value(v) for k, v in row.items() if not _is_internal(k)}


def _where_predicate(where: Any) -> list[tuple[str, str, Any]]:
    if where is None:
        return []
    if isinstance(where, dict):
        return [(key, "eq", value) for key, value in where.items()]
    return list(where)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    role: str  # identity, source, derived, parent_link, internal
    produced_by: Any = None
    content_key: bool = False
    arrow_type: Any = None


@dataclass(frozen=True)
class TableSpec:
    name: str
    identity: str
    columns: list[ColumnSpec] = field(default_factory=list)
    children: list[TableSpec] = field(default_factory=list)
    parent_link: str | None = None
    child_graph: Any = None
    map_input: str | None = None


class HyperTable:
    """A Hypergraph graph where each node output is a stored column."""

    def __init__(
        self,
        nodes: list,
        *,
        identity: str,
        store: Any,
        _components: dict[str, Any] | None = None,
        _runner: Any | None = None,
        _graph: Graph | None = None,
    ):
        self._nodes = nodes
        self._identity = identity
        self._store = store
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
            _components=merged,
            _runner=self._runner,
        )

    def with_runner(self, runner: Any) -> HyperTable:
        return HyperTable(
            self._nodes,
            identity=self._identity,
            store=self._store,
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

    def _python_type_to_arrow(self, tp: Any) -> Any:
        import pyarrow as pa

        if tp is str:
            return pa.utf8()
        if tp is int:
            return pa.int64()
        if tp is float:
            return pa.float64()
        if tp is bool:
            return pa.bool_()
        if tp is bytes:
            return pa.large_binary()

        origin = typing.get_origin(tp)
        args = typing.get_args(tp)
        if origin is list:
            if args and args[0] is float:
                return pa.list_(pa.float32())
            if args and args[0] is str:
                return pa.list_(pa.utf8())
            if args and args[0] is int:
                return pa.list_(pa.int64())
            return pa.list_(pa.utf8())
        return pa.utf8()

    def _column(self, name: str, *, role: str, produced_by: Any = None, content_key: bool = False, python_type: Any = str) -> ColumnSpec:
        return ColumnSpec(
            name,
            role=role,
            produced_by=produced_by,
            content_key=content_key,
            arrow_type=self._python_type_to_arrow(python_type),
        )

    def _input_types(self, graph: Graph) -> dict[str, Any]:
        input_types: dict[str, Any] = {}
        nodes_dict = graph.nodes if isinstance(graph.nodes, dict) else {}
        for _name, node_obj in nodes_dict.items():
            func = getattr(node_obj, "func", None) or getattr(node_obj, "_func", None)
            if func is None:
                continue
            for name, hint in typing.get_type_hints(func).items():
                if name != "return":
                    input_types.setdefault(name, hint)
        return input_types

    def _return_type(self, node_obj: Any) -> Any:
        func = getattr(node_obj, "func", None) or getattr(node_obj, "_func", None)
        if func is None:
            return str
        return typing.get_type_hints(func).get("return", str)

    def _analyze_graph(self):
        graph = self._graph
        root_columns = []
        child_specs = []

        input_types = self._input_types(graph)
        root_columns.append(self._column(self._identity, role="identity"))

        required = set(graph.inputs.required) if isinstance(graph.inputs.required, tuple) else set(graph.inputs.required.keys())

        for inp_name in sorted(required):
            if inp_name == self._identity:
                continue
            root_columns.append(self._column(inp_name, role="source", content_key=True, python_type=input_types.get(inp_name, str)))

        for map_node in getattr(self, "_map_over_nodes", []):
            child_spec = self._analyze_map_over(map_node)
            if child_spec:
                child_specs.append(child_spec)

        child_map_inputs = {cs.map_input for cs in child_specs if cs.map_input}

        nodes_dict = graph.nodes if isinstance(graph.nodes, dict) else {}
        for _name, n in nodes_dict.items():
            for out_name in n.data_outputs if hasattr(n, "data_outputs") else ():
                if out_name not in child_map_inputs:
                    root_columns.append(self._column(out_name, role="derived", produced_by=n, python_type=self._return_type(n)))

        derived_cols = [c for c in root_columns if c.role == "derived"]
        prov_cols = [self._column(f"_provenance_{c.name}", role="internal") for c in derived_cols]
        final_columns = (
            root_columns
            + [self._column("_row_fingerprint", role="internal")]
            + prov_cols
            + [self._column("_write_gen", role="internal", python_type=int)]
        )

        self._spec = TableSpec(
            name=self._identity.replace("_id", ""),
            identity=self._identity,
            columns=final_columns,
            children=child_specs,
        )

    def _analyze_map_over(self, map_node) -> TableSpec | None:
        config = map_node._map_config if hasattr(map_node, "_map_config") else {}
        identity = config.get("identity", "item_id")
        inner_graph = getattr(map_node, "graph", None) or getattr(map_node, "_graph", None)
        raw_map_over = getattr(map_node, "_map_over", None)
        map_input = raw_map_over[0] if isinstance(raw_map_over, list) and raw_map_over else config.get("map_over")

        child_columns = [
            self._column(identity, role="identity"),
            self._column("_parent_id", role="parent_link"),
        ]

        if inner_graph:
            input_types = self._input_types(inner_graph)
            component_names = set(self._components.keys())
            inner_required = (
                set(inner_graph.inputs.required) if isinstance(inner_graph.inputs.required, tuple) else set(inner_graph.inputs.required.keys())
            )
            inner_optional = (
                set(inner_graph.inputs.optional) if isinstance(inner_graph.inputs.optional, tuple) else set(inner_graph.inputs.optional.keys())
            )
            inner_all = inner_required | inner_optional
            for inp_name in sorted(inner_all):
                if inp_name != identity and inp_name not in component_names:
                    child_columns.append(self._column(inp_name, role="source", content_key=True, python_type=input_types.get(inp_name, str)))
            nodes_dict = inner_graph.nodes if isinstance(inner_graph.nodes, dict) else {}
            for _name, n in nodes_dict.items():
                for out_name in n.data_outputs if hasattr(n, "data_outputs") else []:
                    child_columns.append(self._column(out_name, role="derived", produced_by=n, python_type=self._return_type(n)))

        child_columns.append(self._column("_row_fingerprint", role="internal"))
        child_columns.append(self._column("_write_gen", role="internal", python_type=int))

        table_name = identity.replace("_id", "")
        return TableSpec(
            name=table_name,
            identity=identity,
            columns=child_columns,
            parent_link="_parent_id",
            child_graph=inner_graph,
            map_input=map_input,
        )

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
        required = self._graph.inputs.required
        return set(required) if isinstance(required, tuple) else set(required.keys())

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

        row["_row_fingerprint"] = self._compute_row_fingerprint(item, graph_inputs)
        row["_write_gen"] = write_gen

        for col in derived_cols:
            prov = self._compute_provenance(col.name, graph_inputs, outputs)
            row[f"_provenance_{col.name}"] = prov

        return row

    def _evolve_for_metadata(self, item: dict[str, Any], *, table_name: str | None = None, identity: str | None = None) -> None:
        """Add schema columns for metadata keys the store hasn't seen."""
        store = self._store
        target = table_name or self._spec.name
        id_col = identity or self._identity
        sample = store.read_rows(target, limit=1)
        known_cols = set(sample[0].keys()) if sample else {c.name for c in self._spec.columns}
        new_meta = {k: (type(v) if v is not None else str) for k, v in item.items() if k not in known_cols and k != id_col}
        if new_meta:
            store.evolve_schema(target, new_meta)

    def _get_derived_column_type(self, column_name: str) -> type:
        for c in self._spec.columns:
            if c.name == column_name and c.role == "derived" and c.produced_by:
                func = getattr(c.produced_by, "func", None) or getattr(c.produced_by, "_func", None)
                if func:
                    hints = typing.get_type_hints(func)
                    return hints.get("return", str)
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

    def get(self, identity_value: str) -> dict[str, Any] | None:
        self._ensure_analyzed()
        row = self._store.read_one(self._spec.name, self._identity, identity_value)
        if row is None:
            return None
        return _public_row(row)

    def children(self, parent_id: str) -> list[dict[str, Any]]:
        self._ensure_analyzed()
        if not self._spec.children:
            return []
        child_spec = self._spec.children[0]
        rows = self._store.read_rows(child_spec.name, [("_parent_id", "eq", parent_id)])
        rows = _dedup_child_rows(rows, child_spec.identity)
        return [_public_row(row) for row in rows]

    def filter(self, where: Any = None, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return public rows matching a store predicate."""
        self._ensure_analyzed()
        rows = self._store.read_rows(self._spec.name, _where_predicate(where), limit=limit)
        rows = _dedup_rows(rows, self._identity)
        return [_public_row(row) for row in rows]

    def delete_children(self, where: Any = None) -> int:
        """Delete child rows matching a predicate. Returns count deleted."""
        self._ensure_analyzed()
        if not self._spec.children:
            return 0
        child_spec = self._spec.children[0]
        return self._store.delete_rows(child_spec.name, _where_predicate(where))

    def filter_children(self, where: Any = None, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return child rows matching a store predicate."""
        self._ensure_analyzed()
        if not self._spec.children:
            return []
        child_spec = self._spec.children[0]
        rows = self._store.read_rows(child_spec.name, _where_predicate(where), limit=limit)
        rows = _dedup_child_rows(rows, child_spec.identity)
        return [_public_row(row) for row in rows]

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
                [(identity, "eq", row[identity]), ("_write_gen", "lt", write_gen)],
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

    def _insert_one(self, item: dict[str, Any], write_gen: int) -> str:
        """Insert or upsert a single row. Returns 'inserted', 'updated', or 'skipped'."""
        identity_value = item[self._identity]
        graph_inputs = self._extract_graph_inputs(item)

        # Incrementality: check existing fingerprint
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        if existing is not None:
            new_fingerprint = self._compute_row_fingerprint(item, graph_inputs)
            if existing.get("_row_fingerprint") == new_fingerprint:
                return "skipped"

        # Run graph
        result = self._runner.run(self._graph, **graph_inputs)
        outputs = self._extract_outputs(result)

        # Schema evolution for metadata
        self._evolve_for_metadata(item)

        # Build and write row
        row = self._build_row(item, graph_inputs, outputs, write_gen)
        self._store.write_rows(self._spec.name, [row])

        for child_spec in self._spec.children:
            self._insert_children(identity_value, outputs, child_spec, write_gen)

        # Delete old version if exists
        if existing is not None:
            self._store.delete_rows(
                self._spec.name,
                [
                    (self._identity, "eq", identity_value),
                    ("_write_gen", "lt", write_gen),
                ],
            )
            # Delete old children
            for child_spec in self._spec.children:
                self._store.delete_rows(
                    child_spec.name,
                    [
                        ("_parent_id", "eq", identity_value),
                        ("_write_gen", "lt", write_gen),
                    ],
                )

        return "updated" if existing is not None else "inserted"

    async def _insert_one_async(self, item: dict[str, Any], write_gen: int) -> str:
        """Async insert/upsert for AsyncRunner-bound tables."""
        identity_value = item[self._identity]
        graph_inputs = self._extract_graph_inputs(item)

        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        if existing is not None:
            new_fingerprint = self._compute_row_fingerprint(item, graph_inputs)
            if existing.get("_row_fingerprint") == new_fingerprint:
                return "skipped"

        result = await self._runner.run(self._graph, **graph_inputs)
        outputs = self._extract_outputs(result)

        self._evolve_for_metadata(item)

        row = self._build_row(item, graph_inputs, outputs, write_gen)
        self._store.write_rows(self._spec.name, [row])

        for child_spec in self._spec.children:
            await self._insert_children_async(identity_value, outputs, child_spec, write_gen)

        if existing is not None:
            self._store.delete_rows(
                self._spec.name,
                [
                    (self._identity, "eq", identity_value),
                    ("_write_gen", "lt", write_gen),
                ],
            )
            for child_spec in self._spec.children:
                self._store.delete_rows(
                    child_spec.name,
                    [
                        ("_parent_id", "eq", identity_value),
                        ("_write_gen", "lt", write_gen),
                    ],
                )

        return "updated" if existing is not None else "inserted"

    def _insert_children(self, parent_id: str, outputs: dict, child_spec: TableSpec, write_gen: int):
        child_graph = child_spec.child_graph
        if not child_graph:
            return

        child_items = outputs.get(child_spec.map_input)
        if not child_items or not isinstance(child_items, list):
            return

        for child_item in child_items:
            child_item = _normalize_to_dict(child_item)
            child_identity = child_item.get(child_spec.identity, "")
            child_inputs = {}
            for col in child_spec.columns:
                if col.role == "source" and col.content_key and col.name in child_item:
                    child_inputs[col.name] = child_item[col.name]

            bound_graph = self._bind_child_components(child_graph)

            child_result = self._runner.run(bound_graph, **child_inputs)

            if hasattr(child_result, "values") and isinstance(child_result.values, dict):
                child_outputs = child_result.values
            elif isinstance(child_result, dict):
                child_outputs = child_result
            else:
                child_outputs = {}

            child_row = {
                child_spec.identity: child_identity,
                "_parent_id": parent_id,
                "_write_gen": write_gen,
                "_row_fingerprint": "",
            }

            for k, v in child_item.items():
                if k != child_spec.identity and k != "_parent_id":
                    child_row[k] = v

            for k, v in child_outputs.items():
                child_row[k] = v

            self._store.write_rows(child_spec.name, [child_row])

    def _bind_child_components(self, child_graph: Any) -> Any:
        if not self._components:
            return child_graph
        valid_inputs = set(child_graph.inputs.all)
        binds = {key: value for key, value in self._components.items() if key in valid_inputs}
        return child_graph.bind(**binds) if binds else child_graph

    async def _insert_children_async(self, parent_id: str, outputs: dict, child_spec: TableSpec, write_gen: int) -> None:
        child_graph = child_spec.child_graph
        if not child_graph:
            return

        child_items = outputs.get(child_spec.map_input)
        if not child_items or not isinstance(child_items, list):
            return

        for child_item in child_items:
            child_item = _normalize_to_dict(child_item)
            child_identity = child_item.get(child_spec.identity, "")
            child_inputs = {}
            for col in child_spec.columns:
                if col.role == "source" and col.content_key and col.name in child_item:
                    child_inputs[col.name] = child_item[col.name]

            bound_graph = self._bind_child_components(child_graph)

            child_result = await self._runner.run(bound_graph, **child_inputs)

            if hasattr(child_result, "values") and isinstance(child_result.values, dict):
                child_outputs = child_result.values
            elif isinstance(child_result, dict):
                child_outputs = child_result
            else:
                child_outputs = {}

            child_row = {
                child_spec.identity: child_identity,
                "_parent_id": parent_id,
                "_write_gen": write_gen,
                "_row_fingerprint": "",
            }

            for k, v in child_item.items():
                if k != child_spec.identity and k != "_parent_id":
                    child_row[k] = v

            for k, v in child_outputs.items():
                child_row[k] = v

            self._store.write_rows(child_spec.name, [child_row])

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
            if k not in spec_col_names and not _is_internal(k):
                item[k] = _normalize_value(v)
        item.update(changes)

        source_names = {c.name for c in self._spec.columns if c.role == "source"}
        needs_rederive = any(k in source_names for k in changes)

        write_gen = self._store.max_write_gen(self._spec.name) + 1
        return item, needs_rederive, write_gen

    def _update_sync(self, identity_value: str, **changes: Any) -> None:
        item, needs_rederive, write_gen = self._prepare_update(identity_value, changes)
        if needs_rederive:
            graph_inputs = self._extract_graph_inputs(item)
            result = self._runner.run(self._graph, **graph_inputs)
            outputs = self._extract_outputs(result)
            self._evolve_for_metadata(item)
            row = self._build_row(item, graph_inputs, outputs, write_gen)
        else:
            existing = self._store.read_one(self._spec.name, self._identity, identity_value)
            row = {k: _normalize_value(v) for k, v in existing.items()}
            row.update(changes)
            row["_write_gen"] = write_gen

        self._store.write_rows(self._spec.name, [row])

        if needs_rederive:
            # Write new children BEFORE deleting old ones — crash-safe ordering
            for child_spec in self._spec.children:
                self._insert_children(identity_value, outputs, child_spec, write_gen)
            for child_spec in self._spec.children:
                self._store.delete_rows(
                    child_spec.name,
                    [
                        ("_parent_id", "eq", identity_value),
                        ("_write_gen", "lt", write_gen),
                    ],
                )

        self._store.delete_rows(
            self._spec.name,
            [
                (self._identity, "eq", identity_value),
                ("_write_gen", "lt", write_gen),
            ],
        )

    async def _update_async(self, identity_value: str, **changes: Any) -> None:
        item, needs_rederive, write_gen = self._prepare_update(identity_value, changes)
        if needs_rederive:
            graph_inputs = self._extract_graph_inputs(item)
            result = await self._runner.run(self._graph, **graph_inputs)
            outputs = self._extract_outputs(result)
            self._evolve_for_metadata(item)
            row = self._build_row(item, graph_inputs, outputs, write_gen)
        else:
            existing = self._store.read_one(self._spec.name, self._identity, identity_value)
            row = {k: _normalize_value(v) for k, v in existing.items()}
            row.update(changes)
            row["_write_gen"] = write_gen

        self._store.write_rows(self._spec.name, [row])

        if needs_rederive:
            for child_spec in self._spec.children:
                await self._insert_children_async(identity_value, outputs, child_spec, write_gen)
            for child_spec in self._spec.children:
                self._store.delete_rows(
                    child_spec.name,
                    [
                        ("_parent_id", "eq", identity_value),
                        ("_write_gen", "lt", write_gen),
                    ],
                )

        self._store.delete_rows(
            self._spec.name,
            [
                (self._identity, "eq", identity_value),
                ("_write_gen", "lt", write_gen),
            ],
        )

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

    def _sync_sync(self, items: list[dict[str, Any]]) -> Any:
        from hypergraph.materialization._types import SyncResult

        existing_rows = _dedup_rows(self._store.read_rows(self._spec.name), self._identity)
        existing_by_id: dict[str, dict] = {str(row[self._identity]): row for row in existing_rows if row.get(self._identity) is not None}

        incoming_ids: set[str] = set()
        inserted = 0
        updated = 0
        skipped = 0

        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for item in items:
            id_val = str(item[self._identity])
            incoming_ids.add(id_val)

            existing = existing_by_id.get(id_val)
            if existing is None:
                self._insert_one(item, write_gen)
                inserted += 1
            else:
                graph_inputs = self._extract_graph_inputs(item)
                new_fp = self._compute_row_fingerprint(item, graph_inputs)
                if existing.get("_row_fingerprint") == new_fp:
                    skipped += 1
                else:
                    changes = {k: v for k, v in item.items() if k != self._identity}
                    self.update(id_val, **changes)
                    updated += 1

        deleted = 0
        for id_val in existing_by_id:
            if id_val not in incoming_ids:
                self.delete(id_val)
                deleted += 1

        return SyncResult(inserted=inserted, updated=updated, deleted=deleted, skipped=skipped, errored=0)

    async def _sync_async(self, items: list[dict[str, Any]]) -> Any:
        from hypergraph.materialization._types import SyncResult

        existing_rows = _dedup_rows(self._store.read_rows(self._spec.name), self._identity)
        existing_by_id: dict[str, dict] = {str(row[self._identity]): row for row in existing_rows if row.get(self._identity) is not None}

        incoming_ids: set[str] = set()
        inserted = 0
        updated = 0
        skipped = 0

        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for item in items:
            id_val = str(item[self._identity])
            incoming_ids.add(id_val)

            existing = existing_by_id.get(id_val)
            if existing is None:
                await self._insert_one_async(item, write_gen)
                inserted += 1
            else:
                graph_inputs = self._extract_graph_inputs(item)
                new_fp = self._compute_row_fingerprint(item, graph_inputs)
                if existing.get("_row_fingerprint") == new_fp:
                    skipped += 1
                else:
                    changes = {k: v for k, v in item.items() if k != self._identity}
                    await self._update_async(id_val, **changes)
                    updated += 1

        deleted = 0
        for id_val in existing_by_id:
            if id_val not in incoming_ids:
                await self._delete_async(id_val)
                deleted += 1

        return SyncResult(inserted=inserted, updated=updated, deleted=deleted, skipped=skipped, errored=0)

    def recompute(self, column: str) -> None:
        """Re-derive one column for all rows using current bound components."""
        self._require_runner()
        self._ensure_analyzed()

        rows = _dedup_rows(self._store.read_rows(self._spec.name), self._identity)
        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for existing in rows:
            id_val = existing[self._identity]

            graph_inputs = {}
            for c in self._spec.columns:
                if c.role == "source" and c.name in existing:
                    graph_inputs[c.name] = _normalize_value(existing[c.name])

            result = self._runner.run(self._graph, **graph_inputs)
            outputs = self._extract_outputs(result)

            new_row = {k: _normalize_value(v) for k, v in existing.items()}
            if column in outputs:
                new_row[column] = outputs[column]
            new_row["_write_gen"] = write_gen
            new_row[f"_provenance_{column}"] = self._compute_provenance(column, graph_inputs, outputs)

            self._store.write_rows(self._spec.name, [new_row])
            self._store.delete_rows(
                self._spec.name,
                [
                    (self._identity, "eq", id_val),
                    ("_write_gen", "lt", write_gen),
                ],
            )

    def backfill(self, column: str) -> None:
        """Derive a new column for existing rows that have NULL."""
        self._require_runner()
        self._ensure_analyzed()

        # Evolve schema if the column doesn't exist yet
        sample = self._store.read_rows(self._spec.name, limit=1)
        if sample and column not in sample[0]:
            col_type = self._get_derived_column_type(column)
            self._store.evolve_schema(
                self._spec.name,
                {
                    column: col_type,
                    f"_provenance_{column}": str,
                },
            )

        rows = _dedup_rows(self._store.read_rows(self._spec.name), self._identity)
        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for existing in rows:
            val = existing.get(column)
            if val is not None and not (isinstance(val, float) and math.isnan(val)):
                continue

            id_val = existing[self._identity]

            graph_inputs = {}
            for c in self._spec.columns:
                if c.role == "source" and c.name in existing:
                    graph_inputs[c.name] = _normalize_value(existing[c.name])

            result = self._runner.run(self._graph, **graph_inputs)
            outputs = self._extract_outputs(result)

            new_row = {k: _normalize_value(v) for k, v in existing.items()}
            if column in outputs:
                new_row[column] = outputs[column]
            new_row["_write_gen"] = write_gen
            new_row[f"_provenance_{column}"] = self._compute_provenance(column, graph_inputs, outputs)

            self._store.write_rows(self._spec.name, [new_row])
            self._store.delete_rows(
                self._spec.name,
                [
                    (self._identity, "eq", id_val),
                    ("_write_gen", "lt", write_gen),
                ],
            )

    # --- Fingerprint and provenance ---

    def _compute_row_fingerprint(self, item: dict, graph_inputs: dict) -> str:
        node_hashes = []
        for n in self._graph.iter_nodes():
            func = getattr(n, "func", None)
            if func is not None:
                node_hashes.append(compute_definition_hash(func))

        component_hashes = {}
        for name, comp in self._components.items():
            config = getattr(comp, "__component_config__", None) or (comp._config() if hasattr(comp, "_config") else None)
            if config is not None:
                component_hashes[name] = str(config)

        payload = json.dumps(
            {
                "inputs": {k: str(v) for k, v in sorted(graph_inputs.items())},
                "nodes": sorted(node_hashes),
                "components": component_hashes,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _compute_provenance(self, col_name: str, inputs: dict, outputs: dict) -> str:
        payload = json.dumps(
            {
                "column": col_name,
                "inputs": {k: str(v) for k, v in sorted(inputs.items())},
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()
