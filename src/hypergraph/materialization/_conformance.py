"""Behavioral conformance harness for TableStore implementations.

``validate_store()`` is a cheap shape check (isinstance + ``open``). This harness
goes further: it drives a store through the *observable* contract HyperTable
relies on — newest-generation reads, the full predicate operator set,
delete-by-predicate counts, schema evolution, and parent/child filtering — and
raises with a precise message naming every invariant the store violates.

Use it against a fresh, empty store in your own test suite::

    from hypergraph.materialization import check_store_conformance
    from hypergraph.materialization._lancedb_store import LanceDBStore

    def test_my_store_conforms(tmp_path):
        check_store_conformance(LanceDBStore(str(tmp_path / "store")))

The checks assert only *observable* behavior, so they pass for both Arrow-native
stores (which use the Arrow types) and schemaless stores (which ignore them).
Only the required abstract methods are exercised; optional methods (``search``,
``save_manifest`` / ``load_manifest``) are out of scope.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pyarrow as pa

from hypergraph.materialization._schema import ColumnSpec, TableSpec
from hypergraph.materialization._table_store import TableStore


def _spec(name: str, *, identity: str = "cid", parent_link: bool = False) -> TableSpec:
    """A minimal valid table: identity + one int source column + reserved columns."""
    cols = [
        ColumnSpec(identity, role="identity", arrow_type=pa.utf8()),
        ColumnSpec("n", role="source", content_key=True, arrow_type=pa.int64()),
    ]
    if parent_link:
        cols.append(ColumnSpec("_parent_id", role="parent_link", arrow_type=pa.utf8()))
    cols += [
        ColumnSpec("_row_fingerprint", role="internal", arrow_type=pa.utf8()),
        ColumnSpec("_write_gen", role="internal", arrow_type=pa.int64()),
        ColumnSpec("_status", role="internal", arrow_type=pa.utf8()),
        ColumnSpec("_error", role="internal", arrow_type=pa.utf8()),
    ]
    return TableSpec(name=name, identity=identity, columns=cols, parent_link="_parent_id" if parent_link else None)


def _row(idval: str, n: int, write_gen: int, *, idcol: str = "cid", **extra: Any) -> dict[str, Any]:
    return {
        idcol: idval,
        "n": n,
        "_row_fingerprint": f"fp{write_gen}",
        "_write_gen": write_gen,
        "_status": "complete",
        "_error": None,
        **extra,
    }


def _ids(rows: list[dict[str, Any]], key: str = "cid") -> set[str]:
    return {r[key] for r in rows}


# --- individual invariant checks; each raises AssertionError with a precise message ---


def _check_open_and_roundtrip(store: TableStore) -> None:
    cols = store.open(_spec("c_roundtrip"), [])
    assert "c_roundtrip" in cols, "open() must return a {table_name: [columns]} mapping that includes the root table"
    store.write_rows("c_roundtrip", [_row("a", 1, 1)])
    got = store.read_one("c_roundtrip", "cid", "a")
    assert got is not None, "read_one must find a row that was just written"
    assert got["n"] == 1, f"read_one returned the wrong data for the written row: {got!r}"
    assert store.count("c_roundtrip") >= 1, "count() must reflect a written row"
    assert store.read_one("c_roundtrip", "cid", "missing") is None, "read_one must return None for an unknown identity"


def _check_read_one_returns_newest_generation(store: TableStore) -> None:
    store.open(_spec("c_newest"), [])
    store.write_rows("c_newest", [_row("x", 10, 1)])
    store.write_rows("c_newest", [_row("x", 20, 2)])
    got = store.read_one("c_newest", "cid", "x")
    assert got is not None and got["n"] == 20, (
        "read_one must return the highest _write_gen when one identity has multiple generations "
        f"(crash-leftover dedup). Got {got!r}, expected the n=20 / _write_gen=2 row."
    )


def _check_predicate_operators(store: TableStore) -> None:
    store.open(_spec("c_ops"), [])
    store.write_rows("c_ops", [_row("a", 1, 1), _row("b", 2, 1), _row("c", 3, 1)])

    def rr(where: list) -> set[str]:
        return _ids(store.read_rows("c_ops", where))

    expected = [
        ("eq", [("n", "eq", 2)], {"b"}),
        ("ne", [("n", "ne", 2)], {"a", "c"}),
        ("lt", [("n", "lt", 2)], {"a"}),
        ("lte", [("n", "lte", 2)], {"a", "b"}),
        ("gt", [("n", "gt", 2)], {"c"}),
        ("gte", [("n", "gte", 2)], {"b", "c"}),
        ("in", [("n", "in", [1, 3])], {"a", "c"}),
    ]
    for op, where, want in expected:
        got = rr(where)
        assert got == want, f"read_rows operator {op!r} is wrong: got {got}, expected {want}"

    assert _ids(store.read_rows("c_ops", limit=1)) <= {"a", "b", "c"}, "read_rows must honor `limit`"
    assert len(store.read_rows("c_ops", limit=1)) == 1, "read_rows `limit=1` must return at most one row"


def _check_delete_returns_count(store: TableStore) -> None:
    store.open(_spec("c_del"), [])
    store.write_rows("c_del", [_row("a", 1, 1), _row("b", 2, 1), _row("c", 3, 1)])
    deleted = store.delete_rows("c_del", [("n", "gte", 2)])
    assert deleted == 2, f"delete_rows must return the number of rows deleted; got {deleted}, expected 2"
    assert _ids(store.read_rows("c_del")) == {"a"}, "delete_rows must actually remove the matched rows"


def _check_string_quotes(store: TableStore) -> None:
    store.open(_spec("c_quotes"), [])
    store.write_rows("c_quotes", [_row("O'Brien", 1, 1)])
    got = store.read_one("c_quotes", "cid", "O'Brien")
    assert got is not None and got["n"] == 1, "read_one must handle identity values containing a single quote"
    deleted = store.delete_rows("c_quotes", [("cid", "eq", "O'Brien")])
    assert deleted == 1, f"delete_rows must handle a single quote in a string value; got {deleted}, expected 1"
    assert store.read_one("c_quotes", "cid", "O'Brien") is None, "the row must be gone after delete"


def _check_max_write_gen(store: TableStore) -> None:
    store.open(_spec("c_gen"), [])
    assert store.max_write_gen("c_gen") == 0, "max_write_gen must be 0 for an empty table"
    store.write_rows("c_gen", [_row("a", 1, 1), _row("b", 2, 2), _row("c", 3, 5)])
    assert store.max_write_gen("c_gen") == 5, "max_write_gen must return the highest persisted _write_gen"


def _check_evolve_schema(store: TableStore) -> None:
    store.open(_spec("c_evolve"), [])
    store.write_rows("c_evolve", [_row("a", 1, 1)])
    cols = store.evolve_schema("c_evolve", {"extra": pa.utf8()})
    assert "extra" in cols, "evolve_schema must return the table's column names, including the newly added column"
    store.write_rows("c_evolve", [_row("a", 1, 2, extra="hello")])
    got = store.read_one("c_evolve", "cid", "a")
    assert got is not None and got.get("extra") == "hello", f"a row written after evolve_schema must round-trip the new column; got {got!r}"


def _check_parent_child_filter(store: TableStore) -> None:
    store.open(_spec("c_parent"), [_spec("c_child", identity="kid", parent_link=True)])
    store.write_rows(
        "c_child",
        [
            _row("k1", 1, 1, idcol="kid", _parent_id="p1"),
            _row("k2", 2, 1, idcol="kid", _parent_id="p1"),
            _row("k3", 3, 1, idcol="kid", _parent_id="p2"),
        ],
    )
    kids = _ids(store.read_rows("c_child", [("_parent_id", "eq", "p1")]), key="kid")
    assert kids == {"k1", "k2"}, f"read_rows with a _parent_id predicate must scope to one parent; got {kids}, expected {{'k1', 'k2'}}"


_CHECKS: list[Callable[[TableStore], None]] = [
    _check_open_and_roundtrip,
    _check_read_one_returns_newest_generation,
    _check_predicate_operators,
    _check_delete_returns_count,
    _check_string_quotes,
    _check_max_write_gen,
    _check_evolve_schema,
    _check_parent_child_filter,
]


def check_store_conformance(store: TableStore) -> None:
    """Drive a fresh, empty ``store`` through the TableStore behavioral contract.

    Raises ``AssertionError`` listing every invariant the store violates (or
    ``TypeError`` if it isn't a ``TableStore``). Each check uses its own table, so
    a single store instance is fine — just make sure it starts empty.
    """
    if not isinstance(store, TableStore):
        raise TypeError(f"store must subclass TableStore, got {type(store).__name__}")

    failures: list[str] = []
    for check in _CHECKS:
        label = check.__name__.removeprefix("_check_")
        try:
            check(store)
        except AssertionError as exc:
            failures.append(f"  - {label}: {exc}")
        except Exception as exc:  # a crash is itself a conformance failure
            failures.append(f"  - {label}: raised {type(exc).__name__}: {exc}")

    if failures:
        raise AssertionError("TableStore conformance failed:\n" + "\n".join(failures))
