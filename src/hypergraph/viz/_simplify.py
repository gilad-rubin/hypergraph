"""Transitive reduction of visible graph edges — the ``simplify`` option.

``A ──▶ B ──▶ C`` plus a direct ``A ──▶ C`` draws a shortcut that adds no
reachability information, so the direct edge is dropped and the reader follows
the chain instead.

This module is the single derivation authority for that decision. Three
consumers share it so the interactive widget, the Python scene oracle and the
Mermaid exporter never disagree about which edges are redundant:

1. ``scene_builder.py`` — React Flow scene edges (and, via the twin
   ``simplifyTransitiveEdges`` in ``assets/scene_builder.js``, the browser).
2. ``mermaid.py`` — resolved Mermaid edge lines.
3. Tests, which assert against :func:`redundant_edge_keys` directly.

Callers own edge identity and edge classification; this module only answers
"is this edge implied by a longer path?".
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeRef:
    """One edge, as the reduction needs to see it.

    Attributes:
        key: Caller-owned identity returned in the redundant set.
        source: Resolved source node id.
        target: Resolved target node id.
        removable: True only for plain data edges the caller is willing to
            drop. Structural producer→DATA (``output``) edges, INPUT edges and
            START/END edges are scaffolding; mutex (``exclusive``) arms are
            alternatives rather than a chain plus a shortcut. All pass ``False``
            and can only ever act as path segments.
        traversable: True only for edges on the **data-flow spine** — the
            edges that actually carry a value from one node to the next. Two
            exclusions matter:

            - Control and ordering edges. A gate's dotted ``gate ⇢ archive``
              means "archive may run", not "archive receives this value", so it
              cannot justify dropping ``deliver → archive``: the reader would
              be left with no indication that ``archive`` consumes anything.
            - Back edges. In a cycle every edge is reachable "the long way
              round", so leaving them in would let the reduction eat the loop.
    """

    key: Hashable
    source: str
    target: str
    removable: bool
    traversable: bool = True


def redundant_edge_keys(edges: Iterable[EdgeRef]) -> set[Hashable]:
    """Return the keys of removable edges that a longer path already implies.

    Reachability is preserved: because every dropped edge is justified by a
    path through the kept graph, no node loses its last connection.

    Callers must pass the edges that are *currently visible*. Redundancy is a
    property of the rendered view, not of the graph definition — an edge that
    is a shortcut while a container is collapsed can be the only path once that
    container expands.

    Cost is one traversal per candidate edge — O(V·E) worst case. That is
    deliberate: it runs on every expand/collapse in the browser, so measured
    numbers matter more than the bound. A 50-node graph is well under 1 ms and
    200 nodes / 1k edges is ~13 ms; a synthetic 1000-node graph with fan-in 10
    reaches ~1 s. Real diagrams are far below that (nobody reads a 1000-node
    canvas), so there is no size cutoff — a cutoff would silently change what
    the diagram shows at a threshold, which is worse than a slow click. If a
    graph ever does get that large, memoize forward reachability per source
    node instead of re-walking per edge.
    """
    edges = list(edges)

    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        if not edge.traversable:
            continue
        adjacency.setdefault(edge.source, set()).add(edge.target)

    redundant: set[Hashable] = set()
    for edge in edges:
        if not edge.removable or edge.source == edge.target:
            continue
        if _has_indirect_path(adjacency, edge.source, edge.target):
            redundant.add(edge.key)
    return redundant


def _has_indirect_path(adjacency: dict[str, set[str]], source: str, target: str) -> bool:
    """True if ``target`` is reachable from ``source`` without ever taking the
    direct ``source -> target`` edge."""
    stack = [n for n in adjacency.get(source, ()) if n != target]
    seen = set(stack)
    while stack:
        current = stack.pop()
        if current == target:
            return True
        for successor in adjacency.get(current, ()):
            if current == source and successor == target:
                continue  # never traverse the edge under test
            if successor not in seen:
                seen.add(successor)
                stack.append(successor)
    return False
