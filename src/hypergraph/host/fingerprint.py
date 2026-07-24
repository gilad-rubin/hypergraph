"""Start fingerprint for submission dedup (amendment A5).

The fingerprint hashes a canonical JSON document covering the complete
pinned Definition identity, normalized inputs, the (future) effective Batch
configuration, and the requested ``start_at``. Identical resubmissions
dedupe only when every aspect matches; a mismatch on the same
``workflow_id`` is a typed conflict, never a silent reuse.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from hypergraph.host.definition import DefinitionId


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
