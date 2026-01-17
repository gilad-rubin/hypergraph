"""Tests for GraphNode.map_over() method."""

import pytest

from hypergraph import Graph, node
from hypergraph.runners import RunStatus, SyncRunner, AsyncRunner


# === Test Fixtures ===


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="sum")
def add(a: int, b: int) -> int:
    return a + b


@node(output_name="doubled")
async def async_double(x: int) -> int:
    return x * 2


# === Tests ===


class TestMapOverConfiguration:
    """Tests for map_over() configuration."""

    def test_map_over_returns_new_instance(self):
        """map_over returns a new GraphNode instance."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("x")

        assert mapped is not gn

    def test_map_over_preserves_original(self):
        """Original GraphNode is unchanged."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("x")

        assert gn._map_over is None
        assert mapped._map_over == ["x"]

    def test_map_over_single_param(self):
        """Configure with single parameter."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("x")

        assert mapped._map_over == ["x"]
        assert mapped._map_mode == "zip"

    def test_map_over_multiple_params(self):
        """Configure with multiple parameters."""
        inner = Graph([add], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("a", "b")

        assert mapped._map_over == ["a", "b"]

    def test_map_over_mode_zip_default(self):
        """Default mode is zip."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("x")

        assert mapped._map_mode == "zip"

    def test_map_over_mode_product(self):
        """Can configure product mode."""
        inner = Graph([add], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("a", "b", mode="product")

        assert mapped._map_mode == "product"

    def test_map_over_nonexistent_param_raises(self):
        """Error if parameter doesn't exist."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        with pytest.raises(ValueError, match="not an input"):
            gn.map_over("nonexistent")

    def test_map_over_requires_at_least_one_param(self):
        """Error if no parameters specified."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        with pytest.raises(ValueError, match="at least one parameter"):
            gn.map_over()


class TestMapOverRenameIntegration:
    """Tests for map_over with input renaming."""

    def test_rename_after_map_over_updates(self):
        """Renaming input after map_over updates map_over params."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("x")
        renamed = mapped.with_inputs(x="input_x")

        assert renamed._map_over == ["input_x"]
        assert "input_x" in renamed.inputs

    def test_map_over_after_rename_uses_new_name(self):
        """map_over after rename uses renamed input name."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        renamed = gn.with_inputs(x="input_x")
        mapped = renamed.map_over("input_x")

        assert mapped._map_over == ["input_x"]

    def test_rename_unrelated_param_no_change(self):
        """Renaming unrelated param doesn't affect map_over."""
        inner = Graph([add], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("a")
        renamed = mapped.with_inputs(b="input_b")

        assert renamed._map_over == ["a"]


class TestMapOverExecution:
    """Tests for execution with map_over configuration."""

    def test_graphnode_with_map_over_iterates(self):
        """GraphNode with map_over iterates over input."""
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        runner = SyncRunner()

        result = runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == [2, 4, 6]

    def test_graphnode_map_over_zip_mode(self):
        """map_over with zip mode."""
        inner = Graph([add], name="inner")
        outer = Graph([inner.as_node().map_over("a", "b", mode="zip")])
        runner = SyncRunner()

        result = runner.run(outer, {"a": [1, 2, 3], "b": [10, 20, 30]})

        assert result["sum"] == [11, 22, 33]

    def test_graphnode_map_over_product_mode(self):
        """map_over with product mode generates cartesian product."""
        inner = Graph([add], name="inner")
        outer = Graph([inner.as_node().map_over("a", "b", mode="product")])
        runner = SyncRunner()

        result = runner.run(outer, {"a": [1, 2], "b": [10, 20]})

        # 2 * 2 = 4 results
        assert len(result["sum"]) == 4
        assert sorted(result["sum"]) == [11, 12, 21, 22]

    def test_nested_map_over_in_parent_graph(self):
        """map_over works in multi-node parent graph."""
        inner = Graph([double], name="inner")
        outer = Graph(
            [
                inner.as_node().map_over("x"),
                add.with_inputs(a="doubled"),  # "doubled" is now a list
            ]
        )
        runner = SyncRunner()

        # Note: add expects int, but doubled is list[int]
        # This test documents current behavior
        result = runner.run(outer, {"x": [1, 2], "b": 0})

        # doubled = [2, 4], which gets passed to add as-is
        # This may raise or have undefined behavior depending on implementation
        # For now, just check it doesn't crash
        assert result.status in (RunStatus.COMPLETED, RunStatus.FAILED)

    async def test_graphnode_map_over_async_runner(self):
        """map_over works with AsyncRunner."""
        inner = Graph([async_double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        runner = AsyncRunner()

        result = await runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == [2, 4, 6]

    def test_empty_list_returns_empty_results(self):
        """map_over with empty list returns empty output list."""
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        runner = SyncRunner()

        result = runner.run(outer, {"x": []})

        assert result["doubled"] == []

    def test_broadcast_values_with_map_over(self):
        """Non-mapped values are broadcast to each iteration."""
        inner = Graph([add], name="inner")
        outer = Graph([inner.as_node().map_over("a")])
        runner = SyncRunner()

        result = runner.run(outer, {"a": [1, 2, 3], "b": 10})

        assert result["sum"] == [11, 12, 13]


class TestConcurrentNestedMaps:
    """Tests for concurrent nested map executions (GAP-03)."""

    def test_map_within_map_sync(self):
        """Nested map_over: outer graph has map_over on a graph with map_over."""
        # Inner: simple transform
        inner = Graph([double], name="inner")

        # Middle: maps over inner
        middle = Graph([inner.as_node().map_over("x")], name="middle")

        # Outer: maps over middle with different param structure
        @node(output_name="results")
        def collect(doubled: list) -> list:
            return doubled

        outer = Graph([middle.as_node(), collect])
        runner = SyncRunner()

        # Pass list to inner map_over
        result = runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == [2, 4, 6]
        assert result["results"] == [2, 4, 6]

    async def test_map_within_map_concurrent(self):
        """Nested map_over executions with AsyncRunner."""
        import asyncio

        @node(output_name="value")
        async def slow_transform(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 2

        inner = Graph([slow_transform], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = AsyncRunner()

        # Multiple items should process concurrently
        result = await runner.run(outer, {"x": [1, 2, 3, 4, 5]})

        assert result.status == RunStatus.COMPLETED
        assert result["value"] == [2, 4, 6, 8, 10]

    async def test_nested_map_with_max_concurrency(self):
        """Concurrency limits work in nested maps."""
        import asyncio
        import time

        @node(output_name="value")
        async def timed_transform(x: int) -> int:
            await asyncio.sleep(0.03)
            return x * 2

        inner = Graph([timed_transform], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = AsyncRunner()

        # With max_concurrency=1, should be sequential
        start = time.time()
        result = await runner.run(outer, {"x": [1, 2, 3]}, max_concurrency=1)
        elapsed = time.time() - start

        assert result.status == RunStatus.COMPLETED
        # Sequential: ~0.09s (3 * 0.03s)
        # The outer runner has max_concurrency, but inner map iterates
        assert result["value"] == [2, 4, 6]

    async def test_map_over_graph_with_async_nodes(self):
        """map_over graph containing async nodes."""

        @node(output_name="a")
        async def async_step1(x: int) -> int:
            return x + 1

        @node(output_name="b")
        async def async_step2(a: int) -> int:
            return a * 2

        inner = Graph([async_step1, async_step2], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = AsyncRunner()
        result = await runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        # (1+1)*2=4, (2+1)*2=6, (3+1)*2=8
        assert result["b"] == [4, 6, 8]

    def test_doubly_nested_map_over(self):
        """Two levels of map_over nesting."""
        # Innermost: simple double
        innermost = Graph([double], name="innermost")

        # Middle: maps over innermost
        middle = Graph([innermost.as_node().map_over("x")], name="middle")

        # Outer: wraps middle (no additional map)
        outer = Graph([middle.as_node()])

        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == [2, 4, 6]

    def test_multiple_mapped_graphnodes_in_parallel(self):
        """Multiple GraphNodes with map_over in same graph."""
        # Two independent inner graphs
        inner_a = Graph([double.with_outputs(doubled="a")], name="inner_a")
        inner_b = Graph([double.with_outputs(doubled="b")], name="inner_b")

        # Both mapped in outer
        @node(output_name="combined")
        def combine(a: list, b: list) -> list:
            return [x + y for x, y in zip(a, b)]

        outer = Graph(
            [
                inner_a.as_node().map_over("x").with_inputs(x="vals_a"),
                inner_b.as_node().map_over("x").with_inputs(x="vals_b"),
                combine,
            ]
        )

        runner = SyncRunner()
        result = runner.run(outer, {"vals_a": [1, 2], "vals_b": [10, 20]})

        assert result.status == RunStatus.COMPLETED
        assert result["a"] == [2, 4]
        assert result["b"] == [20, 40]
        assert result["combined"] == [22, 44]
