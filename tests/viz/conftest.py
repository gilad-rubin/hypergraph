"""Shared fixtures for visualization tests."""

from typing import Any

import pytest

from hypergraph import Graph, node
from hypergraph.nodes.gate import END, ifelse, route


# =============================================================================
# Simple Node Functions
# =============================================================================


@node(output_name="doubled")
def double(x: int) -> int:
    """Double a number."""
    return x * 2


@node(output_name="tripled")
def triple(x: int) -> int:
    """Triple a number."""
    return x * 3


@node(output_name="result")
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@node(output_name="output")
def identity(x: int) -> int:
    """Return input unchanged."""
    return x


# =============================================================================
# Branch Nodes
# =============================================================================


@ifelse(when_true="double", when_false="triple")
def is_even(x: int) -> bool:
    """Check if a number is even."""
    return x % 2 == 0


@route(targets=["double", "triple", END])
def classify(x: int) -> str:
    """Route based on value."""
    if x > 10:
        return "double"
    elif x > 5:
        return "triple"
    else:
        return END


# =============================================================================
# Graph Fixtures
# =============================================================================


@pytest.fixture
def simple_graph() -> Graph:
    """Single node graph."""
    return Graph(nodes=[double])


@pytest.fixture
def linear_graph() -> Graph:
    """Three-node linear data flow."""
    @node(output_name="doubled")
    def double_fn(x: int) -> int:
        return x * 2

    @node(output_name="tripled")
    def triple_fn(doubled: int) -> int:
        return doubled * 3

    @node(output_name="result")
    def add_fn(tripled: int, y: int) -> int:
        return tripled + y

    return Graph(nodes=[double_fn, triple_fn, add_fn])


@pytest.fixture
def branching_graph() -> Graph:
    """Graph with ifelse branch node."""
    return Graph(nodes=[is_even, double, triple])


@pytest.fixture
def nested_graph() -> Graph:
    """Graph with one level of nesting."""
    inner = Graph(nodes=[double], name="inner")
    return Graph(nodes=[inner.as_node(), add])


@pytest.fixture
def double_nested_graph() -> Graph:
    """Graph with two levels of nesting."""
    innermost = Graph(nodes=[double], name="innermost")
    middle = Graph(nodes=[innermost.as_node(), triple], name="middle")
    return Graph(nodes=[middle.as_node(), add])


@pytest.fixture
def bound_graph() -> Graph:
    """Graph with bound input."""
    return Graph(nodes=[add]).bind(a=5)


# =============================================================================
# Utility Functions
# =============================================================================


def normalize_render_output(render_output: dict[str, Any]) -> dict[str, Any]:
    """Normalize render output for comparison.

    Removes position-dependent fields and sorts collections to make
    structural comparisons easier.

    Args:
        render_output: Output from render_graph()

    Returns:
        Normalized output with:
        - Positions removed
        - Nodes sorted by id
        - Edges sorted by id
        - Collections within nodes sorted
    """
    normalized = {
        "nodes": [],
        "edges": [],
        "meta": render_output.get("meta", {}),
    }

    # Normalize nodes - remove positions, sort
    for node in render_output.get("nodes", []):
        norm_node = {
            "id": node["id"],
            "type": node["type"],
            "data": dict(node["data"]),
        }

        # Include parent reference if present
        if "parentNode" in node:
            norm_node["parentNode"] = node["parentNode"]

        # Sort inputs/outputs if present
        if "inputs" in norm_node["data"]:
            norm_node["data"]["inputs"] = sorted(
                norm_node["data"]["inputs"], key=lambda x: x["name"]
            )
        if "outputs" in norm_node["data"]:
            norm_node["data"]["outputs"] = sorted(
                norm_node["data"]["outputs"], key=lambda x: x["name"]
            )

        normalized["nodes"].append(norm_node)

    # Sort nodes by id
    normalized["nodes"].sort(key=lambda x: x["id"])

    # Normalize edges - just copy structure, sort
    for edge in render_output.get("edges", []):
        norm_edge = {
            "id": edge["id"],
            "source": edge["source"],
            "target": edge["target"],
            "data": dict(edge.get("data", {})),
        }
        normalized["edges"].append(norm_edge)

    # Sort edges by id
    normalized["edges"].sort(key=lambda x: x["id"])

    return normalized
