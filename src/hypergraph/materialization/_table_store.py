"""Storage protocol for HyperTable — decouples table logic from any specific database."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol, runtime_checkable

RowOperator = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in"]
RowPredicate = Sequence[tuple[str, RowOperator, Any]]


@runtime_checkable
class TableStore(Protocol):
    """Abstract storage backend for HyperTable."""

    def open(self, spec: Any, children: list[Any]) -> dict[str, list[str]]:
        """Ensure physical tables exist. Returns {table_name: [column_names]}."""
        ...

    def count(self, table_name: str) -> int: ...

    def read_rows(self, table_name: str, where: RowPredicate | None = None, *, limit: int | None = None) -> list[dict[str, Any]]: ...

    def read_one(self, table_name: str, identity_column: str, identity_value: Any) -> dict[str, Any] | None: ...

    def write_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None: ...

    def delete_rows(self, table_name: str, where: RowPredicate) -> int: ...

    def max_write_gen(self, table_name: str) -> int: ...

    def evolve_schema(self, table_name: str, new_columns: dict[str, Any]) -> list[str]: ...
