"""Declarative incremental materialization with DerivedTable and HyperTable."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from hypergraph.materialization._hypertable import HyperTable
from hypergraph.materialization._markers import ContentKey, Identity
from hypergraph.materialization._table_store import TableStore
from hypergraph.materialization._types import (
    ChainedTableError,
    DerivationError,
    ErrorRow,
    SyncResult,
)

if TYPE_CHECKING:
    from hypergraph.materialization._table import DerivedTable

__all__ = [
    "Identity",
    "ContentKey",
    "DerivedTable",
    "HyperTable",
    "TableStore",
    "ErrorRow",
    "SyncResult",
    "DerivationError",
    "ChainedTableError",
]


def __getattr__(name: str):
    if name == "DerivedTable":
        mod = importlib.import_module("hypergraph.materialization._table")
        return mod.DerivedTable
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
