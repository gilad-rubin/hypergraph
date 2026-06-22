"""Content key computation, marker extraction, and schema fingerprinting."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
from typing import Any, get_type_hints

from hypergraph.materialization._markers import ContentKey, Identity


@dataclasses.dataclass(frozen=True)
class MarkerInfo:
    """Extracted marker metadata from a source dataclass."""

    identity_fields: list[str]
    content_key_fields: list[str]


def extract_markers(cls: type) -> MarkerInfo:
    """Extract Identity and ContentKey field names from a frozen dataclass."""
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls.__name__} is not a dataclass")

    hints = get_type_hints(cls, include_extras=True)
    identity_fields = []
    content_key_fields = []

    for name, hint in hints.items():
        metadata = getattr(hint, "__metadata__", ())
        has_identity = any(m is Identity for m in metadata)
        has_content = any(m is ContentKey for m in metadata)
        if has_identity:
            identity_fields.append(name)
        if has_content:
            content_key_fields.append(name)

    if not identity_fields:
        raise ValueError(f"{cls.__name__} must have at least one Identity-annotated field")

    if not content_key_fields:
        all_fields = [f.name for f in dataclasses.fields(cls)]
        content_key_fields = [f for f in all_fields if f not in identity_fields]

    return MarkerInfo(
        identity_fields=sorted(identity_fields),
        content_key_fields=sorted(content_key_fields),
    )


def compute_schema_fingerprint(cls: type) -> str:
    """Hash of field names + types for an output dataclass."""
    parts = []
    for f in sorted(dataclasses.fields(cls), key=lambda f: f.name):
        type_name = getattr(f.type, "__name__", str(f.type))
        parts.append(f"{f.name}:{type_name}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def compute_definition_hash(fn: Any) -> str:
    """Hash the source code of a derive function."""
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        source = repr(fn)
    return hashlib.sha256(source.encode()).hexdigest()


def compute_graph_definition_hash(graph: Any) -> str:
    """Definition hash for a Graph derive: node code + topology + selection + bindings.

    ``inspect.getsource`` on a Graph would fall back to ``repr()``; instead capture
    the graph's own code hash, structural hash, output selection, and pre-bound
    config, so changing any of them invalidates the content key.
    """
    bound = {str(k): str(v) for k, v in sorted(graph.inputs.bound.items())}
    payload = json.dumps(
        {
            "code": getattr(graph, "code_hash", ""),
            "structural": getattr(graph, "structural_hash", ""),
            "selected": list(graph.selected) if graph.selected else None,
            "bound": bound,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def extract_markers_lenient(cls: type) -> MarkerInfo:
    """Like extract_markers but falls back to first field as Identity if none annotated."""
    try:
        return extract_markers(cls)
    except ValueError:
        all_fields = [f.name for f in dataclasses.fields(cls)]
        if not all_fields:
            raise
        return MarkerInfo(
            identity_fields=[all_fields[0]],
            content_key_fields=all_fields[1:] if len(all_fields) > 1 else all_fields,
        )


def compute_content_key(
    item: Any,
    component_configs: dict[str, Any],
    definition_hash: str,
    schema_fingerprint: str,
) -> str:
    """Compute the content key for a source item.

    Hashes ContentKey field values + component configs + definition hash + schema fingerprint.
    """
    markers = extract_markers_lenient(type(item))
    content_values = {}
    for field_name in markers.content_key_fields:
        val = getattr(item, field_name)
        if hasattr(val, "__materialization_repr__"):
            content_values[field_name] = val.__materialization_repr__()
        else:
            content_values[field_name] = val

    payload = {
        "content": content_values,
        "components": component_configs,
        "definition_hash": definition_hash,
        "schema_fingerprint": schema_fingerprint,
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()
