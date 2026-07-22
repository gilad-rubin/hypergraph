"""Graph-native incremental materialization: HyperTable (derives) and Table (doesn't)."""

from __future__ import annotations

from hypergraph.materialization._branches import MaterializationBranch, MaterializedArtifact
from hypergraph.materialization._hypertable import ChildTable, HyperTable
from hypergraph.materialization._table import Table
from hypergraph.materialization._table_store import TableStore, validate_store
from hypergraph.materialization._types import (
    ErroredRow,
    RecipeDrift,
    RowReceipt,
    RowStatus,
    TableReceipt,
    TableStatus,
    WaitingRow,
    WriteOutcome,
)

__all__ = [
    "HyperTable",
    "ChildTable",
    "MaterializationBranch",
    "MaterializedArtifact",
    "LanceDBStore",
    "Table",
    "TableStore",
    "validate_store",
    "ErroredRow",
    "RecipeDrift",
    "RowReceipt",
    "RowStatus",
    "TableReceipt",
    "TableStatus",
    "WaitingRow",
    "WriteOutcome",
    "check_store_conformance",
]


def __getattr__(name: str):
    # Lazy: the conformance harness imports pyarrow, so keep it off the default
    # import path (runtime consumers that never author a store shouldn't pay it).
    if name == "check_store_conformance":
        from hypergraph.materialization._conformance import check_store_conformance

        return check_store_conformance
    # Lazy: lancedb is the optional [materialization] extra. Consumers that only
    # use HyperTable/Table with another store must not pay an import-time cost
    # for a dependency they never installed.
    if name == "LanceDBStore":
        from hypergraph.materialization._lancedb_store import LanceDBStore

        return LanceDBStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
