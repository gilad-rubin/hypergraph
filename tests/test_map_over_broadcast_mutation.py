"""Reproduce: map_over broadcast values are shared by reference.

If a node mutates a broadcast input in-place, subsequent iterations
see the mutated version instead of the original.
"""

from hypergraph import Graph, node
from hypergraph import SyncRunner


@node(output_name="result")
def append_to_shared(items: list, value: int) -> list:
    """Simulates a node that mutates a shared input in-place (like ComfyUI's ReferenceLatent)."""
    items.append(value)
    return list(items)  # return a copy as output, but damage is done


def test_broadcast_mutation_isolation():
    """Each map_over iteration should see the original broadcast value, not one mutated by prior iterations."""
    inner = Graph([append_to_shared], name="inner")
    mapped = inner.as_node(name="mapped").map_over("value")
    outer = Graph([mapped], name="outer")

    runner = SyncRunner()
    result = runner.run(outer, {"items": [0], "value": [1, 2, 3]})

    # Expected: each iteration starts with [0], appends its own value
    # [0, 1], [0, 2], [0, 3]
    assert result["result"] == [[0, 1], [0, 2], [0, 3]], (
        f"Broadcast mutation leaked across iterations: {result['result']}"
    )


if __name__ == "__main__":
    test_broadcast_mutation_isolation()
    print("PASS")
