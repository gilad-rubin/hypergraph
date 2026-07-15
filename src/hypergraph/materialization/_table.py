"""A durable typed table with no derivation graph."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from hypergraph.materialization._provenance import normalize_value
from hypergraph.materialization._schema import ColumnSpec, TableSpec, is_internal_column, python_type_to_arrow
from hypergraph.materialization._table_store import TableStore
from hypergraph.materialization._types import RowReceipt, RowStatus, TableReceipt, WriteOutcome


def _predicate(where: Any) -> list[tuple[str, str, Any]]:
    if where is None:
        return []
    if isinstance(where, dict):
        return [(name, "eq", value) for name, value in where.items()]
    return list(where)


class Table:
    """A durable append-only fact table: identity, store, and schema evolution."""

    def __init__(self, *, identity: str, store: TableStore, name: str | None = None):
        if not isinstance(store, TableStore):
            raise TypeError(f"store must be a TableStore instance, got {type(store)}")
        self._identity = identity
        self._store = store
        self._spec = TableSpec(
            name=name or identity.replace("_id", ""),
            identity=identity,
            columns=[
                ColumnSpec(identity, role="identity", arrow_type=python_type_to_arrow(str)),
                ColumnSpec("_row_fingerprint", role="internal", arrow_type=python_type_to_arrow(str)),
                ColumnSpec("_write_gen", role="internal", arrow_type=python_type_to_arrow(int)),
                ColumnSpec("_status", role="internal", arrow_type=python_type_to_arrow(str)),
                ColumnSpec("_error", role="internal", arrow_type=python_type_to_arrow(str)),
                ColumnSpec("_question", role="internal", arrow_type=python_type_to_arrow(str)),
            ],
        )
        self._store.open(self._spec, [])

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def table_name(self) -> str:
        return self._spec.name

    @staticmethod
    def _items(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        if args and isinstance(args[0], list):
            return args[0], False
        if kwargs:
            return [kwargs], True
        raise ValueError("append() requires keyword columns or a list of row dicts")

    def _evolve(self, item: dict[str, Any]) -> None:
        known = set(self._store.column_names(self.table_name))
        additions = {
            name: python_type_to_arrow(type(value) if value is not None else str)
            for name, value in item.items()
            if name not in known and name != self._identity
        }
        if additions:
            self._store.evolve_schema(self.table_name, additions)

    def append(self, *args: Any, **kwargs: Any) -> RowReceipt | TableReceipt:
        """Append rows whose identities are absent; existing identities are skipped."""
        items, single = self._items(*args, **kwargs)
        write_gen = self._store.max_write_gen(self.table_name) + 1
        receipts: list[RowReceipt] = []
        for item in items:
            identity_value = item[self._identity]
            if self._store.read_one(self.table_name, self._identity, identity_value) is not None:
                receipts.append(RowReceipt(str(identity_value), WriteOutcome.SKIPPED, RowStatus.COMPLETE))
                continue
            self._evolve(item)
            row = dict(item)
            payload = json.dumps(item, sort_keys=True, default=repr).encode()
            row.update(
                _row_fingerprint=hashlib.sha256(payload).hexdigest(),
                _write_gen=write_gen,
                _status="complete",
                _error=None,
                _question=None,
            )
            self._store.write_rows(self.table_name, [row])
            receipts.append(RowReceipt(str(identity_value), WriteOutcome.INSERTED, RowStatus.COMPLETE))
        result = TableReceipt(tuple(receipts))
        return result.receipts[0] if single else result

    def update(self, identity_value: str, **changes: Any) -> RowReceipt:
        existing = self._store.read_one(self.table_name, self._identity, identity_value)
        if existing is None:
            raise KeyError(identity_value)
        self._evolve({self._identity: identity_value, **changes})
        write_gen = self._store.max_write_gen(self.table_name) + 1
        row = {name: normalize_value(value) for name, value in existing.items()}
        row.update(changes)
        row["_write_gen"] = write_gen
        self._store.write_rows(self.table_name, [row])
        self._store.delete_rows(
            self.table_name,
            [(self._identity, "eq", identity_value), ("_write_gen", "lt", write_gen)],
        )
        return RowReceipt(str(identity_value), WriteOutcome.UPDATED, RowStatus.COMPLETE)

    def delete(self, identity_value: str) -> None:
        self._store.delete_rows(self.table_name, [(self._identity, "eq", identity_value)])

    def get(self, identity_value: str) -> dict[str, Any] | None:
        row = self._store.read_one(self.table_name, self._identity, identity_value)
        if row is None:
            return None
        return {name: normalize_value(value) for name, value in row.items() if not is_internal_column(name)}

    def rows(self, where: Any = None, *, limit: int | None = None) -> list[dict[str, Any]]:
        rows = self._store.read_rows(self.table_name, _predicate(where), limit=limit)
        return [{name: normalize_value(value) for name, value in row.items() if not is_internal_column(name)} for row in rows]

    def count(self) -> int:
        return len(self.rows())
