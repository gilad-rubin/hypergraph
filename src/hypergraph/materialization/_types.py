"""Core types for materialization."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorRow:
    """A row that failed derivation."""

    identity: dict
    error_type: str
    error_msg: str


@dataclass(frozen=True)
class SyncResult:
    """Counts returned by sync()."""

    inserted: int
    updated: int
    deleted: int
    skipped: int
    errored: int
    errors: tuple[ErrorRow, ...] = ()


@dataclass(frozen=True)
class TableStatus:
    """Dry-run staleness report for one table, returned by status().

    A row is stale when its stored fingerprint no longer matches
    hash(stored source values + current node code + current component
    configs) — the recipe or the content changed after the row was written.
    Errored rows are reported separately; both re-derive on the next sync.
    """

    table: str
    total: int
    fresh: int
    stale: int
    errored: int
    stale_ids: tuple[str, ...] = ()
    errored_ids: tuple[str, ...] = ()
    stale_columns: tuple[tuple[str, int], ...] = ()
    children: tuple[TableStatus, ...] = ()

    @property
    def is_fresh(self) -> bool:
        """True when no row here or in any child table would re-derive."""
        return self.stale == 0 and self.errored == 0 and all(child.is_fresh for child in self.children)
