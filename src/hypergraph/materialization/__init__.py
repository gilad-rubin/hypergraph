"""Declarative incremental materialization with DerivedTable and HyperTable."""

from hypergraph.materialization._hypertable import HyperTable
from hypergraph.materialization._markers import ContentKey, Identity
from hypergraph.materialization._table import DerivedTable
from hypergraph.materialization._types import (
    ChainedTableError,
    DerivationError,
    ErrorRow,
    SyncResult,
)

__all__ = [
    "Identity",
    "ContentKey",
    "DerivedTable",
    "HyperTable",
    "ErrorRow",
    "SyncResult",
    "DerivationError",
    "ChainedTableError",
]
