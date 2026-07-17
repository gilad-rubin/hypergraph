"""Pure map-item resume decisions shared by sync and async templates."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from hypergraph.checkpointers.types import Run

MAP_SIGNATURE_CONFIG_KEY = "map_item_signature"


def normalize_signature_value(value: Any) -> Any:
    """Normalize map inputs into a JSON-stable structure for hashing."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): normalize_signature_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [normalize_signature_value(v) for v in value]
    if isinstance(value, (set, frozenset)):
        normalized = [normalize_signature_value(v) for v in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        )
    return {"__type__": type(value).__name__, "__repr__": repr(value)}


def compute_map_item_signature(
    variation_inputs: dict[str, Any],
    map_over: list[str],
    map_mode: str,
) -> str:
    """Compute a stable signature for one mapped item input payload."""
    payload = {
        "map_mode": map_mode,
        "map_over": map_over,
        "inputs": normalize_signature_value(variation_inputs),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def index_completed_child_runs(
    child_runs: list[Run],
    workflow_id: str | None,
) -> tuple[dict[str, list[str]], dict[int, list[str]]]:
    """Index completed child runs into disjoint signed and legacy pools.

    Persisted signatures are authoritative (#199): a child with a valid
    signature enters ONLY the signature pool, so a signed child whose inputs
    changed can never be restored through the numeric index fallback. A child
    whose config lacks the signature key entirely (persisted before signatures
    existed) enters ONLY the legacy index pool. A child with
    present-but-invalid signature metadata enters neither pool and is
    therefore re-executed fresh rather than restored or erroring.
    """
    by_signature: dict[str, list[str]] = defaultdict(list)
    legacy_by_index: dict[int, list[str]] = defaultdict(list)

    for run in child_runs:
        config = run.config if isinstance(run.config, dict) else {}
        if MAP_SIGNATURE_CONFIG_KEY in config:
            signature = config[MAP_SIGNATURE_CONFIG_KEY]
            if isinstance(signature, str) and signature:
                by_signature[signature].append(run.id)
            # Present-but-invalid metadata: neither pool -> fresh execution.
            continue

        if workflow_id is None:
            continue
        suffix = run.id.removeprefix(f"{workflow_id}/")
        if suffix.isdigit():
            legacy_by_index[int(suffix)].append(run.id)

    for ids in by_signature.values():
        ids.sort()
    for ids in legacy_by_index.values():
        ids.sort()
    return by_signature, legacy_by_index


def claim_completed_child_run_id(
    *,
    idx: int,
    signature: str,
    by_signature: dict[str, list[str]],
    legacy_by_index: dict[int, list[str]],
) -> str | None:
    """Claim a completed child run id for one map item, or None for fresh.

    Signed evidence is authoritative: a signature match claims the
    lexicographically smallest unclaimed matching run id, so duplicate
    signatures are claimed deterministically in ascending run-id order, one
    child per claim. The numeric index fallback consults only legacy children
    (signature key absent); signed children are never claimable by index.
    Pools are disjoint and claims pop, so each child is claimed at most once.
    """
    signed = by_signature.get(signature)
    if signed:
        return signed.pop(0)

    legacy = legacy_by_index.get(idx)
    if legacy:
        return legacy.pop(0)

    return None
