"""Graph-native incremental materialization with HyperTable."""

from __future__ import annotations

from hypergraph.materialization._hypertable import HyperTable
from hypergraph.materialization._table_store import TableStore, validate_store
from hypergraph.materialization._types import ErrorRow, SyncResult

__all__ = [
    "HyperTable",
    "TableStore",
    "validate_store",
    "ErrorRow",
    "SyncResult",
]
