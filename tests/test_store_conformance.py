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

    def column_names(self, table_name: str) -> list[str]:
        return list(self._schemas.get(table_name, []))

    def evolve_schema(self, table_name: str, new_columns: dict[str, pa.DataType]) -> list[str]:
        known = self._schemas.setdefault(table_name, [])
        # Idempotent: only append columns the tracked schema does not already have.
        for name in new_columns:
            if name not in known:
                known.append(name)
        return list(known)


def test_lancedb_store_conforms(tmp_path) -> None:
    check_store_conformance(LanceDBStore(str(tmp_path / "conformance_store")))


def test_lancedb_reads_see_another_connection_write(tmp_path) -> None:
    """A read must reflect a commit made by a SEPARATE store on the same folder.

    Two ``LanceDBStore`` objects over one path model the KB gate flow: an operator
    holds one KB handle while a decision resolver builds its own handle and writes
    (a replace bumps ``version`` to 2). LanceDB caches a ``Table`` object pinned to
    the version it was opened at, so without advancing the cached handle the
    operator's reads would keep returning the stale ``version=1`` — a silent lie.
    Reads must ``checkout_latest`` so committed data is always visible.
    """
    from hypergraph.materialization._schema import ColumnSpec, TableSpec

    path = str(tmp_path / "cross_conn_store")
    spec = TableSpec(
        name="t",
        identity="cid",
        columns=[
            ColumnSpec("cid", role="identity", arrow_type=pa.utf8()),
            ColumnSpec("version", role="source", arrow_type=pa.int64()),
            ColumnSpec("_write_gen", role="internal", arrow_type=pa.int64()),
            ColumnSpec("_status", role="internal", arrow_type=pa.utf8()),
            ColumnSpec("_row_fingerprint", role="internal", arrow_type=pa.utf8()),
            ColumnSpec("_error", role="internal", arrow_type=pa.utf8()),
        ],
    )
    base_row = {"_status": "complete", "_row_fingerprint": "fp", "_error": None}

    operator = LanceDBStore(path)
    operator.open(spec, [])
    operator.write_rows("t", [{"cid": "a", "version": 1, "_write_gen": 1, **base_row}])
    # The operator reads once, which pins its cached Table handle.
    assert operator.read_one("t", "cid", "a")["version"] == 1

    # A separate store (the resolver) commits a version bump on the same folder.
    resolver = LanceDBStore(path)
    resolver.open(spec, [])
    resolver.write_rows("t", [{"cid": "a", "version": 2, "_write_gen": 2, **base_row}])

    # The operator's reads must now see version 2, not the pinned version 1.
    assert operator.read_one("t", "cid", "a")["version"] == 2, "read_one must see another connection's commit"
    newest = max(
        operator.read_rows("t", [("cid", "eq", "a")], columns=["cid", "version", "_write_gen"]),
        key=lambda r: r["_write_gen"],
    )
    assert newest["version"] == 2, "read_rows must see another connection's commit"
    assert operator.count("t") == 2, "count must see another connection's commit"
    assert operator.max_write_gen("t") == 2, "max_write_gen must see another connection's commit"


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
