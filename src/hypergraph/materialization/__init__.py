"""Declarative incremental materialization with DerivedTable."""

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
    "ErrorRow",
    "SyncResult",
    "DerivationError",
    "ChainedTableError",
]
