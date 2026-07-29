"""Start fingerprint for submission dedup (amendment A5).

The fingerprint hashes a canonical JSON document covering the complete
pinned Definition identity, normalized inputs, the effective Batch
configuration (item keys and tolerance, when batched), and the requested
``start_at``. Identical resubmissions
dedupe only when every aspect matches; a mismatch on the same
``workflow_id`` is a typed conflict, never a silent reuse.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from hypergraph.host.definition import DefinitionId

if TYPE_CHECKING:
    from hypergraph.host.batch import BatchTolerance


def canonical_json(document: Any) -> str:
    """Serialize a JSON document canonically (sorted keys, compact)."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def start_fingerprint(definition: DefinitionId, inputs_json: str, start_at: str | None) -> str:
    """Hash the canonical start document for one submission.

    Args:
        definition: The pinned Definition identity.
        inputs_json: The submission's inputs as a JSON string (any valid
            serialization; it is parsed and re-serialized canonically so
            key order and whitespace never affect the fingerprint).
        start_at: ISO delayed-start string, or None.
    """
    document = {
        "definition": [definition.name, definition.deployment_version, definition.structural_hash],
        "inputs": json.loads(inputs_json),
        "batch": None,
        "start_at": start_at,
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def fingerprint_mismatch_aspect(
    existing: dict[str, Any],
    *,
    definition_name: str,
    def_version: str,
    def_struct_hash: str,
    inputs_json: str,
    start_at: str | None,
) -> str:
    """Name which fingerprint aspect differs from the stored submission.

    The fingerprint itself is opaque; this compares the stored row's fields
    against the new submission so the conflict message can say whether the
    Definition identity, the inputs, or the start time mismatched. Callers
    must only invoke this when the fingerprints already differ.
    """
    if (existing["definition_name"], existing["def_version"], existing["def_struct_hash"]) != (
        definition_name,
        def_version,
        def_struct_hash,
    ):
        return "definition identity"
    if canonical_json(json.loads(existing["inputs_json"])) != canonical_json(json.loads(inputs_json)):
        return "inputs"
    return "start_at"


def batch_fingerprint(
    definition: DefinitionId,
    items: dict[str, Any],
    tolerance: BatchTolerance | None,
    start_at: str | None,
    exclusive_by: str | None = None,
) -> str:
    """Hash the canonical start document for one Batch submission.

    The fingerprint covers the complete pinned Definition identity, the
    normalized manifest (logical item key -> inputs), the pinned tolerance
    declaration, and the requested ``start_at`` — identical resubmissions
    dedupe only when every aspect matches (amendment A5 applied to Batches).

    Args:
        definition: The pinned Definition identity.
        items: The manifest as a plain dict (item key -> inputs); it is
            serialized canonically so mapping order never affects the
            fingerprint.
        tolerance: The pinned BatchTolerance, or None.
        start_at: ISO delayed-start string, or None.
    """
    document = {
        "definition": [definition.name, definition.deployment_version, definition.structural_hash],
        "items": items,
        "tolerance": tolerance.to_dict() if tolerance is not None else None,
        "start_at": start_at,
    }
    # Preserve pre-#354 fingerprints for Batches without exclusivity so an
    # unsettled manifest accepted before upgrade still deduplicates.
    if exclusive_by is not None:
        document["exclusive_by"] = exclusive_by
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def batch_mismatch_aspect(
    existing: dict[str, Any],
    *,
    definition_name: str,
    def_version: str,
    def_struct_hash: str,
    items_canonical: str,
    tolerance_json: str | None,
    start_at: str | None,
    exclusive_by: str | None = None,
) -> str:
    """Name which Batch fingerprint aspect differs from the stored manifest.

    ``existing`` is a host_batches row; ``items_canonical`` is the new
    manifest's canonical JSON. Callers must only invoke this when the
    fingerprints already differ.
    """
    if (existing["definition_name"], existing["def_version"], existing["def_struct_hash"]) != (
        definition_name,
        def_version,
        def_struct_hash,
    ):
        return "definition identity"
    if canonical_json(json.loads(existing["items_json"])) != items_canonical:
        return "items"
    if existing["tolerance_json"] != tolerance_json:
        return "tolerance"
    if existing["exclusive_by"] != exclusive_by:
        return "exclusive_by"
    return "start_at"
