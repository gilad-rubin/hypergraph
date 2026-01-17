"""Rename tracking utilities for node transformations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


import itertools

_batch_counter = itertools.count()


@dataclass(frozen=True)
class RenameEntry:
    """Tracks a single rename operation for error messages.

    Attributes:
        kind: Which attribute was renamed ("name", "inputs", or "outputs")
        old: Original value before rename
        new: New value after rename
        batch_id: Groups entries from the same with_inputs/with_outputs call
                  (entries in the same batch should be treated as parallel transforms)
    """

    kind: Literal["name", "inputs", "outputs"]
    old: str
    new: str
    batch_id: int | None = None


def get_next_batch_id() -> int:
    """Get the next batch ID for grouping rename operations."""
    return next(_batch_counter)


class RenameError(Exception):
    """Raised when a rename operation references a non-existent name.

    The error message includes context from rename history to help
    users understand what happened (e.g., if the name was already renamed).
    """

    pass


def _validate_rename_keys(
    mapping: dict[str, str],
    values: tuple[str, ...],
    kind: Literal["inputs", "outputs"],
) -> None:
    """Validate that all rename keys exist in values.

    Args:
        mapping: {old: new} rename mapping
        values: Tuple of valid names
        kind: Type being renamed (for error messages)

    Raises:
        RenameError: If any key in mapping is not in values
    """
    valid_names = set(values)
    unknown_keys = [k for k in mapping if k not in valid_names]

    if unknown_keys:
        unknown_str = ", ".join(repr(k) for k in unknown_keys)
        valid_str = ", ".join(repr(v) for v in values) if values else "(none)"
        raise RenameError(
            f"Cannot rename unknown {kind}: {unknown_str}. "
            f"Valid {kind}: {valid_str}."
        )


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

    Raises:
        RenameError: If any key in mapping is not in values
    """
    if not mapping:
        return values, []

    _validate_rename_keys(mapping, values, kind)
    history = [RenameEntry(kind, old, new) for old, new in mapping.items()]
    return tuple(mapping.get(v, v) for v in values), history
