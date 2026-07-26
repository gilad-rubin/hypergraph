"""Batch value objects and validation for the durable host.

A Batch is an immutable manifest of unique logical item keys, each mapped to
one independent child Run (PRD 0019). This module owns the pinned tolerance
declaration, its strictly-exceeds trip predicate, the runner-shaped
input expansion that freezes into that manifest, and submission-time item
validation. Where a trip is evaluated (in the same transaction as the child
terminal write) and what it does (close admission, mark the rest unstarted)
belongs to the Run Home.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from hypergraph.host.errors import ItemKeyError

#: Map expansion modes, verbatim from ``runner.map`` — Batch reuses the
#: input-expansion vocabulary and nothing else from it (PRD 0017: durable
#: admission owns concurrency, Batch tolerance owns failure policy).
MapMode = Literal["zip", "product"]


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
    never ``serve()`` configuration. Both thresholds are evaluated
    independently by ``tolerance_trips``; either one exceeded trips the
    Batch.
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


def tolerance_trips(tolerance: BatchTolerance | None, *, failure_count: int, total_items: int) -> bool:
    """True when failure-equivalent children STRICTLY exceed either threshold.

    Count and percentage are evaluated independently: either one exceeded
    trips the Batch, and an exact-threshold failure count never does
    (``count == max_failed`` is tolerated, ``count == max_failed + 1`` is
    not).

    ``total_items`` is the total logical manifest item count pinned at
    acceptance — the percentage denominator, which never shrinks as items
    settle. The comparison is exact integer arithmetic
    (``failures * 100 > percent * total``) so no float rounding can move the
    boundary: 2 of 8 at 25% is tolerated, 3 of 8 is not.

    Args:
        tolerance: The Batch's pinned tolerance, or None (never trips).
        failure_count: Failure-equivalent children — failed runs and
            recovery-exhausted submissions only.
        total_items: Total logical manifest items pinned at acceptance.
    """
    if tolerance is None:
        return False
    if tolerance.max_failed is not None and failure_count > tolerance.max_failed:
        return True
    return tolerance.max_failed_percent is not None and failure_count * 100 > tolerance.max_failed_percent * total_items


def normalize_map_over(map_over: str | Sequence[str]) -> list[str]:
    """Normalize ``map_over`` to a non-empty list of distinct input names.

    A durable Batch always expands: a submission with nothing to map over is
    one Run, and ``host.submit`` is the verb for that. The vocabulary is
    ``runner.map``'s verbatim (``str`` or a sequence of names), so a caller
    never learns a second collection-expansion model.
    """
    names = [map_over] if isinstance(map_over, str) else list(map_over)
    if not names:
        raise ValueError(
            "submit_batch() requires map_over to name at least one input to expand.\n\n"
            "How to fix: pass map_over='work_item_id' (or a list of input names). "
            "A submission with nothing to expand is one Run — use host.submit(graph, values)."
        )
    for name in names:
        if not isinstance(name, str) or not name:
            raise ValueError(f"submit_batch() map_over entries must be non-empty input-name strings, got {name!r}.")
    if len(set(names)) != len(names):
        raise ValueError(f"submit_batch() map_over names an input twice: {names!r}. Each expanded input is named once.")
    return names


def _require_key_input(key_by: str, map_over: Sequence[str]) -> None:
    """``key_by`` must name an EXPANDED input, not a broadcast one.

    A broadcast input holds the same value for every item, so it can only
    ever produce one key for the whole manifest. Refusing it here names the
    real mistake instead of reporting a duplicate-key collision the caller
    then has to decode.
    """
    if not isinstance(key_by, str) or not key_by:
        raise ValueError(
            f"submit_batch() requires key_by to name one expanded input, got {key_by!r}.\n\n"
            "How to fix: pass key_by='work_item_id'. Durable Batch items need a stable logical key so "
            "restart and out-of-order completion never make an item anonymous."
        )
    if key_by not in map_over:
        raise ItemKeyError(
            key_by,
            f"submit_batch() key_by={key_by!r} does not name an expanded input; map_over expands {list(map_over)!r}.\n\n"
            "How to fix: name one of the map_over inputs. A broadcast input has the same value for every "
            "item, so it cannot identify one.",
        )


def _item_key(value: Any, *, key_by: str, index: int) -> str:
    """Project one expanded per-item value into its logical item key.

    Accepts the JSON-safe scalars that stay stable as durable identity:
    ``str`` and ``int``. ``bool`` is an ``int`` subclass but names at most
    two items, ``float`` has no stable text form, and ``None`` is absence —
    all three are refused rather than silently stringified into a key an
    operator can never reproduce. A generated map index is never acceptable
    durable identity, so there is no fallback.
    """
    if isinstance(value, str):
        if not value:
            raise ItemKeyError(
                key_by,
                f"submit_batch() item {index} has an empty {key_by!r}; a logical item key must name the item.\n\n"
                "How to fix: give every expanded item a non-empty key value.",
            )
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    kind = "missing" if value is None else f"a {type(value).__name__}"
    raise ItemKeyError(
        key_by,
        f"submit_batch() item {index} has {kind} {key_by!r} ({value!r}); a logical item key must be a "
        "JSON-safe scalar (a non-empty str, or an int).\n\n"
        "How to fix: expand an input whose per-item value already identifies the item (Panda passes its "
        "generated WorkItem id). Composite identity needs an explicit key projection computed before "
        "submission — Hypergraph never invents one from the map index.",
    )


def expand_batch_items(
    values: Mapping[str, Any],
    *,
    map_over: str | Sequence[str],
    map_mode: MapMode,
    key_by: str,
) -> list[tuple[str, str]]:
    """Expand runner-shaped values into the immutable keyed manifest.

    THE bridge between ``runner.map``'s input-expansion vocabulary and PRD
    0019's manifest: expansion happens **before** the acceptance
    transaction, and only the frozen ``(item_key, inputs_json)`` pairs
    reach the store. Mutating the caller's collection afterwards therefore
    cannot change durable intent.

    Args:
        values: Runner-shaped input values. Inputs named in ``map_over``
            hold per-item collections; everything else is broadcast to
            every item verbatim.
        map_over: Input name (or names) to expand, exactly as ``runner.map``
            reads it.
        map_mode: ``"zip"`` (parallel iteration) or ``"product"``
            (cartesian), exactly as ``runner.map`` reads it.
        key_by: The expanded input whose per-item scalar value becomes the
            logical item key.

    Returns:
        Manifest-ordered ``(item_key, inputs_json)`` pairs.

    Raises:
        TypeError: ``values`` is not a Mapping, or an item's inputs are not
            JSON-serializable.
        ValueError: Empty/duplicate ``map_over``, an unknown ``map_mode``, a
            ``map_over`` input missing from ``values``, or unequal zip
            lengths.
        ItemKeyError: ``key_by`` names a broadcast input, or an item's key
            is missing, empty, non-scalar, or duplicated.
    """
    from hypergraph.runners._shared.map_inputs import generate_map_inputs

    if not isinstance(values, Mapping):
        raise TypeError(f"submit_batch() values must be a Mapping of input name to value, got {type(values).__name__}.")
    names = normalize_map_over(map_over)
    _require_key_input(key_by, names)
    if map_mode not in ("zip", "product"):
        raise ValueError(f"submit_batch() map_mode must be 'zip' or 'product', got {map_mode!r}.")
    missing = [name for name in names if name not in values]
    if missing:
        raise ValueError(
            f"submit_batch() map_over names {missing!r}, which {'is' if len(missing) == 1 else 'are'} not in values.\n\n"
            f"How to fix: pass a collection for every expanded input. Given inputs: {sorted(values)}."
        )
    expanded = list(generate_map_inputs(dict(values), names, map_mode))
    if not expanded:
        raise ValueError("submit_batch() expanded to zero items; an empty Batch is not a Batch.")
    return _freeze_manifest(expanded, key_by=key_by)


def _freeze_manifest(expanded: list[dict[str, Any]], *, key_by: str) -> list[tuple[str, str]]:
    """Key and serialize expanded items, refusing duplicates before acceptance."""
    seen: dict[str, int] = {}
    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(expanded):
        key = _item_key(item.get(key_by), key_by=key_by, index=index)
        if key in seen:
            raise ItemKeyError(
                key_by,
                f"submit_batch() duplicate item key {key!r}: expanded items {seen[key]} and {index} both key to it.\n\n"
                "How to fix: every logical item key must be unique — two child Runs cannot claim to be the same "
                "item. De-duplicate the expanded collection, or key on an input that is distinct per item.",
            )
        seen[key] = index
        try:
            pairs.append((key, json.dumps(item)))
        except (TypeError, ValueError):
            raise TypeError(f"submit_batch() item {key!r} inputs must be JSON-serializable; got {item!r}.") from None
    return pairs
