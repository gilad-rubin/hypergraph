"""SQLite-backed TableStore: ``Table`` and ``HyperTable`` on stdlib ``sqlite3``, no lancedb."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hypergraph.checkpointers.sqlite import SQLITE_BUSY_TIMEOUT_MS, _ensure_wal
from hypergraph.materialization._table_store import RowPredicate, TableStore, _cas_next_row, _physical_columns

if TYPE_CHECKING:
    import pyarrow as pa

_MEMORY = ":memory:"

# A column's kind is spelled as its declared SQLite type, so any process reads it
# back from PRAGMA table_info. Each spelling keeps the affinity the kind needs:
# JSON_TEXT contains TEXT (a plain "JSON" would get NUMERIC affinity and turn the
# text "123" into 123), and BOOLEAN's NUMERIC affinity keeps the stored 0/1.
_DECLARED = {"text": "TEXT", "int": "INTEGER", "real": "REAL", "bool": "BOOLEAN", "blob": "BLOB", "json": "JSON_TEXT"}
_KIND_OF_DECLARED = {declared: kind for kind, declared in _DECLARED.items()}
_KIND_LABEL = {
    "text": "utf8 (str)",
    "int": "int64 (int)",
    "real": "float64 (float or int)",
    "bool": "bool",
    "blob": "large_binary (bytes)",
}
_ACCEPTS: dict[str, Callable[[Any], bool]] = {
    "text": lambda value: isinstance(value, str),
    "int": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "real": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "bool": lambda value: isinstance(value, bool),
    "blob": lambda value: isinstance(value, bytes),
    "any": lambda value: True,  # a column this store did not declare: stored as given
}
_COMPARISONS = {"eq": "=", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
_OPERATORS = (*_COMPARISONS, "in")


def _q(identifier: str) -> str:
    """A quoted SQL identifier; values never go through here, they are bound."""
    return '"' + identifier.replace('"', '""') + '"'


def _kind(arrow_type: pa.DataType) -> str:
    import pyarrow as pa

    if pa.types.is_boolean(arrow_type):
        return "bool"
    if pa.types.is_integer(arrow_type):
        return "int"
    if pa.types.is_floating(arrow_type):
        return "real"
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return "text"
    if pa.types.is_binary(arrow_type) or pa.types.is_large_binary(arrow_type):
        return "blob"
    return "json"


def _plain(value: Any) -> Any:
    """numpy scalars and arrays as plain Python, without importing numpy."""
    np = sys.modules.get("numpy")
    if np is not None:
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.generic):
            return value.item()
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return value


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _cell(table: str, column: str, kind: str, value: Any) -> Any:
    """The SQLite value stored for one cell; a value of the wrong type is refused."""
    value = _plain(value)
    if value is None:
        return None
    if kind == "json":
        try:
            return json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"SqliteTableStore cannot store {type(value).__name__} {_short(value)} in column {column!r} of table {table!r}.\n\n"
                f"The column is declared JSON, and the value is not JSON-serializable ({exc}); nothing from this call was written.\n\n"
                f"How to fix: store lists, dicts, str, int, float, bool or None in {column!r}."
            ) from None
    if not _ACCEPTS[kind](value):
        raise TypeError(
            f"SqliteTableStore cannot store {type(value).__name__} {_short(value)} in column {column!r} of table {table!r}.\n\n"
            f"The column is declared {_KIND_LABEL[kind]}; nothing from this call was written.\n\n"
            f"How to fix: write a {_KIND_LABEL[kind]} value to {column!r}, or put this value under a new column name."
        )
    return value


def _operand(kind: str, value: Any) -> Any:
    """A predicate value as SQLite compares it to a stored cell."""
    value = _plain(value)
    if value is not None and kind == "json":
        return json.dumps(value)
    return value


def _decode(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "json":
        return json.loads(value)
    if kind == "bool":
        return bool(value)
    return value


def _check_operators(where: RowPredicate | None) -> None:
    for column, op, _value in where or ():
        if op not in _OPERATORS:
            raise ValueError(
                f"SqliteTableStore cannot filter column {column!r} with operator {op!r}.\n\n"
                f"A row predicate uses one of: {', '.join(_OPERATORS)}.\n\n"
                f"How to fix: express the filter with one of those operators."
            )


def _where(kinds: dict[str, str], where: RowPredicate | None) -> tuple[str, list[Any]] | None:
    """The WHERE clause and its parameters, or None when no row can match.

    A predicate on a column the table does not have matches nothing, as on
    LanceDBStore: HyperTable sends child-table predicates here without knowing
    every child column.
    """
    clauses: list[str] = []
    params: list[Any] = []
    for column, op, value in where or ():
        if column not in kinds:
            return None
        kind = kinds[column]
        if op != "in":
            clauses.append(f"{_q(column)} {_COMPARISONS[op]} ?")
            params.append(_operand(kind, value))
            continue
        values = [_operand(kind, item) for item in value]
        if not values:
            return None
        if kind == "blob":  # JSON cannot carry bytes
            clauses.append(f"{_q(column)} IN ({', '.join(['?'] * len(values))})")
            params.extend(values)
        else:  # one parameter however long the list, never SQLite's variable limit
            clauses.append(f"{_q(column)} IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(values))
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _case_clash(name: str, existing: Any, what: str, table: str | None = None) -> ValueError | None:
    """A refusal when ``existing`` holds a name equal to ``name`` apart from letter case."""
    clash = next((other for other in existing if other.lower() == name.lower()), None)
    if clash is None:
        return None
    place = f" in table {table!r}" if table is not None else ""
    return ValueError(
        f"SqliteTableStore cannot create {what} {name!r}{place}: {clash!r} already exists.\n\n"
        f"SQLite table and column names ignore letter case, so the two would be one {what}.\n\n"
        f"How to fix: rename one of them so the names differ by more than letter case."
    )


class SqliteTableStore(TableStore):
    """Tables in one SQLite file (or ``":memory:"``, one database per instance).

    Each Arrow type maps to a SQLite column: bool, integer, floating, string and
    binary types to their native columns; any other type (lists, structs) is
    stored as JSON text and decoded on read. A value of the wrong type for its
    column raises ``TypeError`` and nothing from that call is written. A key that
    is not a column is ignored, as on ``LanceDBStore``. A NaN float reads back as
    ``None``, because SQLite stores NaN as NULL.

    Every method is one transaction, and ``compare_and_set`` is atomic across
    threads and processes. ``Table.append`` is still a read followed by a write
    inside ``Table``, so it is not atomic here either. Threads share one
    connection behind a lock, so a read waits while this store is writing; a file
    store in WAL mode waits up to 30 s for another process's write.

    No search and no manifests (named indexes and branches fail loudly when used).
    Needs pyarrow, as ``Table`` and ``HyperTable`` already do; never imports lancedb.
    Construction does no I/O; ``open()`` creates the file and its parent directory.
    Call ``close()`` when done.
    """

    thread_safe = True

    def __init__(self, path: str | os.PathLike[str] = _MEMORY) -> None:
        self._path = os.fspath(path)
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self._lock = threading.RLock()

    def close(self) -> None:
        """Close the connection. Idempotent; the store cannot be used afterwards."""
        with self._lock:
            self._closed = True
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # --- connection and transactions ---

    def _connection(self, *, create: bool) -> sqlite3.Connection | None:
        """The store's one connection, or None for a read of a file not created yet."""
        if self._closed:
            raise RuntimeError(
                f"SqliteTableStore({self._path!r}) is closed.\n\n"
                "close() released its connection, so it cannot read or write any more.\n\n"
                "How to fix: create a new SqliteTableStore for the same path."
            )
        if self._conn is None:
            on_disk = self._path != _MEMORY
            if on_disk and not create and not os.path.exists(self._path):
                return None
            if on_disk:
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
            try:
                conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
                if on_disk:
                    _ensure_wal(conn)
            except BaseException:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    @contextlib.contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection | None]:
        with self._lock:
            yield self._connection(create=False)

    @contextlib.contextmanager
    def _writing(self, *, create: bool = False) -> Iterator[sqlite3.Connection | None]:
        """One write transaction: the store lock, BEGIN IMMEDIATE before any read
        that decides the write, COMMIT on normal exit, ROLLBACK on any exception."""
        with self._lock:
            db = self._connection(create=create)
            if db is None:
                yield None
                return
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        db.execute("ROLLBACK")
                raise

    # --- schema ---

    @staticmethod
    def _columns(db: sqlite3.Connection | None, table: str) -> dict[str, str] | None:
        """Column name -> kind for a table with exactly this name, or None."""
        if db is None or db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is None:
            return None
        return {row[1]: _KIND_OF_DECLARED.get(row[2], "any") for row in db.execute(f"PRAGMA table_info({_q(table)})")}

    def open(self, spec: Any, children: list[Any]) -> dict[str, list[str]]:
        opened: dict[str, list[str]] = {}
        with self._writing(create=True) as db:
            assert db is not None  # create=True always connects
            for table in (spec, *children):
                kinds = self._columns(db, table.name)
                if kinds is None:
                    kinds = self._create(db, table)
                opened[table.name] = list(kinds)
        return opened

    def _create(self, db: sqlite3.Connection, spec: Any) -> dict[str, str]:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        clash = _case_clash(spec.name, tables, "table")
        if clash is not None:
            raise clash
        kinds: dict[str, str] = {}
        for name, arrow_type in _physical_columns(spec):
            clash = _case_clash(name, kinds, "column", spec.name)
            if clash is not None:
                raise clash
            kinds[name] = _kind(arrow_type)
        declared = ", ".join(f"{_q(name)} {_DECLARED[kind]}" for name, kind in kinds.items())
        db.execute(f"CREATE TABLE {_q(spec.name)} ({declared})")
        return kinds

    def column_names(self, table_name: str) -> list[str]:
        with self._reading() as db:
            return list(self._columns(db, table_name) or ())

    def evolve_schema(self, table_name: str, new_columns: dict[str, pa.DataType]) -> list[str]:
        with self._writing() as db:
            kinds = self._columns(db, table_name)
            if db is None or kinds is None:
                raise KeyError(f"table {table_name!r} is not open; call open() before evolve_schema()")
            return list(self._add_columns(db, table_name, kinds, new_columns))

    def _add_columns(self, db: sqlite3.Connection, table: str, kinds: dict[str, str], new_columns: dict[str, pa.DataType]) -> dict[str, str]:
        """Add the columns the table lacks; one it already holds is skipped."""
        kinds = dict(kinds)
        for name, arrow_type in new_columns.items():
            if name in kinds:
                continue
            clash = _case_clash(name, kinds, "column", table)
            if clash is not None:
                raise clash
            kinds[name] = _kind(arrow_type)
            db.execute(f"ALTER TABLE {_q(table)} ADD COLUMN {_q(name)} {_DECLARED[kinds[name]]}")
        return kinds

    # --- reads ---

    def count(self, table_name: str) -> int:
        with self._reading() as db:
            if db is None or self._columns(db, table_name) is None:
                return 0
            return int(db.execute(f"SELECT COUNT(*) FROM {_q(table_name)}").fetchone()[0])

    def read_rows(
        self, table_name: str, where: RowPredicate | None = None, *, limit: int | None = None, columns: list[str] | None = None
    ) -> list[dict[str, Any]]:
        _check_operators(where)
        with self._reading() as db:
            kinds = self._columns(db, table_name)
            if db is None or kinds is None:
                return []
            fetch = self._projection(table_name, kinds, columns)
            clause = _where(kinds, where)
            if clause is None:
                return []
            sql_where, params = clause
            sql = f"SELECT {self._select_list(fetch)} FROM {_q(table_name)}{sql_where} ORDER BY rowid"
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            return [self._row(fetch, kinds, values) for values in db.execute(sql, params)]

    def read_one(self, table_name: str, identity_column: str, identity_value: Any, *, columns: list[str] | None = None) -> dict[str, Any] | None:
        with self._reading() as db:
            kinds = self._columns(db, table_name)
            if db is None or kinds is None:
                return None
            return self._newest(db, table_name, kinds, identity_column, identity_value, columns)

    def _newest(
        self, db: sqlite3.Connection, table: str, kinds: dict[str, str], identity_column: str, identity_value: Any, columns: list[str] | None
    ) -> dict[str, Any] | None:
        """The highest-generation row for an identity; a tie goes to the first written, as on LanceDBStore."""
        fetch = self._projection(table, kinds, columns)
        clause = _where(kinds, [(identity_column, "eq", identity_value)])
        if clause is None:
            return None
        sql_where, params = clause
        order = ' ORDER BY "_write_gen" DESC, rowid' if "_write_gen" in kinds else " ORDER BY rowid"
        found = db.execute(f"SELECT {self._select_list(fetch)} FROM {_q(table)}{sql_where}{order} LIMIT 1", params).fetchone()
        return None if found is None else self._row(fetch, kinds, found)

    def max_write_gen(self, table_name: str) -> int:
        with self._reading() as db:
            kinds = self._columns(db, table_name)
            if db is None or kinds is None:
                return 0
            return self._max_write_gen(db, table_name, kinds)

    @staticmethod
    def _max_write_gen(db: sqlite3.Connection, table: str, kinds: dict[str, str]) -> int:
        if "_write_gen" not in kinds:
            return 0
        return int(db.execute(f'SELECT COALESCE(MAX("_write_gen"), 0) FROM {_q(table)}').fetchone()[0])

    @staticmethod
    def _projection(table: str, kinds: dict[str, str], columns: list[str] | None) -> list[str]:
        if columns is None:
            return list(kinds)
        unknown = [name for name in columns if name not in kinds]
        if unknown:
            raise KeyError(f"read requested unknown column(s) {unknown} on table {table!r}; available columns: {sorted(kinds)}")
        return list(dict.fromkeys(columns))

    @staticmethod
    def _select_list(fetch: list[str]) -> str:
        return ", ".join(_q(name) for name in fetch) or "1"

    @staticmethod
    def _row(fetch: list[str], kinds: dict[str, str], values: Any) -> dict[str, Any]:
        if not fetch:  # an empty projection selected the constant 1
            return {}
        return {name: _decode(kinds[name], value) for name, value in zip(fetch, values, strict=True)}

    # --- writes ---

    def write_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self._writing() as db:
            kinds = self._columns(db, table_name)
            if db is None or kinds is None:
                raise KeyError(f"table {table_name!r} is not open; call open() before write_rows()")
            self._insert(db, table_name, kinds, rows)

    @staticmethod
    def _insert(db: sqlite3.Connection, table: str, kinds: dict[str, str], rows: list[dict[str, Any]]) -> None:
        """Insert every row with every column; a missing key is NULL, a non-column key is ignored."""
        names = list(kinds)
        params = [tuple(_cell(table, name, kinds[name], row.get(name)) for name in names) for row in rows]
        db.executemany(
            f"INSERT INTO {_q(table)} ({', '.join(_q(name) for name in names)}) VALUES ({', '.join(['?'] * len(names))})",
            params,
        )

    def delete_rows(self, table_name: str, where: RowPredicate) -> int:
        _check_operators(where)
        with self._writing() as db:
            kinds = self._columns(db, table_name)
            if db is None or kinds is None:
                return 0
            clause = _where(kinds, where)
            if clause is None:
                return 0
            sql_where, params = clause
            return int(db.execute(f"DELETE FROM {_q(table_name)}{sql_where}", params).rowcount)

    def compare_and_set(
        self,
        table_name: str,
        identity_column: str,
        identity_value: Any,
        expected: dict[str, Any],
        changes: dict[str, Any],
        new_columns: dict[str, pa.DataType],
    ) -> bool:
        """Compare, evolve, write and drop older generations in one transaction."""
        with self._writing() as db:
            kinds = self._columns(db, table_name)
            if db is None or kinds is None:
                return False
            existing = self._newest(db, table_name, kinds, identity_column, identity_value, None)
            write_gen = self._max_write_gen(db, table_name, kinds) + 1
            row = _cas_next_row(existing, expected, changes, write_gen)
            if row is None:
                return False
            if new_columns:
                kinds = self._add_columns(db, table_name, kinds, new_columns)
            self._insert(db, table_name, kinds, [row])
            clause = _where(kinds, [(identity_column, "eq", identity_value), ("_write_gen", "lt", write_gen)])
            if clause is not None:
                db.execute(f"DELETE FROM {_q(table_name)}{clause[0]}", clause[1])
            return True
