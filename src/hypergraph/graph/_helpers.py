"""Shared helper functions for graph analysis.

These utilities are used by both core.py and input_spec.py to avoid duplication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
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
