"""Shared helper functions for graph analysis.

These utilities are used by both core.py and input_spec.py to avoid duplication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import networkx as nx

if TYPE_CHECKING:
    from hypergraph.graph.core import Graph
    from hypergraph.nodes.base import HyperNode


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


def sources_of(output: str, nodes: dict[str, HyperNode]) -> list[str]:
    """Get all node names that produce the given output."""
    return [node.name for node in nodes.values() if output in node.outputs]


def flatten_subgraph_addressing(values: dict[str, Any], graph: Graph) -> dict[str, Any]:
    """Canonicalize nested-dict addressing into dot-paths for a graph's inputs.

    A user can address a GraphNode's private input either as a dot-path
    (``{"A.x": v}``) or as a nested dict (``{"A": {"x": v}}``). This walks
    the graph's child GraphNodes recursively and flattens nested-dict entries
    whose outer key names a child into dot-paths. Dict values whose outer key
    is NOT a child GraphNode pass through unchanged (the dict IS the value).

    Single source of truth used by both the runner boundary
    (``normalize_inputs``) and ``Graph.bind`` so the two surfaces stay in
    lockstep.

    Raises:
        ValueError: when the same leaf is addressed both as dot-path and
            as nested-dict in the same call.
    """
    from hypergraph.nodes.graph_node import GraphNode

    flat: dict[str, Any] = {}
    for key, value in values.items():
        child = graph._nodes.get(key) if isinstance(graph._nodes.get(key), GraphNode) else None
        if isinstance(value, dict) and child is not None:
            for sub_key, sub_value in flatten_subgraph_addressing(value, child.graph).items():
                full_key = f"{key}.{sub_key}"
                if full_key in flat:
                    raise ValueError(f"Input key {full_key!r} provided twice (mixed dot-path and nested-dict).")
                flat[full_key] = sub_value
        else:
            if key in flat:
                raise ValueError(f"Input key {key!r} provided twice (mixed dot-path and nested-dict).")
            flat[key] = value
    return flat


def describe_addressed_input(path: str) -> str:
    """Render a human-readable description of a (possibly dot-pathed) input.

    Single source of truth for inlining input addresses into error / warning
    text. A flat name renders as ``"'x'"``; a dot-pathed name renders as
    ``"'x' of subgraph 'inner'"`` (one level) or
    ``"'x' of subgraph 'middle.inner'"`` (multi-level chain).

    The format is stable and grep-friendly: callers can interpolate it
    directly without further punctuation choices.
    """
    if "." not in path:
        return f"{path!r}"
    head, leaf = path.rsplit(".", 1)
    return f"{leaf!r} of subgraph {head!r}"
