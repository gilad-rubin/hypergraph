"""Batch value objects and validation for the durable host.

A Batch is an immutable manifest of unique logical item keys, each mapped to
one independent child Run (PRD 0019). This module owns the pinned tolerance
declaration, its strictly-exceeds trip predicate, the runner-shaped
input expansion that freezes into that manifest, and submission-time item
validation. Where a trip is evaluated (in the same transaction as the child
terminal write) and what it does (close admission, then account each
still-pending item unstarted or abandoned by whether it had started)
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


#: The example every ``map_over`` refusal shows, so one shape is taught once.
_MAP_OVER_EXAMPLE = "map_over='work_item_id', or map_over=['work_item_id', 'shard'] for several"


def normalize_map_over(map_over: str | Sequence[str]) -> list[str]:
    """Normalize ``map_over`` to a non-empty list of distinct input names.

    A durable Batch always expands: a submission with nothing to map over is
    one Run, and ``host.submit`` is the verb for that. The vocabulary is
    ``runner.map``'s verbatim (``str`` or a sequence of names), so a caller
    never learns a second collection-expansion model.

    Every refusal here names the submission argument that was wrong, what
    was supplied, and what to pass instead. In particular a non-sequence
    ``map_over`` is refused BY NAME rather than letting ``list()`` surface
    ``'int' object is not iterable`` — a raw Python message that never
    mentions ``submit_batch`` or ``map_over`` and reads like a framework bug.
    """
    if isinstance(map_over, str):
        names: list[str] = [map_over]
    elif isinstance(map_over, Mapping):
        raise TypeError(
            f"submit_batch() map_over must name inputs, not map them: got a {type(map_over).__name__} "
            f"({map_over!r}). Per-item values belong in `values`.\n\n"
            f"How to fix: pass {_MAP_OVER_EXAMPLE}, and put the collections in values — "
            "submit_batch(graph, {'work_item_id': [...]}, map_over='work_item_id', key_by='work_item_id')."
        )
    else:
        try:
            names = list(map_over)
        except TypeError:
            raise TypeError(
                f"submit_batch() map_over must be an input name or a sequence of input names, "
                f"got {type(map_over).__name__} ({map_over!r}).\n\n"
                f"How to fix: pass {_MAP_OVER_EXAMPLE}."
            ) from None
    if not names:
        raise ValueError(
            "submit_batch() requires map_over to name at least one input to expand, got an empty sequence.\n\n"
            f"How to fix: pass {_MAP_OVER_EXAMPLE}. "
            "A submission with nothing to expand is one Run — use host.submit(graph, values)."
        )
    bad = [name for name in names if not isinstance(name, str) or not name]
    if bad:
        raise ValueError(
            f"submit_batch() map_over entries must be non-empty input-name strings; got {bad!r} in {names!r}.\n\n"
            f"How to fix: pass {_MAP_OVER_EXAMPLE}. Each entry names an input of the graph whose "
            "value in `values` is the per-item collection."
        )
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise ValueError(
            f"submit_batch() map_over names {duplicated!r} more than once: {names!r}.\n\n"
            "How to fix: name each expanded input exactly once. Expanding one input twice cannot mean "
            "anything different from expanding it once, in either map_mode."
        )
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
        "How to fix: expand an input whose per-item value already identifies the item — an id column, "
        "not a payload. Composite identity needs an explicit key projection computed before submission; "
        "Hypergraph never invents one from the map index.",
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
        TypeError: ``values`` is not a Mapping, ``map_over`` is neither a
            name nor a sequence of names, or an item's inputs are not
            JSON-serializable.
        ValueError: Empty/duplicate ``map_over``, a non-name ``map_over``
            entry, an unknown ``map_mode``, a ``map_over`` input missing
            from ``values``, unequal zip lengths, or an empty expansion.
        ItemKeyError: ``key_by`` names a broadcast input, or an item's key
            is missing, empty, non-scalar, or duplicated.
    """
    from hypergraph.runners._shared.map_inputs import generate_map_inputs

    if not isinstance(values, Mapping):
        raise TypeError(
            f"submit_batch() values must be a Mapping of input name to value, got {type(values).__name__} ({values!r}).\n\n"
            "How to fix: pass the same runner-shaped dict runner.map takes — "
            "submit_batch(graph, {'work_item_id': [...]}, map_over='work_item_id', key_by='work_item_id')."
        )
    names = normalize_map_over(map_over)
    _require_key_input(key_by, names)
    if map_mode not in ("zip", "product"):
        raise ValueError(
            f"submit_batch() map_mode must be 'zip' or 'product', got {map_mode!r}.\n\n"
            "How to fix: use map_mode='zip' to walk the expanded inputs in parallel (equal lengths, one "
            "item per position), or map_mode='product' for every combination of them."
        )
    missing = [name for name in names if name not in values]
    if missing:
        raise ValueError(
            f"submit_batch() map_over names {missing!r}, which {'is' if len(missing) == 1 else 'are'} not in values.\n\n"
            f"How to fix: pass a collection for every expanded input. Given inputs: {sorted(values)}."
        )
    expanded = list(generate_map_inputs(dict(values), names, map_mode))
    if not expanded:
        empty = sorted(name for name in names if not values[name])
        raise ValueError(
            f"submit_batch() expanded to zero items; an empty Batch is not a Batch. "
            f"map_over={names!r} with map_mode={map_mode!r}, and {empty!r} supplied no values.\n\n"
            "How to fix: submit only when there is work — check the collection before calling, and "
            "treat 'nothing to do' as a decision your code makes, not a durable Batch with no children. "
            "An accepted Batch pins its manifest forever, so an empty one could never be filled in later."
        )
    return _freeze_manifest(expanded, key_by=key_by)


def _is_json_safe(value: Any) -> bool:
    """Whether one input value survives the store round trip, for diagnostics."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


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
        except (TypeError, ValueError) as exc:
            unserializable = sorted(name for name, value in item.items() if not _is_json_safe(value))
            raise TypeError(
                f"submit_batch() item {key!r} inputs must be JSON-serializable; "
                f"{unserializable!r} {'is' if len(unserializable) == 1 else 'are'} not ({exc}). Item: {item!r}.\n\n"
                "How to fix: pass values that survive a round trip through the store — a child Run is "
                "started from its pinned inputs by a worker in another process, which has no access to "
                "the object you submitted. Send an id or a path and load it inside the graph."
            ) from None
    return pairs
