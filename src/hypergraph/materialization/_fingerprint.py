"""Content fingerprinting and provenance for HyperTable rows.

A row's fingerprint is ``hash(source-input values + node definition hashes +
component config hashes)``. When any of those change, the row re-derives on the
next insert/sync; otherwise it is skipped.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any


def compute_definition_hash(fn: Any) -> str:
    """Hash the source code of a derive function (falls back to repr)."""
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        source = repr(fn)
    return hashlib.sha256(source.encode()).hexdigest()


def _node_definition_hashes(graph: Any) -> list[str]:
    if graph is None:
        return []
    return [compute_definition_hash(func) for n in graph.iter_nodes() if (func := getattr(n, "func", None)) is not None]


def _component_config_hashes(components: dict[str, Any], valid_inputs: set[str] | None = None) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, comp in components.items():
        if valid_inputs is not None and name not in valid_inputs:
            continue
        config = getattr(comp, "__component_config__", None) or (comp._config() if hasattr(comp, "_config") else None)
        if config is not None:
            hashes[name] = str(config)
    return hashes


def _fingerprint(inputs: dict[str, Any], node_hashes: list[str], component_hashes: dict[str, str]) -> str:
    payload = json.dumps(
        {
            "inputs": {k: str(v) for k, v in sorted(inputs.items())},
            "nodes": sorted(node_hashes),
            "components": component_hashes,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_row_fingerprint(graph: Any, components: dict[str, Any], graph_inputs: dict[str, Any]) -> str:
    """Fingerprint a root row from its source inputs, node code, and component configs."""
    return _fingerprint(graph_inputs, _node_definition_hashes(graph), _component_config_hashes(components))


def compute_child_fingerprint(child_graph: Any, components: dict[str, Any], child_inputs: dict[str, Any]) -> str:
    """Fingerprint a child row, scoped to the child graph (only its components count)."""
    valid_inputs = set(child_graph.inputs.all) if child_graph is not None and hasattr(child_graph.inputs, "all") else set()
    return _fingerprint(child_inputs, _node_definition_hashes(child_graph), _component_config_hashes(components, valid_inputs))


def compute_provenance(col_name: str, inputs: dict[str, Any]) -> str:
    """Per-column provenance hash over the inputs that produced it."""
    payload = json.dumps(
        {"column": col_name, "inputs": {k: str(v) for k, v in sorted(inputs.items())}},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
