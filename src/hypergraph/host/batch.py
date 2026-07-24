"""Batch value objects and validation for the durable host.

A Batch is an immutable manifest of unique logical item keys, each mapped to
one independent child Run (PRD 0019). This module owns the pinned tolerance
declaration and submission-time item validation; trip semantics
(strictly-exceeds evaluation, PARTIAL) land with the tolerance ticket.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BatchTolerance:
    """Pinned failure tolerance declared at Batch acceptance.

    Attributes:
        max_failed: Maximum failure-equivalent children tolerated, or None.
        max_failed_percent: Maximum failure-equivalent children as a whole
            percentage (0-100) of the total manifest item count, or None.
            The denominator is fixed at acceptance; it never changes during
            execution.

    At least one threshold must be set. Values are pinned into the Batch
    manifest at acceptance (they are part of the dedup fingerprint) and are
    never ``serve()`` configuration. Trip evaluation lands with the
    tolerance ticket; today the declaration is persisted verbatim.
    """

    max_failed: int | None = None
    max_failed_percent: int | None = None

    def __post_init__(self) -> None:
        if self.max_failed is None and self.max_failed_percent is None:
            raise ValueError("BatchTolerance requires at least one of max_failed or max_failed_percent.")
        if self.max_failed is not None and (isinstance(self.max_failed, bool) or not isinstance(self.max_failed, int) or self.max_failed < 0):
            raise ValueError(f"BatchTolerance.max_failed must be an int >= 0 or None, got {self.max_failed!r}.")
        if self.max_failed_percent is not None and (
            isinstance(self.max_failed_percent, bool) or not isinstance(self.max_failed_percent, int) or not 0 <= self.max_failed_percent <= 100
        ):
            raise ValueError(f"BatchTolerance.max_failed_percent must be an int between 0 and 100 or None, got {self.max_failed_percent!r}.")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for storage (both keys always present)."""
        return {"max_failed": self.max_failed, "max_failed_percent": self.max_failed_percent}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchTolerance:
        """Rebuild a BatchTolerance from ``to_dict()`` output."""
        if not isinstance(data, dict):
            raise TypeError(f"BatchTolerance.from_dict() expects a dict, got {type(data).__name__}.")
        return cls(max_failed=data.get("max_failed"), max_failed_percent=data.get("max_failed_percent"))


def validate_batch_items(items: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Validate a Batch manifest and return ``(item_key, inputs_json)`` pairs.

    Keys must be unique, non-empty strings; each item's inputs must be a
    JSON-serializable Mapping. The returned list preserves the caller's
    mapping order — that order is the manifest order used for keyed outcomes
    and unstarted-item listings.
    """
    if not isinstance(items, Mapping):
        raise TypeError(f"submit_batch() items must be a Mapping of item key to inputs, got {type(items).__name__}.")
    pairs = list(items.items())
    if not pairs:
        raise ValueError("submit_batch() requires at least one item; an empty Batch is not a Batch.")
    seen: set[str] = set()
    validated: list[tuple[str, str]] = []
    for key, value in pairs:
        if not isinstance(key, str) or not key:
            raise ValueError(f"submit_batch() item keys must be non-empty strings, got {key!r}.")
        if key in seen:
            raise ValueError(f"submit_batch() duplicate item key {key!r}; every logical item key must be unique.")
        seen.add(key)
        if not isinstance(value, Mapping):
            raise TypeError(f"submit_batch() item {key!r} inputs must be a Mapping, got {type(value).__name__}.")
        try:
            inputs_json = json.dumps(dict(value))
        except (TypeError, ValueError):
            raise TypeError(f"submit_batch() item {key!r} inputs must be JSON-serializable.") from None
        validated.append((key, inputs_json))
    return validated
