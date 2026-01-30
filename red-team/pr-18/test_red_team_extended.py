
import pytest
import asyncio
from hypergraph import Graph, node, SyncRunner, AsyncRunner, GraphConfigError, RunStatus

# =============================================================================
# Map Operations
# =============================================================================
def test_map_empty_list():
    """Verify behavior of map with empty list."""

    @node(output_name="out")
    def double(x: int) -> int:
        return x * 2

    graph = Graph(nodes=[double])
    runner = SyncRunner()

    # Should return empty list, not fail
    res = runner.map(graph, values={"x": []}, map_over="x")
    assert res == []

def test_map_zip_mismatch():
    """Verify map with mismatched list lengths (zip mode)."""

    @node(output_name="sum")
    def add(a: int, b: int) -> int:
        return a + b

    graph = Graph(nodes=[add])
    runner = SyncRunner()

    # a has 2, b has 3 items
    try:
        runner.map(graph, values={"a": [1, 2], "b": [1, 2, 3]}, map_over=["a", "b"])
        pytest.fail("Should have raised ValueError for mismatched lengths")
    except ValueError as e:
        assert "lengths" in str(e).lower()

# =============================================================================
# Argument Handling (*args, **kwargs)
# =============================================================================
@pytest.mark.xfail(strict=True, reason="Design Flaw: **kwargs are not detected as graph inputs")
def test_kwargs_support():
    """Verify support for nodes with **kwargs."""

    @node(output_name="out")
    def flexible_node(a: int, **kwargs) -> dict:
        return {"a": a, "extra": kwargs.get("b", 0)}

    # Graph construction relies on signature inspection.
    # 'b' is not in signature, so it won't be in graph.inputs
    graph = Graph(nodes=[flexible_node])
    runner = SyncRunner()

    # If we provide 'b', validation will likely reject it as "internal/unexpected"
    # OR (more likely) complain it's not a valid input because it wasn't detected.

    try:
        res = runner.run(graph, {"a": 1, "b": 2})
        assert res["out"]["extra"] == 2
    except Exception as e:
        # If it fails, it means kwargs aren't supported
        pytest.fail(f"kwargs support failed: {e}")

# =============================================================================
# Nested Graph Edge Cases
# =============================================================================
def test_nested_graph_bindings_persistence():
    """Verify bindings persist when graph is wrapped as node."""

    @node(output_name="y")
    def add_k(x: int, k: int) -> int:
        return x + k

    inner = Graph(nodes=[add_k], name="inner")
    # Bind k=10
    inner_bound = inner.bind(k=10)

    # Wrap as node
    gn = inner_bound.as_node()

    # Outer graph
    outer = Graph(nodes=[gn])
    runner = SyncRunner()

    # Run. Should not ask for k.
    res = runner.run(outer, {"x": 5})
    assert res["y"] == 15

def test_recursive_graph_structure_conflict():
    """Verify validation catches conflicts in recursive-like structures."""

    @node(output_name="x")
    def identity(x: int) -> int: return x

    g1_placeholder = Graph(nodes=[identity], name="g1") # Empty for now

    @node(output_name="y")
    def n2(x: int) -> int: return x

    # g2 contains g1_placeholder
    g2 = Graph(nodes=[g1_placeholder.as_node(), n2], name="g2")

    # Try to put g2 inside a NEW g1.
    # This creates a collision: g2 produces 'x' (via g1_placeholder), and 'identity' produces 'x'.
    try:
        g1 = Graph(nodes=[g2.as_node(), identity], name="g1")
        pytest.fail("Should have raised GraphConfigError due to output conflict")
    except GraphConfigError as e:
        assert "Multiple nodes produce 'x'" in str(e)

# =============================================================================
# Async Exception Handling
# =============================================================================
@pytest.mark.asyncio
async def test_async_map_exception_propagation():
    """Verify that if one item in async map fails, it returns FAILED status."""

    @node(output_name="res")
    async def risky(x: int) -> int:
        if x == 0:
            raise ValueError("Boom")
        return x

    graph = Graph(nodes=[risky])
    runner = AsyncRunner()

    # One success (1), one failure (0)
    results = await runner.map(graph, values={"x": [1, 0]}, map_over="x")

    assert len(results) == 2
    assert results[0].status == RunStatus.COMPLETED
    assert results[0].values["res"] == 1

    assert results[1].status == RunStatus.FAILED
    assert isinstance(results[1].error, ValueError)
    assert "Boom" in str(results[1].error)
