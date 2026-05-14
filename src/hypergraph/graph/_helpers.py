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
        if isinstance(value, dict) and child is not None and child.namespaced:
            if key in flat_input_names:
                raise ValueError(
                    f"Ambiguous addressing: {key!r} is both a flat input of this graph and the "
                    f"name of a child GraphNode. Pass a flat dict value via the resolved address "
                    f"(e.g., {{{key + '.<inner>'!r}: ...}}) to disambiguate."
                )
            for sub_key, sub_value in flatten_subgraph_addressing(value, child.graph).items():
                candidate = f"{key}.{sub_key}"
                if candidate in child.inputs:
                    full_key = candidate
                else:
                    replacement = child.replacement_for_stale_input_address(candidate)
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
