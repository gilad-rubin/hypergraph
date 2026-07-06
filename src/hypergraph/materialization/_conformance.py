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


def _binary_spec(name: str) -> TableSpec:
    """A table whose source and derived columns are ``large_binary`` (blob columns).

    Mirrors the one-HyperTable KB shape: a ``content`` source column carrying raw
    bytes and a derived ``thumb`` column also in bytes. Exercises that a store
    round-trips binary through write / read / update / evolve without decoding.
    """
    cols = [
        ColumnSpec("cid", role="identity", arrow_type=pa.utf8()),
        ColumnSpec("content", role="source", content_key=True, arrow_type=pa.large_binary()),
        ColumnSpec("label", role="source", content_key=True, arrow_type=pa.utf8()),
        ColumnSpec("thumb", role="derived", arrow_type=pa.large_binary()),
        ColumnSpec("_row_fingerprint", role="internal", arrow_type=pa.utf8()),
        ColumnSpec("_write_gen", role="internal", arrow_type=pa.int64()),
        ColumnSpec("_status", role="internal", arrow_type=pa.utf8()),
        ColumnSpec("_error", role="internal", arrow_type=pa.utf8()),
    ]
    return TableSpec(name=name, identity="cid", columns=cols)


# A payload with bytes that are NOT valid UTF-8 (0x80, 0xff) and an embedded NUL,
# so a store that secretly decodes-then-reencodes bytes is caught.
_BLOB = bytes([0x00, 0x01, 0x02, 0x80, 0xFF, 0xFE]) + b"\x00PDF\x00" + bytes(range(256))


def _binary_row(idval: str, content: bytes, write_gen: int, *, thumb: bytes | None = None, label: str = "x") -> dict[str, Any]:
    return {
        "cid": idval,
        "content": content,
        "label": label,
        "thumb": thumb,
        "_row_fingerprint": f"fp{write_gen}",
        "_write_gen": write_gen,
        "_status": "complete",
        "_error": None,
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


def _check_evolve_schema_is_idempotent(store: TableStore) -> None:
    """Re-evolving a column the schema already holds is a no-op, never an error.

    HyperTable can ask to add a metadata column that already exists — it re-infers
    the "new" set from an emptied table and cannot always know the physical schema.
    A store must skip the existing column rather than append a duplicate field
    (which breaks the next write). Exercised on the emptied-table shape that first
    surfaced the bug: add a column, delete every row, then re-evolve + re-insert.
    """
    store.open(_spec("c_evolve_idem"), [])
    store.write_rows("c_evolve_idem", [_row("a", 1, 1)])
    store.evolve_schema("c_evolve_idem", {"extra": pa.utf8()})
    # Empty the table — the state in which HyperTable re-infers columns wrongly.
    store.delete_rows("c_evolve_idem", [("cid", "eq", "a")])

    cols = store.evolve_schema("c_evolve_idem", {"extra": pa.utf8()})
    assert "extra" in cols, f"re-evolving an existing column must still report it in the column names; got {cols!r}"
    # Duplicate field only surfaces on the next write; this must not raise.
    store.write_rows("c_evolve_idem", [_row("b", 2, 1, extra="world")])
    got = store.read_one("c_evolve_idem", "cid", "b")
    assert got is not None and got.get("extra") == "world", f"after an idempotent re-evolve, a row must still round-trip the column; got {got!r}"


def _check_column_names(store: TableStore) -> None:
    """``column_names`` reports the physical schema, growing as it evolves.

    The default is ``[]`` (a store that cannot introspect its schema), so this
    only asserts the shape when a store returns anything: an opened table lists at
    least its identity column, and an evolved column shows up afterwards.
    """
    store.open(_spec("c_cols"), [])
    names = store.column_names("c_cols")
    if not names:
        return  # store opts out of schema introspection; evolve idempotence guards it
    assert "cid" in names, f"column_names must include the identity column of an opened table; got {names!r}"
    store.evolve_schema("c_cols", {"tag": pa.utf8()})
    assert "tag" in store.column_names("c_cols"), "column_names must reflect a column added by evolve_schema"


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


def _check_binary_source_roundtrip(store: TableStore) -> None:
    store.open(_binary_spec("c_bin"), [])
    store.write_rows("c_bin", [_binary_row("a", _BLOB, 1, thumb=b"\xde\xad\xbe\xef")])
    got = store.read_one("c_bin", "cid", "a")
    assert got is not None, "read_one must find a row with a large_binary source column"
    assert got["content"] == _BLOB, (
        f"a large_binary source column must round-trip its exact bytes (no decode/re-encode). Got {got['content']!r}, expected {_BLOB!r}"
    )
    assert got["thumb"] == b"\xde\xad\xbe\xef", f"a large_binary derived column must round-trip its bytes; got {got.get('thumb')!r}"


def _check_binary_in_updates(store: TableStore) -> None:
    store.open(_binary_spec("c_bin_upd"), [])
    store.write_rows("c_bin_upd", [_binary_row("a", _BLOB, 1)])
    new_blob = _BLOB[::-1]  # different bytes, same length
    store.write_rows("c_bin_upd", [_binary_row("a", new_blob, 2, thumb=b"\x01\x02")])
    got = store.read_one("c_bin_upd", "cid", "a")
    assert got is not None and got["content"] == new_blob, (
        f"updating a large_binary column (write a newer generation) must return the new bytes; got {got['content']!r} expected {new_blob!r}"
    )
    assert got["thumb"] == b"\x01\x02", "a large_binary column set on a later generation must round-trip"


def _check_binary_null(store: TableStore) -> None:
    store.open(_binary_spec("c_bin_null"), [])
    store.write_rows("c_bin_null", [_binary_row("a", _BLOB, 1, thumb=None)])
    got = store.read_one("c_bin_null", "cid", "a")
    assert got is not None and got.get("thumb") is None, (
        f"a null large_binary derived column must read back as None, not empty bytes; got {got.get('thumb')!r}"
    )


def _check_read_rows_column_projection(store: TableStore) -> None:
    store.open(_binary_spec("c_proj"), [])
    store.write_rows("c_proj", [_binary_row("a", _BLOB, 1, label="alpha"), _binary_row("b", _BLOB, 1, label="beta")])

    projected = store.read_rows("c_proj", columns=["cid", "label"])
    assert {r["cid"] for r in projected} == {"a", "b"}, "projected read_rows must still return every matching row"
    for r in projected:
        assert set(r.keys()) == {"cid", "label"}, f"read_rows(columns=[...]) must return ONLY the requested columns; got keys {sorted(r.keys())}"

    # A predicate on a non-projected column must still filter correctly.
    filtered = store.read_rows("c_proj", [("label", "eq", "alpha")], columns=["cid"])
    assert {r["cid"] for r in filtered} == {"a"}, "a predicate must apply even when its column is not in the projection"
    assert all(set(r.keys()) == {"cid"} for r in filtered), "the projection must hold after a predicate on a non-projected column"

    # columns=None (default) returns the full row.
    full = store.read_rows("c_proj", [("cid", "eq", "a")])
    assert full and full[0]["content"] == _BLOB, "read_rows with columns=None must return every column, including the blob"


def _check_read_one_column_projection(store: TableStore) -> None:
    store.open(_binary_spec("c_proj_one"), [])
    store.write_rows("c_proj_one", [_binary_row("a", _BLOB, 1, label="alpha")])

    got = store.read_one("c_proj_one", "cid", "a", columns=["label"])
    assert got is not None and got == {"label": "alpha"}, (
        f"read_one(columns=[...]) must return only the requested columns (blob excluded); got {got!r}"
    )

    # The identity column is always retrievable even though it is the match key.
    by_id = store.read_one("c_proj_one", "cid", "a", columns=["cid"])
    assert by_id == {"cid": "a"}, f"read_one must retrieve the identity column when it is the sole projection; got {by_id!r}"

    # Newest-generation dedup must still hold under projection (needs _write_gen internally).
    store.write_rows("c_proj_one", [_binary_row("a", _BLOB, 2, label="beta")])
    newest = store.read_one("c_proj_one", "cid", "a", columns=["label"])
    assert newest == {"label": "beta"}, f"read_one under projection must still return the newest generation; got {newest!r}"


_CHECKS: list[Callable[[TableStore], None]] = [
    _check_open_and_roundtrip,
    _check_read_one_returns_newest_generation,
    _check_predicate_operators,
    _check_delete_returns_count,
    _check_string_quotes,
    _check_max_write_gen,
    _check_evolve_schema,
    _check_evolve_schema_is_idempotent,
    _check_column_names,
    _check_parent_child_filter,
    _check_binary_source_roundtrip,
    _check_binary_in_updates,
    _check_binary_null,
]

# Column projection is a capability, not a universal requirement: a store that
# predates the ``columns=`` kwarg (its ``read_rows`` signature lacks it) still
# conforms — it is simply never handed a projection. These checks run only when
# the store advertises ``supports_column_projection()``. Any store that DOES
# accept the kwarg must satisfy them, whether it projects natively (LanceDB) or
# leans on the base-class ``_project_rows`` default.
_PROJECTION_CHECKS: list[Callable[[TableStore], None]] = [
    _check_read_rows_column_projection,
    _check_read_one_column_projection,
]


def check_store_conformance(store: TableStore) -> None:
    """Drive a fresh, empty ``store`` through the TableStore behavioral contract.

    Raises ``AssertionError`` listing every invariant the store violates (or
    ``TypeError`` if it isn't a ``TableStore``). Each check uses its own table, so
    a single store instance is fine — just make sure it starts empty.

    The column-projection checks are conditional: they run only when the store
    advertises ``supports_column_projection()`` (its read methods accept the
    ``columns=`` kwarg). A store that predates projection stays fully green.
    """
    if not isinstance(store, TableStore):
        raise TypeError(f"store must subclass TableStore, got {type(store).__name__}")

    checks = list(_CHECKS)
    if store.supports_column_projection():
        checks += _PROJECTION_CHECKS

    failures: list[str] = []
    for check in checks:
        label = check.__name__.removeprefix("_check_")
        try:
            check(store)
        except AssertionError as exc:
            failures.append(f"  - {label}: {exc}")
        except Exception as exc:  # a crash is itself a conformance failure
            failures.append(f"  - {label}: raised {type(exc).__name__}: {exc}")

    if failures:
        raise AssertionError("TableStore conformance failed:\n" + "\n".join(failures))
