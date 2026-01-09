"""Rename tracking utilities for node transformations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RenameEntry:
    """Tracks a single rename operation for error messages.

    Attributes:
        kind: Which attribute was renamed ("name", "inputs", or "outputs")
        old: Original value before rename
        new: New value after rename
    """

    kind: Literal["name", "inputs", "outputs"]
    old: str
    new: str


class RenameError(Exception):
    """Raised when a rename operation references a non-existent name.

    The error message includes context from rename history to help
    users understand what happened (e.g., if the name was already renamed).
    """

    pass


def _apply_renames(
    values: tuple[str, ...],
    mapping: dict[str, str] | None,
    kind: Literal["inputs", "outputs"],
) -> tuple[tuple[str, ...], list[RenameEntry]]:
    """Apply renames to a tuple, returning (new_values, history).

    Args:
        values: Original tuple of names
        mapping: Optional {old: new} rename mapping
        kind: Type of rename for history tracking

    Returns:
        Tuple of (renamed_values, history_entries)

    Note:
        Does NOT validate that old names exist in values.
        Validation is handled by _with_renamed at rename time.
    """
    if not mapping:
        return values, []

    history = [RenameEntry(kind, old, new) for old, new in mapping.items()]
    return tuple(mapping.get(v, v) for v in values), history
