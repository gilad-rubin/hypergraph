"""Run the TableStore conformance harness against real and minimal stores.

Two stores are checked:

1. ``LanceDBStore`` — the reference backend, which pushes column projection
   down into LanceDB natively.
2. ``DictTableStore`` — a minimal in-memory store that does NOT implement native
   projection; it fetches full rows and defers to ``TableStore._project_rows``.
   Passing the identical harness proves the base-class projection default is
   correct, so an external store conforms just by accepting the ``columns=``
   kwarg and calling the helper — no native pushdown required.

A store author's own test suite should mirror the LanceDBStore test.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from hypergraph.materialization import check_store_conformance
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.materialization._table_store import RowPredicate, TableStore

_PY_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "in": lambda a, b: a in b,
}


class DictTableStore(TableStore):
    """A minimal, schemaless in-memory TableStore backed by row dicts.

    It implements the required contract with plain Python and relies on the
    base-class ``_project_rows`` default for column projection — deliberately no
    native pushdown, so the conformance harness exercises the fallback path.
    """

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._schemas: dict[str, list[str]] = {}
        self._manifests: dict[str, dict[str, Any]] = {}

    def open(self, spec: Any, children: list[Any]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for s in [spec, *children]:
            self._tables.setdefault(s.name, [])
            self._schemas[s.name] = [c.name for c in s.columns]
            result[s.name] = list(self._schemas[s.name])
        return result

    def count(self, table_name: str) -> int:
        return len(self._tables.get(table_name, []))

    def _matches(self, row: dict[str, Any], where: RowPredicate | None) -> bool:
        return all(_PY_OPS[op](row.get(col), val) for col, op, val in where or ())

    def read_rows(
        self, table_name: str, where: RowPredicate | None = None, *, limit: int | None = None, columns: list[str] | None = None
    ) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self._tables.get(table_name, []) if self._matches(r, where)]
        if limit is not None:
            rows = rows[:limit]
        # No native projection — defer to the base-class default.
        return self._project_rows(rows, columns)

    def read_one(self, table_name: str, identity_column: str, identity_value: Any, *, columns: list[str] | None = None) -> dict[str, Any] | None:
        matches = [r for r in self._tables.get(table_name, []) if r.get(identity_column) == identity_value]
        if not matches:
            return None
        newest = max(matches, key=lambda r: r.get("_write_gen", 0))
        return self._project_rows([dict(newest)], columns)[0]

    def write_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        known = self._schemas.setdefault(table_name, [])
        for row in rows:
            for key in row:
                if key not in known:
                    known.append(key)
            self._tables.setdefault(table_name, []).append(dict(row))

    def delete_rows(self, table_name: str, where: RowPredicate) -> int:
        rows = self._tables.get(table_name, [])
        keep = [r for r in rows if not self._matches(r, where)]
        deleted = len(rows) - len(keep)
        self._tables[table_name] = keep
        return deleted

    def max_write_gen(self, table_name: str) -> int:
        rows = self._tables.get(table_name, [])
        return max((r.get("_write_gen", 0) for r in rows), default=0)

    def evolve_schema(self, table_name: str, new_columns: dict[str, pa.DataType]) -> list[str]:
        known = self._schemas.setdefault(table_name, [])
        for name in new_columns:
            if name not in known:
                known.append(name)
        return list(known)


def test_lancedb_store_conforms(tmp_path) -> None:
    check_store_conformance(LanceDBStore(str(tmp_path / "conformance_store")))


def test_lancedb_projection_unknown_column_fails_loudly(tmp_path) -> None:
    """A schema-aware store must reject a projection naming a column it does not have."""
    from hypergraph.materialization._schema import ColumnSpec, TableSpec

    store = LanceDBStore(str(tmp_path / "unknown_col_store"))
    spec = TableSpec(
        name="t",
        identity="cid",
        columns=[
            ColumnSpec("cid", role="identity", arrow_type=pa.utf8()),
            ColumnSpec("content", role="source", content_key=True, arrow_type=pa.large_binary()),
            ColumnSpec("_write_gen", role="internal", arrow_type=pa.int64()),
            ColumnSpec("_status", role="internal", arrow_type=pa.utf8()),
            ColumnSpec("_row_fingerprint", role="internal", arrow_type=pa.utf8()),
            ColumnSpec("_error", role="internal", arrow_type=pa.utf8()),
        ],
    )
    store.open(spec, [])
    store.write_rows(
        "t",
        [{"cid": "a", "content": b"\x00\xff", "_write_gen": 1, "_status": "complete", "_row_fingerprint": "fp", "_error": None}],
    )
    try:
        store.read_rows("t", columns=["cid", "nope"])
    except KeyError as exc:
        assert "nope" in str(exc), f"the error must name the unknown column; got {exc!r}"
    else:
        raise AssertionError("read_rows must fail loudly on an unknown projected column, not silently drop it")


def test_minimal_store_conforms_via_base_projection() -> None:
    """A store with no native projection conforms through TableStore._project_rows."""
    store = DictTableStore()
    assert store.supports_column_projection(), "the minimal store accepts columns= and should advertise projection support"
    check_store_conformance(store)
