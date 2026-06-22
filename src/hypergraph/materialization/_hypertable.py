"""HyperTable: a Hypergraph graph where each node output is a stored column."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import lancedb
import pyarrow as pa

from hypergraph import Graph
from hypergraph.materialization._keys import compute_definition_hash


def _normalize_value(v: Any) -> Any:
    """Convert numpy/arrow types back to Python-native for the public API."""
    import numpy as np

    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    return v


def _python_type_to_arrow(tp: type) -> pa.DataType:
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
    if hasattr(tp, "__origin__"):
        origin = tp.__origin__
        if origin is list:
            args = getattr(tp, "__args__", ())
            if args and args[0] is float:
                return pa.list_(pa.float32())
            if args and args[0] is str:
                return pa.list_(pa.utf8())
            if args and args[0] is int:
                return pa.list_(pa.int64())
            return pa.list_(pa.utf8())
    return pa.utf8()


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    role: str  # identity, source, derived, parent_link, internal
    produced_by: Any = None
    content_key: bool = False


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
        store: str,
        vector_columns: dict[str, int] | None = None,
        _components: dict[str, Any] | None = None,
        _runner: Any | None = None,
        _graph: Graph | None = None,
    ):
        self._nodes = nodes
        self._identity = identity
        self._vector_columns = vector_columns or {}
        self._store_uri = store
        self._components = _components or {}
        self._runner = _runner
        self._graph = _graph
        self._spec: TableSpec | None = None
        self._db: Any = None
        self._tables: dict[str, Any] = {}
        self._analyzed = False

    def bind(self, **components: Any) -> HyperTable:
        merged = {**self._components, **components}
        return HyperTable(
            self._nodes,
            identity=self._identity,
            store=self._store_uri,
            vector_columns=self._vector_columns,
            _components=merged,
            _runner=self._runner,
        )

    def with_runner(self, runner: Any) -> HyperTable:
        return HyperTable(
            self._nodes,
            identity=self._identity,
            store=self._store_uri,
            vector_columns=self._vector_columns,
            _components=self._components,
            _runner=runner,
        )

    def _ensure_analyzed(self):
        if self._analyzed:
            return
        self._build_graph()
        self._analyze_graph()
        self._open_store()
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
        graph = self._graph
        root_columns = []
        child_specs = []

        root_columns.append(ColumnSpec(self._identity, role="identity"))

        required = set(graph.inputs.required) if isinstance(graph.inputs.required, tuple) else set(graph.inputs.required.keys())

        for inp_name in sorted(required):
            if inp_name == self._identity:
                continue
            root_columns.append(ColumnSpec(inp_name, role="source", content_key=True))

        for map_node in getattr(self, "_map_over_nodes", []):
            child_spec = self._analyze_map_over(map_node)
            if child_spec:
                child_specs.append(child_spec)

        child_map_inputs = {cs.map_input for cs in child_specs if cs.map_input}

        nodes_dict = graph.nodes if isinstance(graph.nodes, dict) else {}
        for _name, n in nodes_dict.items():
            for out_name in n.data_outputs if hasattr(n, "data_outputs") else ():
                if out_name not in child_map_inputs:
                    root_columns.append(ColumnSpec(out_name, role="derived", produced_by=n))

        derived_cols = [c for c in root_columns if c.role == "derived"]
        prov_cols = [ColumnSpec(f"_provenance_{c.name}", role="internal") for c in derived_cols]
        final_columns = root_columns + [ColumnSpec("_row_fingerprint", role="internal")] + prov_cols + [ColumnSpec("_write_gen", role="internal")]

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
            ColumnSpec(identity, role="identity"),
            ColumnSpec("_parent_id", role="parent_link"),
        ]

        if inner_graph:
            inner_required = (
                set(inner_graph.inputs.required) if isinstance(inner_graph.inputs.required, tuple) else set(inner_graph.inputs.required.keys())
            )
            for inp_name in sorted(inner_required):
                if inp_name != identity:
                    child_columns.append(ColumnSpec(inp_name, role="source", content_key=True))
            nodes_dict = inner_graph.nodes if isinstance(inner_graph.nodes, dict) else {}
            for _name, n in nodes_dict.items():
                for out_name in n.data_outputs if hasattr(n, "data_outputs") else []:
                    child_columns.append(ColumnSpec(out_name, role="derived", produced_by=n))

        child_columns.append(ColumnSpec("_row_fingerprint", role="internal"))
        child_columns.append(ColumnSpec("_write_gen", role="internal"))

        table_name = identity.replace("_id", "")
        return TableSpec(
            name=table_name,
            identity=identity,
            columns=child_columns,
            parent_link="_parent_id",
            child_graph=inner_graph,
            map_input=map_input,
        )

    def _open_store(self):
        path = self._store_uri.replace("lancedb://", "")
        self._db = lancedb.connect(path)
        self._ensure_physical_table(self._spec)
        for child in self._spec.children:
            self._ensure_physical_table(child)

    def _build_arrow_schema(self, spec: TableSpec) -> pa.Schema:
        fields = []
        for col in spec.columns:
            if col.role == "internal":
                if col.name == "_write_gen":
                    fields.append(pa.field(col.name, pa.int64()))
                else:
                    fields.append(pa.field(col.name, pa.utf8()))
            elif col.role in ("identity", "parent_link", "source"):
                fields.append(pa.field(col.name, pa.utf8()))
            elif col.role == "derived":
                if col.name in self._vector_columns:
                    dim = self._vector_columns[col.name]
                    fields.append(pa.field(col.name, pa.list_(pa.float32(), dim)))
                else:
                    node_obj = col.produced_by
                    func = getattr(node_obj, "func", None) or getattr(node_obj, "_func", None)
                    if func:
                        import typing

                        hints = typing.get_type_hints(func)
                        ret_type = hints.get("return", str)
                        fields.append(pa.field(col.name, _python_type_to_arrow(ret_type)))
                    else:
                        fields.append(pa.field(col.name, pa.utf8()))
        return pa.schema(fields)

    def _ensure_physical_table(self, spec: TableSpec):
        try:
            tbl = self._db.open_table(spec.name)
        except Exception:
            schema = self._build_arrow_schema(spec)
            tbl = self._db.create_table(spec.name, schema=schema)
        self._tables[spec.name] = tbl

    def _require_runner(self):
        if self._runner is None:
            raise RuntimeError("No runner set. Call .with_runner(SyncRunner()) before write operations.")

    # --- Public API ---

    def count(self, child_table: str | None = None) -> int:
        self._ensure_analyzed()
        if child_table:
            for child in self._spec.children:
                if child.name == child_table:
                    tbl = self._tables.get(child.name)
                    if tbl is None:
                        return 0
                    return len(tbl.to_pandas())
            return 0
        tbl = self._tables.get(self._spec.name)
        if tbl is None:
            return 0
        return len(tbl.to_pandas())

    def get(self, identity_value: str) -> dict[str, Any] | None:
        self._ensure_analyzed()
        tbl = self._tables[self._spec.name]
        df = tbl.to_pandas()
        matches = df[df[self._identity] == identity_value]
        if matches.empty:
            return None
        row = matches.iloc[0].to_dict()
        internal_prefixes = ("_row_fingerprint", "_provenance_", "_write_gen")
        return {k: _normalize_value(v) for k, v in row.items() if not any(k.startswith(p) or k == p for p in internal_prefixes)}

    def children(self, parent_id: str) -> list[dict[str, Any]]:
        self._ensure_analyzed()
        if not self._spec.children:
            return []
        child_spec = self._spec.children[0]
        tbl = self._tables.get(child_spec.name)
        if tbl is None:
            return []
        df = tbl.to_pandas()
        matches = df[df["_parent_id"] == parent_id]
        results = []
        for _, row in matches.iterrows():
            d = row.to_dict()
            clean = {}
            for k, v in d.items():
                if k.startswith("_provenance_") or k in ("_row_fingerprint", "_write_gen"):
                    continue
                clean[k] = _normalize_value(v)
            results.append(clean)
        return results

    def insert(self, *args, **kwargs):
        self._require_runner()
        self._ensure_analyzed()

        if args and isinstance(args[0], list):
            items = args[0]
        elif kwargs:
            items = [kwargs]
        else:
            raise ValueError("insert() requires kwargs or a list of dicts")

        write_gen = self._next_write_gen()

        for item in items:
            self._insert_one(item, write_gen)

    def _next_write_gen(self) -> int:
        tbl = self._tables[self._spec.name]
        df = tbl.to_pandas()
        if df.empty:
            return 1
        return int(df["_write_gen"].max()) + 1

    def _insert_one(self, item: dict[str, Any], write_gen: int):
        identity_value = item[self._identity]

        graph = self._graph
        graph_inputs = {}
        metadata = {}

        required = set(graph.inputs.required) if isinstance(graph.inputs.required, tuple) else set(graph.inputs.required.keys())

        for k, v in item.items():
            if k == self._identity:
                continue
            if k in required:
                graph_inputs[k] = v
            else:
                metadata[k] = v

        result = self._runner.run(graph, **graph_inputs)

        if hasattr(result, "values") and isinstance(result.values, dict):
            outputs = result.values
        elif isinstance(result, dict):
            outputs = result
        else:
            outputs = {}

        row = {self._identity: identity_value}
        row.update({k: v for k, v in item.items() if k != self._identity})

        derived_cols = [c for c in self._spec.columns if c.role == "derived"]
        for col in derived_cols:
            if col.name in outputs:
                row[col.name] = outputs[col.name]

        fingerprint = self._compute_row_fingerprint(item, graph_inputs)
        row["_row_fingerprint"] = fingerprint
        row["_write_gen"] = write_gen

        for col in derived_cols:
            prov = self._compute_provenance(col.name, graph_inputs, outputs)
            row[f"_provenance_{col.name}"] = prov

        tbl = self._tables[self._spec.name]

        new_meta_cols = [k for k in metadata if k not in [f.name for f in tbl.schema]]
        if new_meta_cols:
            new_fields = list(tbl.schema) + [pa.field(k, pa.utf8()) for k in new_meta_cols]
            new_schema = pa.schema(new_fields)
            self._db.drop_table(self._spec.name)
            tbl = self._db.create_table(self._spec.name, schema=new_schema)
            self._tables[self._spec.name] = tbl

        schema = tbl.schema
        for field_obj in schema:
            if field_obj.name not in row:
                row[field_obj.name] = None

        self._write_row(tbl, row, schema)

        for child_spec in self._spec.children:
            self._insert_children(identity_value, outputs, child_spec, write_gen)

    def _write_row(self, tbl, row: dict, schema: pa.Schema):
        arrays = []
        for field_obj in schema:
            val = row.get(field_obj.name)
            if val is None:
                arrays.append(pa.array([None], type=field_obj.type))
            elif pa.types.is_list(field_obj.type) and isinstance(val, list):
                inner_type = field_obj.type.value_type
                inner_arr = pa.array(val, type=inner_type)
                arrays.append(pa.array([inner_arr], type=field_obj.type))
            else:
                arrays.append(pa.array([val], type=field_obj.type))
        record_batch = pa.record_batch(arrays, schema=schema)
        tbl.add(record_batch)

    def _insert_children(self, parent_id: str, outputs: dict, child_spec: TableSpec, write_gen: int):
        child_graph = child_spec.child_graph
        if not child_graph:
            return

        child_items = outputs.get(child_spec.map_input)
        if not child_items or not isinstance(child_items, list):
            return

        child_tbl = self._tables[child_spec.name]

        for child_item in child_items:
            child_identity = child_item.get(child_spec.identity, "")
            child_inputs = {}
            for col in child_spec.columns:
                if col.role == "source" and col.content_key and col.name in child_item:
                    child_inputs[col.name] = child_item[col.name]

            bound_graph = child_graph.bind(**self._components) if self._components else child_graph

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

            schema = child_tbl.schema
            for field_obj in schema:
                if field_obj.name not in child_row:
                    child_row[field_obj.name] = None

            self._write_row(child_tbl, child_row, schema)

    def _compute_row_fingerprint(self, item: dict, graph_inputs: dict) -> str:
        node_hashes = []
        for n in self._graph.nodes:
            if hasattr(n, "fn"):
                node_hashes.append(compute_definition_hash(n.fn))

        component_hashes = {}
        for name, comp in self._components.items():
            if hasattr(comp, "_config"):
                component_hashes[name] = str(comp._config())

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
