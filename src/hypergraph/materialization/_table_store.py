"""Storage interface for HyperTable — decouples table logic from any database."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import pyarrow as pa

RowOperator = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in"]
RowPredicate = Sequence[tuple[str, RowOperator, Any]]


class TableStore(ABC):
    """Abstract storage backend for HyperTable."""

    @abstractmethod
    def open(self, spec: Any, children: list[Any]) -> dict[str, list[str]]:
        """Ensure physical tables exist. Returns {table_name: [column_names]}."""

    @abstractmethod
    def count(self, table_name: str) -> int:
        """Return physical row count for a table."""

    @abstractmethod
    def read_rows(self, table_name: str, where: RowPredicate | None = None, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Read rows, optionally filtered by a row predicate."""

    @abstractmethod
    def read_one(self, table_name: str, identity_column: str, identity_value: Any) -> dict[str, Any] | None:
        """Read one row by identity, returning the newest generation when duplicated."""

    @abstractmethod
    def write_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        """Append or upsert rows into the physical table."""

    @abstractmethod
    def delete_rows(self, table_name: str, where: RowPredicate) -> int:
        """Delete rows matching a predicate and return the physical count deleted."""

    @abstractmethod
    def max_write_gen(self, table_name: str) -> int:
        """Return the highest write generation currently persisted."""

    @abstractmethod
    def evolve_schema(self, table_name: str, new_columns: dict[str, pa.DataType]) -> list[str]:
        """Add columns and return the table's column names.

        ``new_columns`` maps column name to a pyarrow ``DataType``. Arrow is the
        intermediate type system: stores map Arrow to their native format (or
        ignore types when schemaless). No store performs Python-to-Arrow
        conversion — the HyperTable layer does it once before calling here.
        """

    def search(self, table_name: str, *, query: str, query_vector: list[float], **kwargs: Any) -> list[dict[str, Any]]:
        """Search is optional because not every TableStore is a retrieval adapter."""
        raise NotImplementedError("This store does not support search")

    def save_manifest(self, table_name: str, manifest: dict[str, Any]) -> None:
        """Persist table metadata when the backend supports manifests."""
        return None

    def load_manifest(self, table_name: str) -> dict[str, Any] | None:
        """Load table metadata when the backend supports manifests."""
        return None


def validate_store(store: Any) -> TableStore:
    """Validate that an external store satisfies the concrete HyperTable seam."""

    if not isinstance(store, TableStore):
        raise TypeError(f"store must subclass TableStore, got {type(store).__name__}")

    from hypergraph.materialization._schema import ColumnSpec, TableSpec

    spec = TableSpec(
        name="__validate_store",
        identity="id",
        columns=[ColumnSpec("id", role="identity"), ColumnSpec("_write_gen", role="internal")],
    )
    store.open(spec, [])
    return store
