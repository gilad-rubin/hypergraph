"""Content fingerprinting and provenance for HyperTable rows.

A row's fingerprint is ``hash(source-input values + node definition hashes +
component config hashes + bound plain-value payloads)``. Bound non-component
plain values (a scalar such as a segmentation mode) are recipe: they
parameterize derivation exactly like a component config, so they participate
in fingerprints and per-column provenance. When any of those change, the row
re-derives on the next insert/sync; otherwise it is skipped.

Bound values count at EVERY depth. A value bound on a graph mounted as a
``GraphNode`` — a per-page profile prompt, a child table's recipe — reaches no
graph-level hash of its own (``Graph.definition_hash`` excludes bindings by
design) but the run path honors it, so identity has to see it or a profile edit
silently skips instead of re-deriving.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable
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


def combine_recipe_fingerprints(fingerprints: Iterable[str]) -> str:
    """One recipe identity for a column with several producers (a routed union).

    The hash of the SORTED per-producer recipe fingerprints — order-free, so
    graph insertion order does not matter, and a code or config change in ANY
    branch flips the combined recipe.
    """
    payload = json.dumps(sorted(fingerprints))
    return hashlib.sha256(payload.encode()).hexdigest()


def nested_bound_payloads(node: Any, shadowed: Collection[str] = ()) -> list[str]:
    """Recipe payloads for values bound INSIDE a node's nested graph, recursively.

    ``Graph._compute_definition_hash`` deliberately excludes bindings ("runtime
    values, not structure"), so a ``GraphNode``'s ``definition_hash`` moves when
    you bind-vs-not-bind a name but never when the bound VALUE changes. For a
    HyperTable those values ARE recipe — they parameterize derivation exactly
    like a root binding, and the run path already honors them — so identity has
    to see them too.

    Payload rules are the root's rules, unchanged at every depth: a
    ``__component_config__`` first, then a stable plain value, and an object
    with neither stays excluded (never a repr hash). Inner nodes are
    name-qualified so two sibling subgraphs cannot swap values unnoticed.
    Returns ``[]`` for every node that wraps no graph.

    ``shadowed`` names are the ONE precedence rule this module has, applied at
    every depth: the OUTERMOST binding of a name is the value that actually runs
    (an outer bind flattens down through the boundary — the graph layer even
    warns "Parent bind for X overrides nested bind from GraphNode Y"), so every
    inner value it shadows is not recipe and must not move the fingerprint.
    """
    return graph_bound_payloads(getattr(node, "graph", None), shadowed)


def graph_bound_payloads(graph: Any, shadowed: Collection[str] = ()) -> list[str]:
    """Every bound-value payload a graph carries, the graphs nested in it included.

    Same payload rules, same name-qualification and the same ``shadowed``
    precedence as :func:`nested_bound_payloads`, entered from a graph instead of
    from the node that wraps it — the shape a mounted child graph arrives in.
    """
    if graph is None or not hasattr(graph, "iter_nodes"):
        return []
    all_own = getattr(graph, "_bound", None) or {}
    own = {name: value for name, value in all_own.items() if name not in shadowed}
    payloads = [f"{name}={payload}" for name, payload in sorted(_component_config_hashes(own).items())]
    # This graph's own bindings shadow the SAME name deeper, exactly as the
    # caller's shadow it here — precedence is outermost-wins at every hop, not
    # just at the first one. Carry the union down, including names already
    # shadowed from further out.
    deeper = frozenset(shadowed) | frozenset(all_own)
    for inner in graph.iter_nodes():
        payloads.extend(f"{inner.name}/{part}" for part in nested_bound_payloads(inner, deeper))
    return payloads


def compute_node_recipe_hash(node: Any, shadowed: Collection[str] = ()) -> str:
    """A node's recipe identity: its definition hash plus any nested bindings.

    Identical to :func:`compute_node_definition_hash` for every node that wraps
    no graph, and for a ``GraphNode`` whose inner graphs bind nothing hashable —
    so stored stamps only move for the recipes that actually had the hole.
    ``shadowed`` carries the names the caller already folded in as components.
    """
    definition_hash = compute_node_definition_hash(node)
    payloads = nested_bound_payloads(node, shadowed)
    if not payloads:
        return definition_hash
    return hashlib.sha256("|".join([definition_hash, *sorted(payloads)]).encode()).hexdigest()


def _node_definition_hashes(graph: Any, shadowed: Collection[str] = ()) -> list[str]:
    if graph is None:
        return []
    return [compute_node_recipe_hash(n, shadowed) for n in graph.iter_nodes()]


def recipe_component_hashes(
    graph: Any,
    components: dict[str, Any],
    valid_inputs: set[str] | None = None,
) -> tuple[dict[str, str], frozenset[str]]:
    """The component slot for a graph's recipe, and the names it shadows deeper.

    The caller's components are layered OVER the graph's own bindings: a graph
    mounted as a child carries the ``bind(...)`` values it was built with, and
    only the ROOT graph's bindings ever reach ``components``. A root bind wins
    on a shared name, mirroring ``Provenance.bind_child_components`` at run
    time. For a root table the components ARE the graph's bindings, so the
    merge is a no-op there.

    The second element is every name this slot accounts for — hashable or not,
    since an unhashable component still shadows at run time. Pass it down as
    ``shadowed`` so one name is never counted twice, at two different depths,
    with two different values.
    """
    merged = {**(getattr(graph, "_bound", None) or {}), **components}
    if valid_inputs is not None:
        merged = {name: value for name, value in merged.items() if name in valid_inputs}
    return _component_config_hashes(merged), frozenset(merged)


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
        return f"dict:{{{','.join(parts)}}}"  # type: ignore[arg-type]
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


def compute_row_fingerprint(
    graph: Any,
    components: dict[str, Any],
    graph_inputs: dict[str, Any],
    mounted_payloads: Iterable[str] = (),
) -> str:
    """Fingerprint a root row from its source inputs, node code, and component configs.

    ``mounted_payloads`` carries the bound-value payloads of graphs mounted as
    CHILD tables. A ``map_over`` GraphNode is lifted out of the root graph at
    analysis time, so without them a value bound on the child graph is invisible
    here and ``sync()`` skips the very row whose children need re-deriving.
    """
    component_hashes, shadowed = recipe_component_hashes(graph, components)
    return _fingerprint(
        graph_inputs,
        [*_node_definition_hashes(graph, shadowed), *mounted_payloads],
        component_hashes,
    )


def compute_child_fingerprint(
    child_graph: Any,
    components: dict[str, Any],
    child_inputs: dict[str, Any],
    mounted_payloads: Iterable[str] = (),
) -> str:
    """Fingerprint a child row, scoped to the child graph (only its components count).

    ``mounted_payloads`` mirrors ``compute_row_fingerprint``'s: a middle grain
    carries its OWN grandchildren's bound-value payloads, so a grandchild recipe
    change is visible at the grain that gates it and not only at the root.
    """
    valid_inputs = set(child_graph.inputs.all) if child_graph is not None and hasattr(child_graph.inputs, "all") else set()
    component_hashes, shadowed = recipe_component_hashes(child_graph, components, valid_inputs)
    return _fingerprint(
        child_inputs,
        [*_node_definition_hashes(child_graph, shadowed), *mounted_payloads],
        component_hashes,
    )


def compute_recipe_fingerprint(node_fn: Any, component_hashes: dict[str, str], shadowed: Collection[str] = ()) -> str:
    """Recipe identity for a column's producing node: hash(node code + consumed component configs).

    Unlike ``compute_column_provenance`` this excludes input values — it names
    HOW a column is derived, not what it was derived from. A named index records
    it so a rebound component (e.g. a different embedder) flips the index stale.
    """
    payload = json.dumps(
        {
            "node": compute_node_recipe_hash(node_fn, shadowed),
            "components": component_hashes,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_table_recipe_fingerprint(
    graph: Any,
    components: dict[str, Any],
    valid_inputs: set[str] | None = None,
    mounted_payloads: Iterable[str] = (),
) -> str:
    """Recipe-only identity for a whole table's derivation: NO input values.

    The per-row stamp (``_recipe_fingerprint``) written at derive time: the
    same payload composition as ``compute_row_fingerprint`` — node definition
    hashes + component config / bound plain-value hashes — with the inputs
    slot deliberately empty, so every row derived under one recipe carries
    the SAME stamp and "does this row match today's recipe" is a stored-column
    comparison. Root tables pass no ``valid_inputs`` (mirroring
    ``compute_row_fingerprint``'s unscoped component set); child tables scope
    to the child graph's inputs (mirroring ``compute_child_fingerprint``).
    ``mounted_payloads`` mirrors ``compute_row_fingerprint``'s.
    """
    component_hashes, shadowed = recipe_component_hashes(graph, components, valid_inputs)
    return _fingerprint(
        {},
        [*_node_definition_hashes(graph, shadowed), *mounted_payloads],
        component_hashes,
    )


def compute_column_provenance(
    node_fn: Any,
    inputs: dict[str, Any],
    component_hashes: dict[str, str],
    shadowed: Collection[str] = (),
) -> str:
    """Per-column provenance: hash(producing node's code + its direct input values + consumed component configs).

    Direct inputs are themselves stored columns, so transitivity is value-based:
    an upstream change that yields the same value stops the cascade here.
    """
    payload = json.dumps(
        {
            "node": compute_node_recipe_hash(node_fn, shadowed),
            "inputs": {k: f"{type(v).__name__}:{v}" for k, v in sorted(inputs.items())},
            "components": component_hashes,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
