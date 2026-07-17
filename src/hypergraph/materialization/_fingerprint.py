"""Content fingerprinting and provenance for HyperTable rows.

A row's fingerprint is ``hash(source-input values + node definition hashes +
component config hashes + bound plain-value payloads)``. Bound non-component
plain values (a scalar such as a segmentation mode) are recipe: they
parameterize derivation exactly like a component config, so they participate
in fingerprints and per-column provenance. When any of those change, the row
re-derives on the next insert/sync; otherwise it is skipped.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from hypergraph._utils import hash_definition
from hypergraph.materialization._schema import node_func


def compute_definition_hash(fn: Any) -> str:
    """Hash the definition of a derive node: a node's own hash, or ``hash_definition``.

    A column producer may be a subgraph (a GraphNode — e.g. a validation cycle
    wrapped as one node): it has no single source function, but its
    ``definition_hash`` already covers the inner graph's node code plus the
    boundary projection, so that is the code-sensitive basis here. Everything
    else delegates to :func:`hypergraph._utils.hash_definition`, the repo's one
    definition-identity function: a bound method of a configured instance mixes
    the instance's state into the hash (two differently-configured components
    are different recipes), and a dynamically-created function with no
    retrievable source hashes its bytecode instead of a per-process repr.

    A non-callable that carries no ``definition_hash`` has no definition to
    hash — silently hashing its repr would differ in every process, so it is
    rejected loudly instead.
    """
    node_hash = getattr(fn, "definition_hash", None)
    if isinstance(node_hash, str) and node_hash:
        return node_hash
    if not callable(fn):
        raise TypeError(
            f"Cannot fingerprint {type(fn).__name__}: expected a node exposing "
            "definition_hash or a callable definition (recipe payload strings "
            "hash via compute_payload_hash)"
        )
    return hash_definition(fn)


def compute_node_definition_hash(node: Any) -> str:
    """Recipe identity of a producing node: its construction-time ``definition_hash``.

    Every function-carrying node captures ``hash_definition(func)`` when it is
    built (``FunctionNode``, gates, interrupts; a ``GraphNode``'s hash covers
    its inner graph), so the node's own hash is the stable identity. Hashing
    the LIVE callable instead would re-capture mutable instance state — a
    counter, a cache, a client — on every computation and drift the recipe run
    over run. A node exposing only a raw callable falls back to that callable;
    anything else must be a definition in its own right or is rejected by
    ``compute_definition_hash``.
    """
    node_hash = getattr(node, "definition_hash", None)
    if isinstance(node_hash, str) and node_hash:
        return node_hash
    func = node_func(node)
    if func is not None:
        return compute_definition_hash(func)
    return compute_definition_hash(node)


def compute_payload_hash(payload: str) -> str:
    """Content hash of a recipe payload string (a config repr or bound-value text).

    Journal keys for non-callable recipe text: a payload is data, not a
    function definition, so it hashes by content.
    """
    return hashlib.sha256(payload.encode()).hexdigest()


def _node_definition_hashes(graph: Any) -> list[str]:
    if graph is None:
        return []
    return [compute_node_definition_hash(n) for n in graph.iter_nodes()]


def _plain_value_payload(value: Any) -> str | None:
    """A stable hash payload for a bound plain-data value, or None when the value
    is an object whose repr is not stable across processes (those are excluded,
    exactly as a component without a config always was)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return f"{type(value).__name__}:{value!r}"
    if isinstance(value, (list, tuple)):
        parts = [_plain_value_payload(item) for item in value]
        if any(part is None for part in parts):
            return None
        return f"{type(value).__name__}:[{','.join(parts)}]"  # type: ignore[arg-type]
    if isinstance(value, dict):
        parts = []
        for key in sorted(value, key=str):
            part = _plain_value_payload(value[key])
            if part is None:
                return None
            parts.append(f"{key!s}={part}")
        return f"dict:{{{','.join(parts)}}}"
    return None


def _component_config_hashes(components: dict[str, Any], valid_inputs: set[str] | None = None) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, comp in components.items():
        if valid_inputs is not None and name not in valid_inputs:
            continue
        config = getattr(comp, "__component_config__", None) or (comp._config() if hasattr(comp, "_config") else None)
        if config is not None:
            hashes[name] = str(config)
            continue
        # A bound non-component plain value (a scalar such as segment_semantics,
        # or a plain list/dict of scalars) parameterizes derivation the same way
        # a component config does — it is recipe by definition. Fold its value
        # in so changing it stales exactly the columns whose nodes consume it.
        # Objects without a config and without a stable value payload stay
        # excluded, as before.
        plain = _plain_value_payload(comp)
        if plain is not None:
            hashes[name] = plain
    return hashes


def _fingerprint(inputs: dict[str, Any], node_hashes: list[str], component_hashes: dict[str, str]) -> str:
    payload = json.dumps(
        {
            "inputs": {k: f"{type(v).__name__}:{v}" for k, v in sorted(inputs.items())},
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


def compute_recipe_fingerprint(node_fn: Any, component_hashes: dict[str, str]) -> str:
    """Recipe identity for a column's producing node: hash(node code + consumed component configs).

    Unlike ``compute_column_provenance`` this excludes input values — it names
    HOW a column is derived, not what it was derived from. A named index records
    it so a rebound component (e.g. a different embedder) flips the index stale.
    """
    payload = json.dumps(
        {
            "node": compute_node_definition_hash(node_fn),
            "components": component_hashes,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_table_recipe_fingerprint(graph: Any, components: dict[str, Any], valid_inputs: set[str] | None = None) -> str:
    """Recipe-only identity for a whole table's derivation: NO input values.

    The per-row stamp (``_recipe_fingerprint``) written at derive time: the
    same payload composition as ``compute_row_fingerprint`` — node definition
    hashes + component config / bound plain-value hashes — with the inputs
    slot deliberately empty, so every row derived under one recipe carries
    the SAME stamp and "does this row match today's recipe" is a stored-column
    comparison. Root tables pass no ``valid_inputs`` (mirroring
    ``compute_row_fingerprint``'s unscoped component set); child tables scope
    to the child graph's inputs (mirroring ``compute_child_fingerprint``).
    """
    return _fingerprint({}, _node_definition_hashes(graph), _component_config_hashes(components, valid_inputs))


def compute_column_provenance(node_fn: Any, inputs: dict[str, Any], component_hashes: dict[str, str]) -> str:
    """Per-column provenance: hash(producing node's code + its direct input values + consumed component configs).

    Direct inputs are themselves stored columns, so transitivity is value-based:
    an upstream change that yields the same value stops the cascade here.
    """
    payload = json.dumps(
        {
            "node": compute_node_definition_hash(node_fn),
            "inputs": {k: f"{type(v).__name__}:{v}" for k, v in sorted(inputs.items())},
            "components": component_hashes,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
