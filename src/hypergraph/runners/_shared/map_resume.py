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
    """Index completed child runs by signature and by legacy index suffix."""
    by_signature: dict[str, list[str]] = defaultdict(list)
    by_index: dict[int, list[str]] = defaultdict(list)

    for run in child_runs:
        if isinstance(run.config, dict):
            signature = run.config.get(MAP_SIGNATURE_CONFIG_KEY)
            if isinstance(signature, str):
                by_signature[signature].append(run.id)

        if workflow_id is None:
            continue
        suffix = run.id.removeprefix(f"{workflow_id}/")
        if suffix.isdigit():
            by_index[int(suffix)].append(run.id)

    for ids in by_signature.values():
        ids.sort()
    for ids in by_index.values():
        ids.sort()
    return by_signature, by_index


def claim_completed_child_run_id(
    *,
    idx: int,
    signature: str,
    by_signature: dict[str, list[str]],
    by_index: dict[int, list[str]],
) -> str | None:
    """Claim a completed child run id, preferring input identity."""
    by_sig = by_signature.get(signature)
    if by_sig:
        return by_sig.pop(0)

    by_idx = by_index.get(idx)
    if by_idx:
        return by_idx.pop(0)

    return None
