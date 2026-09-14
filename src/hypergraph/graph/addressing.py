"""Input addressing and edge-value lookup shared across package boundaries.

Unlike ``graph/_helpers.py`` (graph-internal), the functions here are depended
on from outside ``hypergraph.graph`` and carry a compatibility contract. Each
function's consumers, exactly:

- ``describe_addressed_input`` -- ``runners._shared.value_resolution`` (the
  bind-override warning) and ``runners._shared.validation`` (the missing-input
  error).
- ``flatten_subgraph_addressing`` -- ``runners._shared.input_normalization``
  and ``graph/core.py`` (``Graph.bind``). Sharing it is the point: the runner
  boundary and ``Graph.bind`` must accept the same addressing forms.
- ``get_edge_produced_values`` -- ``runners._shared.validation``,
  ``graph/core.py`` and ``graph/input_spec.py``.
- ``format_did_you_mean`` -- ``graph/core.py`` (``Graph.bind``,
  ``Graph.select``), ``graph/validation.py`` (``wait_for`` names, gate targets)
  and ``runners._shared`` (``validation`` for run/map inputs,
  ``routing_validation`` for a gate's returned target). It is the one engine
  for *name and address* suggestions; no such caller runs its own fuzzy match.
  ``host/client.py`` deliberately keeps its own ``get_close_matches`` for
  durable batch item keys -- a different namespace, with no path structure to
  rank by.
- ``suggest_addresses`` -- the ranking half of that engine, called by
  ``format_did_you_mean`` and available to any caller that needs the ranked
  addresses without the rendered sentence.

Their output is user-visible. ``describe_addressed_input``'s
``"'x' of subgraph 'inner'"`` shape is inlined verbatim into missing-input
errors and bind-override warnings, ``flatten_subgraph_addressing``'s
``ValueError`` messages surface to users as-is, and ``format_did_you_mean``
renders the suggestion sentence every one of those surfaces prints. Changing
any of those formats changes what users read, so treat them as public behavior,
not as an implementation detail (see ``dev/CODE-CONVENTIONS.md`` § Module
Naming: a cross-module internal API does not carry a ``_`` prefix).
"""

from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

import networkx as nx

if TYPE_CHECKING:
    from hypergraph.graph.core import Graph

#: Minimum ``SequenceMatcher`` ratio for two segments (or two whole paths) to
#: count as a typo of one another. Matches ``difflib.get_close_matches``.
_TYPO_CUTOFF = 0.6


def get_edge_produced_values(nx_graph: nx.DiGraph) -> set[str]:
    """Get all value names that are produced by data edges.

    Only data edges carry values. Control edges (from gates) define
    routing relationships but don't produce values.
    """
    result: set[str] = set()
    for _, _, data in nx_graph.edges(data=True):
        if data.get("edge_type") == "data":
            result.update(data.get("value_names", []))
    return result


def flatten_subgraph_addressing(values: dict[str, Any], graph: Graph) -> dict[str, Any]:
    """Canonicalize nested-dict sugar for valid namespaced graph-node inputs.

    A user can address a namespaced GraphNode input either as a resolved port
    address (``{"A.x": v}``) or as nested-dict sugar
    (``{"A": {"x": v}}``). Flat GraphNodes do not create this nested-dict
    surface; a dict under their node name remains an ordinary dict value.

    Single source of truth used by both the runner boundary
    (``normalize_inputs``) and ``Graph.bind`` so the two surfaces stay in
    lockstep.

    Raises:
        ValueError: when the same address is provided twice, when a nested
            dict targets an exposed/stale address, or when the nested key does
            not resolve to a current namespaced input address.
    """
    from hypergraph.nodes.graph_node import GraphNode

    flat_input_names = set(graph.inputs.all)
    flat: dict[str, Any] = {}
    for key, value in values.items():
        child = graph._nodes.get(key) if isinstance(graph._nodes.get(key), GraphNode) else None
        if isinstance(value, dict) and child is not None and child.namespaced:  # type: ignore[attr-defined]
            if key in flat_input_names:
                raise ValueError(
                    f"Ambiguous addressing: {key!r} is both a flat input of this graph and the "
                    f"name of a child GraphNode. Pass a flat dict value via the resolved address "
                    f"(e.g., {{{key + '.<inner>'!r}: ...}}) to disambiguate."
                )
            for sub_key, sub_value in flatten_subgraph_addressing(value, child.graph).items():  # type: ignore[attr-defined]
                candidate = f"{key}.{sub_key}"
                if candidate in child.inputs:
                    full_key = candidate
                else:
                    replacement = child.replacement_for_stale_input_address(candidate)  # type: ignore[attr-defined]
                    if replacement is not None:
                        raise ValueError(f"Input address {candidate!r} is no longer valid. Use {replacement!r}.")
                    valid_namespaced = sorted(address for address in child.inputs if address.startswith(f"{key}."))
                    raise ValueError(
                        f"Nested input {candidate!r} is not a valid namespaced input address. Valid namespaced inputs: {valid_namespaced}"
                    )
                if full_key in flat:
                    raise ValueError(f"Input key {full_key!r} provided twice (mixed resolved-address and nested-dict forms).")
                flat[full_key] = sub_value
        else:
            if key in flat:
                raise ValueError(f"Input key {key!r} provided twice (mixed resolved-address and nested-dict forms).")
            flat[key] = value
    return flat


def describe_addressed_input(path: str) -> str:
    """Render a human-readable description of a possibly namespaced input.

    Single source of truth for inlining input addresses into error / warning
    text. A flat name renders as ``"'x'"``; a namespaced name renders as
    ``"'x' of subgraph 'inner'"`` (one level) or
    ``"'x' of subgraph 'middle.inner'"`` (multi-level chain).

    The format is stable and grep-friendly: callers can interpolate it
    directly without further punctuation choices.
    """
    if "." not in path:
        return f"{path!r}"
    head, leaf = path.rsplit(".", 1)
    return f"{leaf!r} of subgraph {head!r}"


def suggest_addresses(name: str, candidates: Iterable[str]) -> tuple[str, ...]:
    """Rank the addressable names ``name`` was most likely meant to be.

    An address is a path, so ranking is by path structure first and string
    distance only as a last resort. Tiers, best first:

    1. exact segment suffix -- ``'docs'`` and ``'indexer.docs'`` both resolve to
       ``'embedder.indexer.docs'``; a leaf exposed by several boundaries is
       genuinely ambiguous and every boundary is listed;
    2. one mistyped segment at the same depth -- ``'embeder.indexer.docs'``;
    3. one mistyped segment in a suffix -- ``'dcos'``, ``'indexr.docs'``;
    4. whole-path distance -- catches a dropped middle segment such as
       ``'embedder.docs'``.

    Only the first tier that matches contributes, so a structural match is
    never outranked by a string-distance coincidence (the bug in #90: ``'docs'``
    scoring closer to ``'embedder.indexer.overwrite'`` than to its own address).

    Returns:
        Candidates from ``candidates`` only -- a suggestion is always a name the
        caller can actually address -- sorted for determinism, empty when
        nothing ranks.
    """
    pool = sorted({candidate for candidate in candidates if candidate != name})
    if not pool:
        return ()
    segments = name.split(".")
    # ``or`` short-circuits on the first non-empty tier -- this chain IS the
    # ranking; a later tier never sees a name an earlier one answered.
    return _exact_suffix(segments, pool) or _typo_at_same_depth(segments, pool) or _typo_in_suffix(segments, pool) or _whole_path_distance(name, pool)


def format_did_you_mean(name: str, candidates: Iterable[str]) -> str:
    """Render the one ``Did you mean ...?`` clause used across the codebase.

    Single source of truth for suggestion wording and ordering, so ``bind``,
    ``select``, build-time name checks and runner input validation read alike.

    Returns:
        ``"Did you mean 'a'?"`` (or ``"... 'a' or 'b'?"`` / ``"... 'a', 'b', or
        'c'?"`` for ties), and ``""`` when nothing ranks -- a complete miss
        stays silent rather than guessing.
    """
    matches = suggest_addresses(name, candidates)
    if not matches:
        return ""
    if len(matches) == 1:
        return f"Did you mean {matches[0]!r}?"
    if len(matches) == 2:
        return f"Did you mean {matches[0]!r} or {matches[1]!r}?"
    listed = ", ".join(repr(match) for match in matches[:-1])
    return f"Did you mean {listed}, or {matches[-1]!r}?"


def _exact_suffix(segments: list[str], pool: list[str]) -> tuple[str, ...]:
    """Candidates that end in exactly these segments (a missing address prefix)."""
    depth = len(segments)
    return tuple(candidate for candidate in pool if candidate.split(".")[-depth:] == segments)


def _typo_at_same_depth(segments: list[str], pool: list[str]) -> tuple[str, ...]:
    """Candidates of the same depth differing in exactly one mistyped segment."""
    depth = len(segments)
    scored = [
        (score, candidate)
        for candidate in pool
        if len(parts := candidate.split(".")) == depth and (score := _one_segment_off(segments, parts)) is not None
    ]
    return _best(scored)


def _typo_in_suffix(segments: list[str], pool: list[str]) -> tuple[str, ...]:
    """Deeper candidates whose trailing segments differ in exactly one typo."""
    depth = len(segments)
    scored = [
        (score, candidate)
        for candidate in pool
        if len(parts := candidate.split(".")) > depth and (score := _one_segment_off(segments, parts[-depth:])) is not None
    ]
    return _best(scored)


def _whole_path_distance(name: str, pool: list[str]) -> tuple[str, ...]:
    """Last resort: closeness of the whole path, for a dropped middle segment."""
    scored = [(ratio, candidate) for candidate in pool if (ratio := _ratio(name, candidate)) >= _TYPO_CUTOFF]
    return _best(scored)


def _one_segment_off(segments: list[str], other: list[str]) -> float | None:
    """Similarity of the single differing segment, or ``None`` if not exactly one."""
    differing = [index for index, segment in enumerate(segments) if segment != other[index]]
    if len(differing) != 1:
        return None
    ratio = _ratio(segments[differing[0]], other[differing[0]])
    return ratio if ratio >= _TYPO_CUTOFF else None


def _ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def _best(scored: list[tuple[float, str]]) -> tuple[str, ...]:
    """Every candidate tied for the top score, in deterministic name order."""
    if not scored:
        return ()
    top = max(score for score, _ in scored)
    return tuple(sorted(candidate for score, candidate in scored if score == top))
