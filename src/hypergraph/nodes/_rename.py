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


def build_reverse_rename_map(
    rename_history: list[RenameEntry],
    kind: Literal["inputs", "outputs"] = "inputs",
) -> dict[str, str]:
    """Build a reverse mapping from current names to original names.

    Handles rename chaining correctly:
    - Sequential renames (different batches): a->x then x->z → z maps to a
    - Parallel renames (same batch): x->y, y->z → y maps to x, z maps to y

    Args:
        rename_history: List of RenameEntry from node._rename_history
        kind: Which type of renames to process ("inputs" or "outputs")

    Returns:
        Dict mapping current names to original names.
        Names that were never renamed won't appear in the dict.
    """
    entries = [e for e in rename_history if e.kind == kind]
    if not entries:
        return {}

    # Group entries by batch_id
    batches: dict[int | None, list[RenameEntry]] = {}
    for entry in entries:
        batches.setdefault(entry.batch_id, []).append(entry)

    reverse_map: dict[str, str] = {}

    # Process batches in order (by first occurrence in history)
    for batch_id in dict.fromkeys(e.batch_id for e in entries):
        batch_entries = batches[batch_id]
        # For parallel renames (same batch), compute originals using the
        # reverse_map state BEFORE this batch, not during
        batch_updates = {}
        for entry in batch_entries:
            # Look up original from previous batches only
            original = reverse_map.get(entry.old, entry.old)
            batch_updates[entry.new] = original
        # Apply all updates from this batch at once
        reverse_map.update(batch_updates)

    return reverse_map
