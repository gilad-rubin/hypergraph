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
        # Construction is zero-I/O: only the path is recorded. ``lancedb.connect``
        # creates the directory eagerly, so it is deferred to the first
        # store-method use — config functions construct stores without side
        # effects, and empty-store reads (no cached table handle) never connect.
        self._path = Path(path)
        self._db_handle: Any | None = None
        self._tables: dict[str, Any] = {}
        self._schemas: dict[str, pa.Schema] = {}
        self._vector_dims: dict[str, dict[str, int]] = {}

    @property
    def _db(self) -> Any:
        if self._db_handle is None:
            self._db_handle = lancedb.connect(str(self._path))
        return self._db_handle

    def open(self, spec: TableSpec, children: list[TableSpec]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        self._ensure_table(spec)
        result[spec.name] = [f.name for f in self._tables[spec.name].schema]
        for child in children:
            self._ensure_table(child)
            result[child.name] = [f.name for f in self._tables[child.name].schema]
        return result

    def _readable(self, table_name: str) -> Any | None:
        """The cached Table handle advanced to the latest committed version.

        LanceDB pins an opened ``Table`` to the dataset version it was opened at,
        so a commit made by ANOTHER connection on the same folder is invisible to
        this handle until it is advanced. Every read goes through here so a store's
        reads always reflect committed data — the KB gate flow relies on it (an
        operator's KB handle must see a resolver handle's version bump).

        A table this handle never opened may still exist on disk (a fresh handle
        over an existing store). Mirroring ``search``: an absent on-disk directory
        means "no rows have ever been written" — a documented empty ``None`` — while
        a present directory is opened read-only here (opening an existing table
        creates nothing); one that fails to open is corrupt and must surface.
        """
        tbl = self._tables.get(table_name)
        if tbl is None:
            if not self._table_dir_exists(table_name):
                return None
            tbl = self._db.open_table(table_name)
            self._tables[table_name] = tbl
        tbl.checkout_latest()
        return tbl

    def count(self, table_name: str) -> int:
        tbl = self._readable(table_name)
        if tbl is None:
            return 0
        return tbl.count_rows()

    def read_rows(
        self, table_name: str, where: RowPredicate | None = None, *, limit: int | None = None, columns: list[str] | None = None
    ) -> list[dict[str, Any]]:
        tbl = self._readable(table_name)
        if tbl is None:
            return []
        at = self._read_arrow(tbl, columns, extra=self._predicate_columns(where))
        if where:
            at = _apply_arrow_predicate(at, where)
        if limit is not None:
            at = at.slice(0, limit)
        if columns is not None:
            at = at.select([c for c in columns if c in at.column_names])
        return _arrow_table_to_dicts(at)

    def read_one(self, table_name: str, identity_column: str, identity_value: Any, *, columns: list[str] | None = None) -> dict[str, Any] | None:
        tbl = self._readable(table_name)
        if tbl is None:
            return None
        # Identity + _write_gen are always fetched: identity to match, _write_gen
        # to pick the newest generation on crash-leftover duplicates. Both are
        # dropped from the returned row if the caller did not ask for them.
        at = self._read_arrow(tbl, columns, extra=[identity_column, "_write_gen"])
        at = _apply_arrow_predicate(at, [(identity_column, "eq", identity_value)])
        if len(at) == 0:
            return None
        if len(at) > 1:
            indices = pc.sort_indices(at, sort_keys=[("_write_gen", "descending")])
            at = at.take(indices)
        at = at.slice(0, 1)
        if columns is not None:
            at = at.select([c for c in columns if c in at.column_names])
        rows = _arrow_table_to_dicts(at)
        return rows[0]

    @staticmethod
    def _predicate_columns(where: RowPredicate | None) -> list[str]:
        """Columns a predicate reads — must survive the projection so the filter can run."""
        return [col for col, _op, _val in where] if where else []

    def _read_arrow(self, tbl: Any, columns: list[str] | None, *, extra: list[str]) -> pa.Table:
        """Read an Arrow table, pushing a column projection into LanceDB when asked.

        A projected read never materializes unrequested columns (the point:
        keep ``large_binary`` blobs on disk for a metadata-only read). ``extra``
        names columns the caller needs internally (predicate columns, identity,
        ``_write_gen``) that must be present even if the caller did not list
        them; they are trimmed from the returned rows afterward. An unknown
        requested column fails loudly, naming the column and the real schema.
        """
        if columns is None:
            return tbl.to_arrow()
        available = {f.name for f in tbl.schema}
        requested = list(dict.fromkeys([*columns, *extra]))
        unknown = [c for c in requested if c not in available]
        if unknown:
            raise KeyError(f"read requested unknown column(s) {unknown} on table {tbl.name!r}; available columns: {sorted(available)}")
        projected = [c for c in requested if c in available]
        return tbl.to_lance().to_table(columns=projected)

    def write_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        tbl = self._tables[table_name]
        schema = tbl.schema

        if table_name not in self._vector_dims:
            self._detect_and_fix_vectors(table_name, rows[0])
            tbl = self._tables[table_name]
            schema = tbl.schema

        record_batches: list[pa.RecordBatch] = []
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
            record_batches.append(pa.record_batch(arrays, schema=schema))

        tbl.add(pa.Table.from_batches(record_batches, schema=schema))

    def delete_rows(self, table_name: str, where: RowPredicate) -> int:
        tbl = self._readable(table_name)
        if tbl is None:
            return 0
        predicate_columns = list(dict.fromkeys(self._predicate_columns(where)))
        available = {field.name for field in tbl.schema}
        if any(column not in available for column in predicate_columns):
            return 0
        at = self._read_arrow(tbl, predicate_columns, extra=[])
        matching = len(_apply_arrow_predicate(at, where))
        if matching == 0:
            return 0
        filter_expr = _build_lance_filter(where)
        tbl.delete(filter_expr)
        return matching

    def max_write_gen(self, table_name: str) -> int:
        tbl = self._readable(table_name)
        if tbl is None:
            return 0
        at = self._read_arrow(tbl, ["_write_gen"], extra=[])
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
        tbl = self._readable(table_name)
        if tbl is None:
            # A table LanceDB never created has no on-disk directory: that means
            # "no rows have ever been written" — a documented empty result
            # (mirrors max_write_gen's absent -> 0). But a table whose directory
            # exists yet fails to open is corrupt / unreadable / permission-denied
            # — a real failure that must surface, NOT be swallowed into []. The
            # error message alone can't tell these apart (LanceDB raises the same
            # "Table '<name>' was not found" ValueError for a corrupt table), so
            # we key off the on-disk directory, which a corrupt table still has.
            if not self._table_dir_exists(table_name):
                return []
            tbl = self._db.open_table(table_name)
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

    def column_names(self, table_name: str) -> list[str]:
        # Advance to the latest committed version so a schema evolved by another
        # connection on the same folder is visible (mirrors the read path).
        tbl = self._readable(table_name)
        if tbl is None:
            return []
        return [f.name for f in tbl.schema]

    def evolve_schema(self, table_name: str, new_columns: dict[str, pa.DataType]) -> list[str]:
        # Advance to the latest committed version first: another connection on the
        # same folder may have added a column since this handle was opened. Reading
        # the stale cached schema would keep that column in ``new_columns`` and the
        # rewrite would build a duplicate field — the exact case the idempotence
        # guarantee exists to prevent (mirrors ``column_names`` / the read path).
        tbl = self._readable(table_name)
        if tbl is None:
            raise KeyError(f"table {table_name!r} is not open; call open() before evolve_schema()")
        # Idempotent: skip any column the physical schema already holds. HyperTable
        # can ask to add a column that exists (it re-infers the "new" set from an
        # emptied table); appending it would build a schema with a duplicate field
        # and LanceDB rejects that on the next write.
        existing = {f.name for f in tbl.schema}
        new_columns = {name: arrow_type for name, arrow_type in new_columns.items() if name not in existing}
        if not new_columns:
            return [f.name for f in tbl.schema]
        new_fields = [pa.field(name, arrow_type, nullable=True) for name, arrow_type in new_columns.items()]
        tbl.add_columns(pa.schema(new_fields))
        tbl.checkout_latest()
        return [f.name for f in tbl.schema]

    # --- Internal ---

    def _manifest_path(self, table_name: str) -> Path:
        return self._path / f"{table_name}__manifest.json"

    def _table_dir_exists(self, table_name: str) -> bool:
        """Whether LanceDB has ever created this table on disk.

        LanceDB stores each table as a ``<name>.lance`` directory under the
        connection path. Absent directory = never created (no rows ever
        written); present directory that still fails to open = corrupt.
        """
        return (self._path / f"{table_name}.lance").exists()

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
