"""LanceDB-backed TableStore implementation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa
import pyarrow.compute as pc

from hypergraph.materialization._schema import TableSpec
from hypergraph.materialization._table_store import RowPredicate, TableStore

_PC_OPS = {
    "eq": pc.equal,
    "ne": pc.not_equal,
    "lt": pc.less,
    "lte": pc.less_equal,
    "gt": pc.greater,
    "gte": pc.greater_equal,
}


def _apply_arrow_predicate(table: pa.Table, where: RowPredicate) -> pa.Table:
    """Filter a PyArrow Table by a RowPredicate."""
    col_names = set(table.column_names)
    for col, op, val in where:
        if col not in col_names:
            return table.slice(0, 0)
        column = table.column(col)
        mask = pc.is_in(column, value_set=pa.array(val)) if op == "in" else _PC_OPS[op](column, val)
        table = table.filter(mask)
    return table


def _arrow_table_to_dicts(table: pa.Table) -> list[dict[str, Any]]:
    """Convert a PyArrow Table to a list of row dicts."""
    if len(table) == 0:
        return []
    pydict = table.to_pydict()
    cols = list(pydict.keys())
    return [{c: pydict[c][i] for c in cols} for i in range(len(table))]


def _sql_literal(val: Any) -> str:
    """Render a predicate value as a LanceDB SQL literal, escaping quotes in strings."""
    if isinstance(val, str):
        return "'" + val.replace("'", "''") + "'"
    return str(val)


def _build_lance_filter(where: RowPredicate) -> str:
    """Convert RowPredicate to a LanceDB SQL-like filter string."""
    op_map = {"eq": "=", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
    clauses = []
    for col, op, val in where:
        if op == "in":
            items = ", ".join(_sql_literal(v) for v in val)
            clauses.append(f"`{col}` IN ({items})")
        else:
            clauses.append(f"`{col}` {op_map[op]} {_sql_literal(val)}")
    return " AND ".join(clauses)


class LanceDBStore(TableStore):
    """LanceDB-backed TableStore implementation."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._db = lancedb.connect(path)
        self._tables: dict[str, Any] = {}
        self._schemas: dict[str, pa.Schema] = {}
        self._vector_dims: dict[str, dict[str, int]] = {}

    def open(self, spec: TableSpec, children: list[TableSpec]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        self._ensure_table(spec)
        result[spec.name] = [f.name for f in self._tables[spec.name].schema]
        for child in children:
            self._ensure_table(child)
            result[child.name] = [f.name for f in self._tables[child.name].schema]
        return result

    def count(self, table_name: str) -> int:
        tbl = self._tables.get(table_name)
        if tbl is None:
            return 0
        return tbl.count_rows()

    def read_rows(self, table_name: str, where: RowPredicate | None = None, *, limit: int | None = None) -> list[dict[str, Any]]:
        tbl = self._tables.get(table_name)
        if tbl is None:
            return []
        at = tbl.to_arrow()
        if where:
            at = _apply_arrow_predicate(at, where)
        if limit is not None:
            at = at.slice(0, limit)
        return _arrow_table_to_dicts(at)

    def read_one(self, table_name: str, identity_column: str, identity_value: Any) -> dict[str, Any] | None:
        tbl = self._tables.get(table_name)
        if tbl is None:
            return None
        at = tbl.to_arrow()
        at = _apply_arrow_predicate(at, [(identity_column, "eq", identity_value)])
        if len(at) == 0:
            return None
        if len(at) > 1:
            indices = pc.sort_indices(at, sort_keys=[("_write_gen", "descending")])
            at = at.take(indices)
        rows = _arrow_table_to_dicts(at.slice(0, 1))
        return rows[0]

    def write_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        tbl = self._tables[table_name]
        schema = tbl.schema

        if table_name not in self._vector_dims:
            self._detect_and_fix_vectors(table_name, rows[0])
            tbl = self._tables[table_name]
            schema = tbl.schema

        for row in rows:
            for field_obj in schema:
                if field_obj.name not in row:
                    row[field_obj.name] = None

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

    def delete_rows(self, table_name: str, where: RowPredicate) -> int:
        tbl = self._tables.get(table_name)
        if tbl is None:
            return 0
        at = tbl.to_arrow()
        matching = len(_apply_arrow_predicate(at, where))
        if matching == 0:
            return 0
        filter_expr = _build_lance_filter(where)
        tbl.delete(filter_expr)
        return matching

    def max_write_gen(self, table_name: str) -> int:
        tbl = self._tables.get(table_name)
        if tbl is None:
            return 0
        at = tbl.to_arrow()
        if len(at) == 0:
            return 0
        return pc.max(at.column("_write_gen")).as_py()

    def search(
        self,
        table_name: str,
        *,
        query: str | None = None,
        query_vector: list[float] | None = None,
        vector_column: str | None = None,
        where: RowPredicate | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        if query_vector is None:
            raise ValueError("LanceDBStore.search requires query_vector (text-only search is not supported in v1)")
        tbl = self._tables.get(table_name)
        if tbl is None:
            try:
                tbl = self._db.open_table(table_name)
            except Exception:
                return []
            self._tables[table_name] = tbl
        q = tbl.search(list(query_vector), vector_column_name=vector_column)
        if where:
            q = q.where(_build_lance_filter(where), prefilter=True)
        if limit is not None:
            q = q.limit(limit)
        return q.to_list()

    def save_manifest(self, table_name: str, manifest: dict[str, Any]) -> None:
        path = self._manifest_path(table_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    def load_manifest(self, table_name: str) -> dict[str, Any] | None:
        path = self._manifest_path(table_name)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def evolve_schema(self, table_name: str, new_columns: dict[str, pa.DataType]) -> list[str]:
        tbl = self._tables[table_name]
        existing_data = tbl.to_arrow()
        new_fields = list(tbl.schema) + [pa.field(name, arrow_type) for name, arrow_type in new_columns.items()]
        new_schema = pa.schema(new_fields)
        self._db.drop_table(table_name)
        tbl = self._db.create_table(table_name, schema=new_schema)
        if len(existing_data) > 0:
            for name, arrow_type in new_columns.items():
                existing_data = existing_data.append_column(name, pa.array([None] * len(existing_data), type=arrow_type))
            tbl.add(existing_data)
        self._tables[table_name] = tbl
        return [f.name for f in new_schema]

    # --- Internal ---

    def _manifest_path(self, table_name: str) -> Path:
        return self._path / f"{table_name}__manifest.json"

    def _ensure_table(self, spec: TableSpec) -> None:
        try:
            tbl = self._db.open_table(spec.name)
        except Exception:
            schema = self._build_schema(spec)
            tbl = self._db.create_table(spec.name, schema=schema)
        self._tables[spec.name] = tbl

    def _build_schema(self, spec: TableSpec) -> pa.Schema:
        fields = []
        for col in spec.columns:
            if col.role == "internal":
                if col.name == "_write_gen":
                    fields.append(pa.field(col.name, col.arrow_type or pa.int64()))
                else:
                    fields.append(pa.field(col.name, col.arrow_type or pa.utf8()))
            elif col.role in ("identity", "parent_link", "source") or col.role == "derived":
                fields.append(pa.field(col.name, col.arrow_type or pa.utf8()))
        return pa.schema(fields)

    def _detect_and_fix_vectors(self, table_name: str, row: dict[str, Any]) -> None:
        tbl = self._tables[table_name]
        schema = tbl.schema
        upgrades: dict[str, int] = {}

        for field_obj in schema:
            if pa.types.is_list(field_obj.type) and not pa.types.is_fixed_size_list(field_obj.type):
                val = row.get(field_obj.name)
                if isinstance(val, list) and len(val) > 0 and isinstance(val[0], (int, float)):
                    upgrades[field_obj.name] = len(val)

        if upgrades:
            existing_data = tbl.to_arrow()
            new_fields = []
            for field_obj in schema:
                if field_obj.name in upgrades:
                    dim = upgrades[field_obj.name]
                    new_fields.append(pa.field(field_obj.name, pa.list_(pa.float32(), dim)))
                else:
                    new_fields.append(field_obj)

            new_schema = pa.schema(new_fields)
            self._db.drop_table(table_name)
            tbl = self._db.create_table(table_name, schema=new_schema)
            if len(existing_data) > 0:
                tbl.add(existing_data)
            self._tables[table_name] = tbl

        self._vector_dims[table_name] = upgrades
