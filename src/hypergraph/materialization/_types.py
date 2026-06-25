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
