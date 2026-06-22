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


class DerivationError(Exception):
    """Raised by on_error='raise' after all items are processed."""

    def __init__(self, succeeded: list[dict], failed: list[dict]):
        self.succeeded = succeeded
        self.failed = failed
        super().__init__(f"{len(succeeded)} succeeded, {len(failed)} failed")


class ChainedTableError(Exception):
    """Raised when calling a root-only operation on a chained table."""

    def __init__(self, operation: str):
        super().__init__(f"Cannot call {operation}() on a chained table. Chained tables are populated via cascade only.")
