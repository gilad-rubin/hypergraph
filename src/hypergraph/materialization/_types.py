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
class RecipeDrift:
    """Per-table recipe-drift report, returned by ``HyperTable.recipe_drift()``.

    A row DRIFTED when its stored ``_recipe_fingerprint`` stamp differs from
    the table's current recipe (node code + component configs + bound plain
    values — input values never participate). A row is UNKNOWN when it carries
    no stamp at all (written before stamping existed) — reported honestly as
    needing a re-derive, never as current. Unlike ``status()``, computing this
    reads only identity/reserved columns: content bytes never leave the disk.
    """

    table: str
    total: int
    current: int
    drifted: int
    unknown: int
    children: tuple[RecipeDrift, ...] = ()

    @property
    def stale_total(self) -> int:
        """Rows here and in child tables derived under something other than today's recipe."""
        return self.drifted + self.unknown + sum(child.stale_total for child in self.children)


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
