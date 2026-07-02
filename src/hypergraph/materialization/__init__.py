"""Graph-native incremental materialization with HyperTable."""

from __future__ import annotations

from hypergraph.materialization._hypertable import HyperTable
from hypergraph.materialization._table_store import TableStore, validate_store
from hypergraph.materialization._types import ErrorRow, SyncResult, TableStatus

__all__ = [
    "HyperTable",
    "TableStore",
    "validate_store",
    "ErrorRow",
    "SyncResult",
    "TableStatus",
    "check_store_conformance",
]


def __getattr__(name: str):
    # Lazy: the conformance harness imports pyarrow, so keep it off the default
    # import path (runtime consumers that never author a store shouldn't pay it).
    if name == "check_store_conformance":
        from hypergraph.materialization._conformance import check_store_conformance

        return check_store_conformance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
