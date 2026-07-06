"""Table: a durable typed table — identity + store + schema. No nodes, no provenance.

The promoted form of what used to be spelled ``HyperTable(nodes=[])``: an
append-only log or fact table whose rows are never derived. Downstream
projects (uploads logs, birth-certificate meta rows) build on exactly these
semantics, so they are pinned here as a first-class class instead of resting
on a derivation substrate's degenerate mode.

Semantics (identical to the old plain mode, byte-compatible on disk — same
physical table name, same internal columns, same write generations):

- ``insert`` is insert-if-absent BY IDENTITY: re-inserting an existing
  identity is a no-op even when field values differ. ``update`` is the
  explicit change verb.
- Metadata columns evolve on first sight, exactly as HyperTable metadata does.
- No runner ceremony: a Table derives nothing, so callers never configure one.

Table WRAPS the HyperTable machinery (constructed through the private
``_plain`` flag with an internal SyncRunner) rather than subclassing it: a
Table is not substitutable for a HyperTable — its insert appends inertly
while HyperTable's insert derives — so inheritance would be a Liskov lie.
"""

from __future__ import annotations

from typing import Any


class Table:
    """A durable typed table: identity + store + schema handling, zero derivation."""

    def __init__(self, *, identity: str, store: Any):
        from hypergraph.materialization._hypertable import HyperTable
        from hypergraph.runners import SyncRunner

        self._identity = identity
        self._impl = HyperTable([], identity=identity, store=store, _plain=True).with_runner(SyncRunner())
        # The underlying store, exposed for callers that compose raw reads
        # (e.g. a fixed-identity certificate row read).
        self._store = store

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def table_name(self) -> str:
        """The physical table name (identity minus the ``_id`` suffix)."""
        return self._impl.table_name

    def insert(self, *args, **kwargs) -> Any:
        """Insert rows if their identity is absent; existing identities are untouched."""
        return self._impl.insert(*args, **kwargs)

    def update(self, identity_value: str, **changes: Any) -> Any:
        """Change stored fields on one row — the explicit change verb."""
        return self._impl.update(identity_value, **changes)

    def delete(self, identity_value: str) -> Any:
        """Delete one row by identity."""
        return self._impl.delete(identity_value)

    def get(self, identity_value: str) -> dict[str, Any] | None:
        """One public row by identity, or None."""
        return self._impl.get(identity_value)

    def filter(self, where: Any = None, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Public rows matching a store predicate."""
        return self._impl.filter(where, limit=limit)

    def count(self) -> int:
        """Row count (newest generation per identity)."""
        return self._impl.count()
