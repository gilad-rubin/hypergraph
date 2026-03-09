"""Daft DataFrame runner for hypergraph.

Translates a hypergraph Graph into a Daft DataFrame pipeline,
executing each node as a UDF column operation in topological order.

Usage:
    from hypergraph.integrations.daft import DaftRunner

    runner = DaftRunner()
    result = runner.run(graph, x=42)
    results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")
"""

from hypergraph.integrations.daft.runner import DaftRunner

__all__ = ["DaftRunner"]
